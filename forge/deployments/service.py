import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from forge.core.errors import (
    ConcurrencyError,
    EntityNotFoundError,
    ForgeError,
    ValidationError,
)
from forge.core.models import (
    Application,
    Deployment,
    DeploymentStatus,
    EventKind,
    validate_health_check_path,
)
from forge.proxy.base import Proxy
from forge.runtime.base import Runtime
from forge.storage.db import Database
from forge.storage.repository import (
    ApplicationRepository,
    DeploymentRepository,
    EnvironmentRepository,
    EventRepository,
    RevisionRepository,
)


class DeploymentFailedError(ForgeError):
    def __init__(self, message: str, exit_code: int = 1, logs: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.logs = logs


class DeploymentService:
    def __init__(self, db: Database, runtime: Runtime, proxy: Proxy) -> None:
        self.db = db
        self.runtime = runtime
        self.proxy = proxy
        self.app_repo = ApplicationRepository(db)
        self.dep_repo = DeploymentRepository(db)
        self.event_repo = EventRepository(db)
        self.env_repo = EnvironmentRepository(db)
        self.rev_repo = RevisionRepository(db)

        self._locks_mutex = threading.Lock()
        self._app_locks: dict[str, threading.Lock] = {}
        self._in_flight_mutex = threading.Lock()
        self._in_flight_deployments: set[str] = set()

    def _get_app_lock(self, app_id: str) -> threading.Lock:
        with self._locks_mutex:
            if app_id not in self._app_locks:
                self._app_locks[app_id] = threading.Lock()
            return self._app_locks[app_id]

    def register_in_flight(self, deployment_id: str) -> None:
        with self._in_flight_mutex:
            self._in_flight_deployments.add(deployment_id)

    def unregister_in_flight(self, deployment_id: str) -> None:
        with self._in_flight_mutex:
            self._in_flight_deployments.discard(deployment_id)

    def get_in_flight_deployment_ids(self) -> set[str]:
        with self._in_flight_mutex:
            return set(self._in_flight_deployments)

    def create_deployment(self, app_id: str) -> Deployment:
        app = self.app_repo.get_by_id(app_id)
        if app is None:
            raise EntityNotFoundError(f"Application '{app_id}' not found")

        # Priority 2: Fix deployment creation race at the persistence/concurrency boundary.
        # Acquire app lock so concurrent requests for same app cannot race between check and insert.
        lock = self._get_app_lock(app.id)
        acquired = lock.acquire(blocking=False)
        if not acquired:
            raise ConcurrencyError(
                f"Deployment already in progress for application '{app.name}' ({app.id})"
            )
        try:
            in_progress = self.dep_repo.get_in_progress_deployment(app_id)
            if in_progress is not None:
                raise ConcurrencyError(
                    f"Deployment already in progress for application '{app.name}' ({app.id})"
                )

            dep = self.dep_repo.create(app.id, status=DeploymentStatus.PENDING)
            self.event_repo.record(
                app_id=app.id,
                event_kind=EventKind.DEPLOYMENT_CREATED,
                payload={"deployment_id": dep.id, "app_name": app.name},
                deployment_id=dep.id,
            )
            return dep
        finally:
            lock.release()

    def execute_deployment(
        self,
        deployment_id: str,
        context_path: Path,
        health_check_path: str = "/health",
        health_check_timeout: float = 15.0,
        health_check_interval: float = 0.5,
        build_log_callback: Callable[[str], None] | None = None,
    ) -> Deployment:
        # SEC-05: Strict SSRF protection on health_check_path
        validate_health_check_path(health_check_path)

        dep = self.dep_repo.get_by_id(deployment_id)
        if dep is None:
            raise EntityNotFoundError(f"Deployment '{deployment_id}' not found")

        app = self.app_repo.get_by_id(dep.app_id)
        if app is None:
            raise EntityNotFoundError(f"Application '{dep.app_id}' not found")

        dockerfile = context_path / "Dockerfile"
        if not dockerfile.is_file():
            raise FileNotFoundError(f"Dockerfile not found in '{context_path}'")

        lock = self._get_app_lock(app.id)
        # Concurrency protection: reject simultaneous deployments on same app
        acquired = lock.acquire(blocking=False)
        if not acquired:
            raise ConcurrencyError(
                f"Deployment already in progress for application '{app.name}' ({app.id})"
            )

        self.register_in_flight(dep.id)
        try:
            return self._execute_deployment(
                app=app,
                dep=dep,
                context_path=context_path,
                health_check_path=health_check_path,
                health_check_timeout=health_check_timeout,
                health_check_interval=health_check_interval,
                build_log_callback=build_log_callback,
            )
        finally:
            self.unregister_in_flight(dep.id)
            lock.release()

    def deploy(
        self,
        app_id: str,
        context_path: Path,
        health_check_path: str = "/health",
        health_check_timeout: float = 15.0,
        health_check_interval: float = 0.5,
        build_log_callback: Callable[[str], None] | None = None,
    ) -> Deployment:
        dep = self.create_deployment(app_id)
        return self.execute_deployment(
            deployment_id=dep.id,
            context_path=context_path,
            health_check_path=health_check_path,
            health_check_timeout=health_check_timeout,
            health_check_interval=health_check_interval,
            build_log_callback=build_log_callback,
        )

    def _execute_deployment(
        self,
        app: Application,
        dep: Deployment,
        context_path: Path,
        health_check_path: str,
        health_check_timeout: float,
        health_check_interval: float,
        build_log_callback: Callable[[str], None] | None,
    ) -> Deployment:
        self.runtime.ensure_network("forge-net")
        self.proxy.ensure_proxy()

        previous_active_dep = self.dep_repo.get_active_deployment(app.id)

        tag = f"{app.name}:{uuid.uuid4().hex[:7]}"
        candidate_name = f"{app.name}-{uuid.uuid4().hex[:7]}"

        # 2. State: BUILDING
        dep = self.dep_repo.update_status(dep.id, DeploymentStatus.BUILDING)
        self.event_repo.record(
            app_id=app.id,
            event_kind=EventKind.DEPLOYMENT_BUILD_STARTED,
            payload={"image_tag": tag},
            deployment_id=dep.id,
        )

        try:
            for line in self.runtime.build_image_stream(context_path, tag):
                if build_log_callback is not None:
                    build_log_callback(line)
        except Exception as exc:
            self.dep_repo.update_status(dep.id, DeploymentStatus.FAILED, error_message=str(exc))
            self.event_repo.record(
                app_id=app.id,
                event_kind=EventKind.DEPLOYMENT_FAILED,
                payload={"stage": "building", "error": str(exc)},
                deployment_id=dep.id,
            )
            raise DeploymentFailedError(f"Build failed for '{app.name}': {exc}") from exc

        rev = self.rev_repo.create(
            app_id=app.id,
            version_tag=tag.split(":")[-1] if ":" in tag else tag,
            image_name=tag,
            config_snapshot={"container_port": app.container_port, "domain": app.domain},
        )

        self.event_repo.record(
            app_id=app.id,
            event_kind=EventKind.DEPLOYMENT_BUILD_SUCCEEDED,
            payload={"image_tag": tag, "revision_id": rev.id},
            deployment_id=dep.id,
        )

        # 3. State: STARTING
        dep = self.dep_repo.update_status(
            dep.id,
            DeploymentStatus.STARTING,
            candidate_container_id=candidate_name,
            revision_id=rev.id,
        )

        labels = self.proxy.generate_labels(
            app_name=app.name,
            domain=app.domain,
            port=app.container_port,
            is_candidate=True,
        )
        labels["forge.managed"] = "true"
        labels["forge.app"] = app.name
        labels["forge.deployment_id"] = dep.id
        env_vars = self.env_repo.get_vars(app.id)

        try:
            # SEC-10: Candidate starts strictly with restart_policy="no"
            self.runtime.create_container(
                image_tag=tag,
                container_name=candidate_name,
                network="forge-net",
                labels=labels,
                env_vars=env_vars,
                port=app.container_port,
                restart_policy="no",
            )
            self.runtime.start_container(candidate_name)
            state = self.runtime.inspect_container(candidate_name)
        except Exception as exc:
            self.runtime.remove_container(candidate_name, force=True)
            self.dep_repo.update_status(dep.id, DeploymentStatus.FAILED, error_message=str(exc))
            self.event_repo.record(
                app_id=app.id,
                event_kind=EventKind.DEPLOYMENT_FAILED,
                payload={"stage": "starting", "error": str(exc)},
                deployment_id=dep.id,
            )
            raise DeploymentFailedError(f"Failed to start container '{candidate_name}': {exc}") from exc

        if not state.running:
            logs = self.runtime.logs(candidate_name)
            self.runtime.remove_container(candidate_name, force=True)
            err_msg = f"Container '{candidate_name}' exited with code {state.exit_code}"
            self.dep_repo.update_status(dep.id, DeploymentStatus.FAILED, error_message=err_msg)
            self.event_repo.record(
                app_id=app.id,
                event_kind=EventKind.DEPLOYMENT_FAILED,
                payload={"stage": "startup_crash", "exit_code": state.exit_code},
                deployment_id=dep.id,
            )
            raise DeploymentFailedError(err_msg, exit_code=state.exit_code, logs=logs)

        self.event_repo.record(
            app_id=app.id,
            event_kind=EventKind.DEPLOYMENT_CONTAINER_STARTED,
            payload={"candidate_container": candidate_name},
            deployment_id=dep.id,
        )

        # 4. State: HEALTH_CHECKING
        dep = self.dep_repo.update_status(dep.id, DeploymentStatus.HEALTH_CHECKING)

        is_healthy = self._run_health_check(
            container_name=candidate_name,
            port=app.container_port,
            path=health_check_path,
            timeout=health_check_timeout,
            interval=health_check_interval,
        )

        if not is_healthy:
            logs = self.runtime.logs(candidate_name)
            self.runtime.remove_container(candidate_name, force=True)
            err_msg = f"Health check failed on port {app.container_port}{health_check_path}"
            self.dep_repo.update_status(dep.id, DeploymentStatus.FAILED, error_message=err_msg)
            self.event_repo.record(
                app_id=app.id,
                event_kind=EventKind.DEPLOYMENT_FAILED,
                payload={"stage": "health_check", "error": err_msg},
                deployment_id=dep.id,
            )
            raise DeploymentFailedError(err_msg, exit_code=1, logs=logs)

        self.event_repo.record(
            app_id=app.id,
            event_kind=EventKind.DEPLOYMENT_HEALTH_CHECK_PASSED,
            payload={"path": health_check_path},
            deployment_id=dep.id,
        )

        # 5. Promotion & Decommission previous active deployment
        # Decommission previous active deployment if existed (transition to STOPPING before candidate is ACTIVE)
        if previous_active_dep is not None and previous_active_dep.id != dep.id:
            try:
                self.dep_repo.update_status(previous_active_dep.id, DeploymentStatus.STOPPING)
            except Exception:
                pass

        # State: ACTIVE (Promotion)
        dep = self.dep_repo.update_status(
            dep.id,
            DeploymentStatus.ACTIVE,
            active_container_id=candidate_name,
        )
        # Priority 1: Traefik candidate must never receive traffic before promotion.
        # Now that candidate is active, configure proxy routing to switch traffic to it.
        self.proxy.promote_service(
            app_name=app.name,
            domain=app.domain,
            container_name=candidate_name,
            port=app.container_port,
        )
        self.event_repo.record(
            app_id=app.id,
            event_kind=EventKind.DEPLOYMENT_PROMOTED,
            payload={"active_container": candidate_name},
            deployment_id=dep.id,
        )

        # 6. Finalize decommission of old container
        if previous_active_dep is not None:
            old_container = previous_active_dep.active_container_id
            if old_container and old_container != candidate_name:
                try:
                    self.runtime.stop_container(old_container, timeout=10)
                except Exception:
                    pass
                self.runtime.remove_container(old_container, force=True)
                self.dep_repo.update_status(previous_active_dep.id, DeploymentStatus.STOPPED)
                self.event_repo.record(
                    app_id=app.id,
                    event_kind=EventKind.DEPLOYMENT_STOPPED,
                    payload={"stopped_container": old_container},
                    deployment_id=previous_active_dep.id,
                )

        return dep

    def _run_health_check(
        self,
        container_name: str,
        port: int,
        path: str,
        timeout: float,
        interval: float,
    ) -> bool:
        # Priority 10: Remove Python-only assumptions from health-check mechanism.
        # Multi-strategy probe: curl -> wget -> python3/python -> node -> /dev/tcp.
        probe_cmd = [
            "sh",
            "-c",
            (
                f"if command -v curl >/dev/null 2>&1; then "
                f"curl -fsSL -o /dev/null http://127.0.0.1:{port}{path}; "
                f"elif command -v wget >/dev/null 2>&1; then "
                f"wget -q -O - http://127.0.0.1:{port}{path} >/dev/null 2>&1; "
                f"elif command -v python3 >/dev/null 2>&1; then "
                f"python3 -c \"import urllib.request, sys; sys.exit(0 if 200 <= urllib.request.urlopen('http://127.0.0.1:{port}{path}', timeout=2).getcode() < 400 else 1)\"; "
                f"elif command -v python >/dev/null 2>&1; then "
                f"python -c \"import urllib.request, sys; sys.exit(0 if 200 <= urllib.request.urlopen('http://127.0.0.1:{port}{path}', timeout=2).getcode() < 400 else 1)\"; "
                f"elif command -v node >/dev/null 2>&1; then "
                f"node -e \"const http=require('http'); http.get('http://127.0.0.1:{port}{path}', (r) => process.exit(r.statusCode < 400 ? 0 : 1)).on('error', () => process.exit(1));\"; "
                f"else "
                f"(echo -e 'GET {path} HTTP/1.0\\r\\nHost: 127.0.0.1\\r\\n\\r\\n' > /dev/tcp/127.0.0.1/{port}) 2>/dev/null; "
                f"fi"
            ),
        ]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            exec_res = self.runtime.exec(
                container_id=container_name,
                cmd=probe_cmd,
                timeout=min(5.0, timeout),
            )
            if exec_res.exit_code == 0:
                return True
            time.sleep(interval)
        return False

    def rollback(self, app_id: str, target_deployment_id: str | None = None) -> Deployment:
        app = self.app_repo.get_by_id(app_id)
        if app is None:
            raise EntityNotFoundError(f"Application '{app_id}' not found")

        lock = self._get_app_lock(app.id)
        acquired = lock.acquire(blocking=False)
        if not acquired:
            raise ConcurrencyError(
                f"Deployment already in progress for application '{app.name}' ({app.id})"
            )

        try:
            current_active = self.dep_repo.get_active_deployment(app.id)

            if target_deployment_id:
                target_dep = self.dep_repo.get_by_id(target_deployment_id)
                if target_dep is None or target_dep.app_id != app.id:
                    raise EntityNotFoundError(f"Target deployment '{target_deployment_id}' not found")
            else:
                history = self.dep_repo.list_by_app(app.id, limit=20)
                target_dep = None
                for d in history:
                    if (current_active is None or d.id != current_active.id) and d.status in (
                        DeploymentStatus.STOPPED,
                        DeploymentStatus.ACTIVE,
                    ):
                        target_dep = d
                        break
                if target_dep is None:
                    raise ValidationError("No previous successful deployment found to roll back to")

            image_tag = None
            if target_dep.revision_id:
                rev = self.rev_repo.get_by_id(target_dep.revision_id)
                if rev:
                    image_tag = rev.image_name

            if not image_tag:
                image_tag = f"{app.name}:latest"

            self.event_repo.record(
                app_id=app.id,
                event_kind=EventKind.DEPLOYMENT_ROLLBACK_STARTED,
                payload={
                    "target_deployment_id": target_dep.id,
                    "current_active_deployment_id": current_active.id if current_active else None,
                    "image_tag": image_tag,
                },
            )

            rollback_dep = self.dep_repo.create(
                app.id,
                revision_id=target_dep.revision_id,
                status=DeploymentStatus.PENDING,
            )

            # Priority 3: Fix rollback vs reconciler ownership race.
            # Register rollback deployment as in-flight so reconciler does not touch it.
            self.register_in_flight(rollback_dep.id)
            try:
                candidate_name = f"{app.name}-rb-{uuid.uuid4().hex[:7]}"
                rollback_dep = self.dep_repo.update_status(
                    rollback_dep.id,
                    DeploymentStatus.BUILDING,
                )
                rollback_dep = self.dep_repo.update_status(
                    rollback_dep.id,
                    DeploymentStatus.STARTING,
                    candidate_container_id=candidate_name,
                )

                labels = self.proxy.generate_labels(
                    app_name=app.name,
                    domain=app.domain,
                    port=app.container_port,
                    is_candidate=True,
                )
                labels["forge.managed"] = "true"
                labels["forge.app"] = app.name
                labels["forge.deployment_id"] = rollback_dep.id
                env_vars = self.env_repo.get_vars(app.id)

                try:
                    self.runtime.create_container(
                        image_tag=image_tag,
                        container_name=candidate_name,
                        network="forge-net",
                        labels=labels,
                        env_vars=env_vars,
                        port=app.container_port,
                        restart_policy="no",
                    )
                    self.runtime.start_container(candidate_name)
                    state = self.runtime.inspect_container(candidate_name)
                except Exception as exc:
                    self.runtime.remove_container(candidate_name, force=True)
                    self.dep_repo.update_status(rollback_dep.id, DeploymentStatus.FAILED, error_message=str(exc))
                    raise DeploymentFailedError(f"Rollback container start failed: {exc}") from exc

                if not state.running:
                    self.runtime.remove_container(candidate_name, force=True)
                    err_msg = f"Rollback container exited with code {state.exit_code}"
                    self.dep_repo.update_status(rollback_dep.id, DeploymentStatus.FAILED, error_message=err_msg)
                    raise DeploymentFailedError(err_msg, exit_code=state.exit_code)

                rollback_dep = self.dep_repo.update_status(rollback_dep.id, DeploymentStatus.HEALTH_CHECKING)
                is_healthy = self._run_health_check(
                    container_name=candidate_name,
                    port=app.container_port,
                    path="/health",
                    timeout=15.0,
                    interval=0.5,
                )
                if not is_healthy:
                    self.runtime.remove_container(candidate_name, force=True)
                    err_msg = f"Rollback health check failed on port {app.container_port}/health"
                    self.dep_repo.update_status(rollback_dep.id, DeploymentStatus.FAILED, error_message=err_msg)
                    raise DeploymentFailedError(err_msg)

                # Transition current active to STOPPING before promoting rollback_dep to ACTIVE
                if current_active is not None and current_active.id != rollback_dep.id:
                    try:
                        self.dep_repo.update_status(current_active.id, DeploymentStatus.STOPPING)
                    except Exception:
                        pass

                rollback_dep = self.dep_repo.update_status(
                    rollback_dep.id,
                    DeploymentStatus.ACTIVE,
                    active_container_id=candidate_name,
                )
                self.proxy.promote_service(
                    app_name=app.name,
                    domain=app.domain,
                    container_name=candidate_name,
                    port=app.container_port,
                )

                if current_active is not None and current_active.active_container_id:
                    old_container = current_active.active_container_id
                    if old_container != candidate_name:
                        try:
                            self.runtime.stop_container(old_container, timeout=10)
                        except Exception:
                            pass
                        self.runtime.remove_container(old_container, force=True)
                        self.dep_repo.update_status(current_active.id, DeploymentStatus.ROLLED_BACK)
                        self.event_repo.record(
                            app_id=app.id,
                            event_kind=EventKind.DEPLOYMENT_STOPPED,
                            payload={"stopped_container": old_container},
                            deployment_id=current_active.id,
                        )

                self.event_repo.record(
                    app_id=app.id,
                    event_kind=EventKind.DEPLOYMENT_ROLLBACK_COMPLETED,
                    payload={
                        "rollback_deployment_id": rollback_dep.id,
                        "target_deployment_id": target_dep.id,
                        "active_container": candidate_name,
                    },
                    deployment_id=rollback_dep.id,
                )
                return rollback_dep
            finally:
                self.unregister_in_flight(rollback_dep.id)
        finally:
            lock.release()
