import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from forge.api.auth import ApiAuth
from forge.core.errors import (
    ConcurrencyError,
    EntityNotFoundError,
    ForgeError,
    PayloadTooLargeError,
    ValidationError,
)
from forge.core.models import DeploymentStatus
from forge.core.scrubber import scrub_text
from forge.deployments.service import DeploymentService
from forge.proxy.base import Proxy
from forge.runtime.base import Runtime
from forge.scheduler.queue import DeploymentQueue
from forge.scheduler.reconciler import Reconciler
from forge.storage.db import Database
from forge.storage.repository import (
    ApplicationRepository,
    DeploymentRepository,
    EnvironmentRepository,
    EventRepository,
    RevisionRepository,
)


class ForgeRequestHandler(BaseHTTPRequestHandler):
    # Default injected dependencies on the server class
    server: "ForgeThreadingServer"

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default stdout logging during tests / operations
        pass

    def _send_json(self, status_code: int, data: Any) -> None:
        response_bytes = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def _send_error(self, status_code: int, code: str, message: str) -> None:
        self._send_json(
            status_code,
            {
                "error": {
                    "code": code,
                    "message": message,
                }
            },
        )

    # Priority 5: Add strict request body size limits (10 MB maximum)
    MAX_BODY_SIZE = 10 * 1024 * 1024

    def _parse_json_body(self) -> dict[str, Any]:
        cl_header = self.headers.get("Content-Length")
        if cl_header is None:
            return {}
        try:
            content_length = int(cl_header)
        except ValueError:
            raise ValidationError("Invalid Content-Length header")

        if content_length < 0:
            raise ValidationError("Negative Content-Length header is forbidden")

        if content_length > self.MAX_BODY_SIZE:
            raise PayloadTooLargeError(
                f"Payload size ({content_length} bytes) exceeds maximum allowed limit of {self.MAX_BODY_SIZE} bytes"
            )

        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Malformed JSON request body: {exc}") from exc

    def _check_auth(self) -> bool:
        auth_header = self.headers.get("Authorization")
        if not self.server.auth.verify_token(auth_header):
            self._send_error(401, "unauthorized", "Invalid or missing Bearer API token")
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Health endpoint is public and unauthenticated
        if path == "/health":
            self._send_json(200, {"status": "ok", "version": "0.2.0"})
            return

        if not self._check_auth():
            return

        try:
            # GET /api/v1/applications
            if path == "/api/v1/applications":
                apps = self.server.app_repo.list_all()
                res = [
                    {
                        "id": a.id,
                        "name": a.name,
                        "domain": a.domain,
                        "container_port": a.container_port,
                        "created_at": a.created_at,
                        "updated_at": a.updated_at,
                    }
                    for a in apps
                ]
                self._send_json(200, res)
                return

            # GET /api/v1/applications/{id}
            match_app = re.match(r"^/api/v1/applications/([^/]+)$", path)
            if match_app:
                app_id = match_app.group(1)
                app = self.server.app_repo.get_by_id(app_id)
                if not app:
                    raise EntityNotFoundError(f"Application '{app_id}' not found")

                active_dep = self.server.dep_repo.get_active_deployment(app.id)
                # SEC-07: Masked env vars, zero plaintext leak
                masked_env = self.server.env_repo.get_masked_vars(app.id)
                data = {
                    "id": app.id,
                    "name": app.name,
                    "domain": app.domain,
                    "container_port": app.container_port,
                    "created_at": app.created_at,
                    "updated_at": app.updated_at,
                    "active_deployment": (
                        {
                            "id": active_dep.id,
                            "status": active_dep.status.value,
                            "active_container_id": active_dep.active_container_id,
                            "created_at": active_dep.created_at,
                        }
                        if active_dep
                        else None
                    ),
                    "env_vars": masked_env,
                }
                self._send_json(200, data)
                return

            # GET /api/v1/applications/{id}/deployments
            match_app_deps = re.match(r"^/api/v1/applications/([^/]+)/deployments$", path)
            if match_app_deps:
                app_id = match_app_deps.group(1)
                app = self.server.app_repo.get_by_id(app_id)
                if not app:
                    raise EntityNotFoundError(f"Application '{app_id}' not found")
                limit = int(query.get("limit", [50])[0])
                deps = self.server.dep_repo.list_by_app(app_id, limit=limit)
                res = [
                    {
                        "id": d.id,
                        "app_id": d.app_id,
                        "status": d.status.value,
                        "revision_id": d.revision_id,
                        "candidate_container_id": d.candidate_container_id,
                        "active_container_id": d.active_container_id,
                        "error_message": d.error_message,
                        "created_at": d.created_at,
                        "started_at": d.started_at,
                        "finished_at": d.finished_at,
                    }
                    for d in deps
                ]
                self._send_json(200, res)
                return

            # GET /api/v1/deployments/{id}
            match_dep = re.match(r"^/api/v1/deployments/([^/]+)$", path)
            if match_dep:
                dep_id = match_dep.group(1)
                dep = self.server.dep_repo.get_by_id(dep_id)
                if not dep:
                    raise EntityNotFoundError(f"Deployment '{dep_id}' not found")
                data = {
                    "id": dep.id,
                    "app_id": dep.app_id,
                    "status": dep.status.value,
                    "revision_id": dep.revision_id,
                    "candidate_container_id": dep.candidate_container_id,
                    "active_container_id": dep.active_container_id,
                    "error_message": dep.error_message,
                    "created_at": dep.created_at,
                    "started_at": dep.started_at,
                    "finished_at": dep.finished_at,
                }
                self._send_json(200, data)
                return

            # GET /api/v1/applications/{id}/events
            match_events = re.match(r"^/api/v1/applications/([^/]+)/events$", path)
            if match_events:
                app_id = match_events.group(1)
                app = self.server.app_repo.get_by_id(app_id)
                if not app:
                    raise EntityNotFoundError(f"Application '{app_id}' not found")
                limit = int(query.get("limit", [50])[0])
                events = self.server.event_repo.list_by_app(app_id, limit=limit)
                res = [
                    {
                        "id": e.id,
                        "app_id": e.app_id,
                        "deployment_id": e.deployment_id,
                        "event_kind": e.event_kind.value,
                        "payload": e.payload,
                        "created_at": e.created_at,
                    }
                    for e in events
                ]
                self._send_json(200, res)
                return

            # GET /api/v1/applications/{id}/logs
            match_logs = re.match(r"^/api/v1/applications/([^/]+)/logs$", path)
            if match_logs:
                app_id = match_logs.group(1)
                app = self.server.app_repo.get_by_id(app_id)
                if not app:
                    raise EntityNotFoundError(f"Application '{app_id}' not found")
                tail = int(query.get("tail", [100])[0])
                active_dep = self.server.dep_repo.get_active_deployment(app_id)
                if not active_dep or not active_dep.active_container_id:
                    self._send_json(200, {"app_id": app_id, "logs": ""})
                    return
                raw_logs = self.server.runtime.logs(active_dep.active_container_id, tail=tail)
                # Priority 7: Zero-leak logs - scrub all configured secrets and sensitive patterns
                app_env = self.server.env_repo.get_vars(app_id)
                scrubbed_logs = scrub_text(raw_logs, secrets=list(app_env.values()))
                self._send_json(
                    200,
                    {
                        "app_id": app_id,
                        "container_id": active_dep.active_container_id,
                        "logs": scrubbed_logs,
                    },
                )
                return

            self._send_error(404, "not_found", f"Endpoint '{path}' not found")
        except EntityNotFoundError as exc:
            self._send_error(404, "not_found", str(exc))
        except ValidationError as exc:
            self._send_error(400, "validation_error", str(exc))
        except Exception as exc:
            self._send_error(500, "internal_error", str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._check_auth():
            return

        try:
            body = self._parse_json_body()

            # POST /api/v1/applications
            if path == "/api/v1/applications":
                name = body.get("name")
                domain = body.get("domain")
                container_port = int(body.get("container_port", 8000))
                if not name or not domain:
                    raise ValidationError("Fields 'name' and 'domain' are required")

                app = self.server.app_repo.create(
                    name=name,
                    domain=domain,
                    container_port=container_port,
                )
                data = {
                    "id": app.id,
                    "name": app.name,
                    "domain": app.domain,
                    "container_port": app.container_port,
                    "created_at": app.created_at,
                    "updated_at": app.updated_at,
                }
                self._send_json(201, data)
                return

            # POST /api/v1/applications/{id}/deployments
            match_app_deploy = re.match(r"^/api/v1/applications/([^/]+)/deployments$", path)
            if match_app_deploy:
                app_id = match_app_deploy.group(1)
                context_path_raw = body.get("context_path")
                if not context_path_raw:
                    raise ValidationError("Field 'context_path' is required")
                context_path = Path(context_path_raw).resolve()
                if not context_path.exists():
                    raise ValidationError(f"Build context path does not exist: '{context_path}'")
                if not context_path.is_dir():
                    raise ValidationError(f"Build context path must be a directory: '{context_path}'")

                # Priority 4: Restrict deployment build context to an explicit workspace boundary.
                if self.server.workspace_boundary is not None:
                    allowed_boundary = self.server.workspace_boundary.resolve()
                    try:
                        context_path.relative_to(allowed_boundary)
                    except ValueError:
                        raise ValidationError(
                            f"Security violation: Build context path '{context_path}' is outside authorized workspace boundary '{allowed_boundary}'"
                        )

                health_check_path = body.get("health_check_path", "/health")
                health_check_timeout = float(body.get("health_check_timeout", 15.0))
                health_check_interval = float(body.get("health_check_interval", 0.5))

                dep, job_id = self.server.job_queue.submit_deployment(
                    app_id=app_id,
                    context_path=context_path,
                    health_check_path=health_check_path,
                    health_check_timeout=health_check_timeout,
                    health_check_interval=health_check_interval,
                )
                self._send_json(
                    202,
                    {
                        "deployment_id": dep.id,
                        "job_id": job_id,
                        "status": dep.status.value,
                        "app_id": app_id,
                    },
                )
                return

            # POST /api/v1/deployments/{id}/rollback
            match_rollback = re.match(r"^/api/v1/deployments/([^/]+)/rollback$", path)
            if match_rollback:
                dep_id = match_rollback.group(1)
                target_dep = self.server.dep_repo.get_by_id(dep_id)
                if not target_dep:
                    raise EntityNotFoundError(f"Deployment '{dep_id}' not found")

                rb_dep = self.server.deployment_service.rollback(
                    app_id=target_dep.app_id,
                    target_deployment_id=target_dep.id,
                )
                self._send_json(
                    200,
                    {
                        "deployment_id": rb_dep.id,
                        "app_id": rb_dep.app_id,
                        "status": rb_dep.status.value,
                        "active_container_id": rb_dep.active_container_id,
                        "message": "Rollback successful",
                    },
                )
                return

            self._send_error(404, "not_found", f"Endpoint '{path}' not found")
        except EntityNotFoundError as exc:
            self._send_error(404, "not_found", str(exc))
        except PayloadTooLargeError as exc:
            self._send_error(413, "payload_too_large", str(exc))
        except ValidationError as exc:
            self._send_error(400, "validation_error", str(exc))
        except ConcurrencyError as exc:
            self._send_error(409, "concurrency_error", str(exc))
        except FileNotFoundError as exc:
            self._send_error(400, "file_not_found", str(exc))
        except ForgeError as exc:
            self._send_error(400, "forge_error", str(exc))
        except Exception as exc:
            self._send_error(500, "internal_error", str(exc))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._check_auth():
            return

        try:
            match_app = re.match(r"^/api/v1/applications/([^/]+)$", path)
            if match_app:
                app_id = match_app.group(1)
                deleted = self.server.app_repo.delete(app_id)
                if not deleted:
                    raise EntityNotFoundError(f"Application '{app_id}' not found")
                self._send_json(200, {"deleted": True, "id": app_id})
                return

            self._send_error(404, "not_found", f"Endpoint '{path}' not found")
        except EntityNotFoundError as exc:
            self._send_error(404, "not_found", str(exc))
        except Exception as exc:
            self._send_error(500, "internal_error", str(exc))


class ForgeThreadingServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        db: Database,
        runtime: Runtime,
        proxy: Proxy,
        api_token: str | None = None,
        reconcile_interval: float = 10.0,
        workspace_boundary: Path | str | None = None,
    ) -> None:
        # SEC-04: Ensure server binds strictly to localhost unless overridden
        host, _ = server_address
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValidationError(
                f"Security violation: Forge server must bind to localhost (127.0.0.1), got '{host}'"
            )

        super().__init__(server_address, ForgeRequestHandler)
        self.db = db
        self.runtime = runtime
        self.proxy = proxy
        self.auth = ApiAuth(api_token)
        self.workspace_boundary = Path(workspace_boundary).resolve() if workspace_boundary is not None else None

        self.app_repo = ApplicationRepository(db)
        self.dep_repo = DeploymentRepository(db)
        self.event_repo = EventRepository(db)
        self.env_repo = EnvironmentRepository(db)
        self.rev_repo = RevisionRepository(db)

        self.deployment_service = DeploymentService(db, runtime, proxy)
        self.job_queue = DeploymentQueue(db, self.deployment_service)
        self.reconciler = Reconciler(
            db=db,
            runtime=runtime,
            deployment_service=self.deployment_service,
            job_queue=self.job_queue,
            interval=reconcile_interval,
        )


