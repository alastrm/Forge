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

        self._locks_mutex = threading.Lock()
        self._app_locks: dict[str, threading.Lock] = {}

    def _get_app_lock(self, app_id: str) -> threading.Lock:
        with self._locks_mutex:
            if app_id not in self._app_locks:
                self._app_locks[app_id] = threading.Lock()
            return self._app_locks[app_id]

    def create_deployment(self, app_id: str) -> Deployment:
        app = self.app_repo.get_by_id(app_id)
        if app is None:
            raise EntityNotFoundError(f"Application '{app_id}' not found")

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

        self.event_repo.record(
            app_id=app.id,
            event_kind=EventKind.DEPLOYMENT_BUILD_SUCCEEDED,
            payload={"image_tag": tag},
            deployment_id=dep.id,
        )

        # 3. State: STARTING
        dep = self.dep_repo.update_status(
            dep.id,
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

        # 5. State: ACTIVE (Promotion)
        dep = self.dep_repo.update_status(
            dep.id,
            DeploymentStatus.ACTIVE,
            active_container_id=candidate_name,
        )
        self.event_repo.record(
            app_id=app.id,
            event_kind=EventKind.DEPLOYMENT_PROMOTED,
            payload={"active_container": candidate_name},
            deployment_id=dep.id,
        )

        # 6. Decommission previous active deployment if existed
        if previous_active_dep is not None:
            old_container = previous_active_dep.active_container_id
            if old_container and old_container != candidate_name:
                try:
                    self.dep_repo.update_status(previous_active_dep.id, DeploymentStatus.STOPPING)
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
        python_script = (
            f"import urllib.request, sys; "
            f"sys.exit(0 if 200 <= urllib.request.urlopen('http://127.0.0.1:{port}{path}', timeout=2).getcode() < 400 else 1)"
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            exec_res = self.runtime.exec(
                container_id=container_name,
                cmd=["python", "-c", python_script],
                timeout=min(5.0, timeout),
            )
            if exec_res.exit_code == 0:
                return True
            time.sleep(interval)
        return False