class ForgeApiServer:
    """High-level daemon wrapper for Forge Control Plane server."""

    def __init__(
        self,
        db: Database,
        runtime: Runtime,
        proxy: Proxy,
        host: str = "127.0.0.1",
        port: int = 8000,
        api_token: str | None = None,
        reconcile_interval: float = 10.0,
        workspace_boundary: Path | str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.db = db
        self.runtime = runtime
        self.proxy = proxy
        self.api_token = api_token
        self.reconcile_interval = reconcile_interval
        self.workspace_boundary = Path(workspace_boundary).resolve() if workspace_boundary is not None else None

        self._server: ForgeThreadingServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def server_port(self) -> int:
        if self._server:
            return self._server.server_address[1]
        return self.port

    @property
    def server_address(self) -> tuple[str, int]:
        if self._server:
            return (self._server.server_address[0], self._server.server_address[1])
        return (self.host, self.port)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return

        self._server = ForgeThreadingServer(
            server_address=(self.host, self.port),
            db=self.db,
            runtime=self.runtime,
            proxy=self.proxy,
            api_token=self.api_token,
            reconcile_interval=self.reconcile_interval,
            workspace_boundary=self.workspace_boundary,
        )

        # Start background workers
        self._server.job_queue.start()
        self._server.reconciler.start()

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="ForgeApiServerThread",
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server.job_queue.stop(timeout=2.0)
            self._server.reconciler.stop(timeout=2.0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None
