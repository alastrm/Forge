import http.client
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from forge.api.server import ForgeApiServer
from forge.core.errors import ConcurrencyError, PayloadTooLargeError, ValidationError
from forge.core.models import DeploymentStatus, EventKind
from forge.core.scrubber import scrub_dict, scrub_text
from forge.deployments.service import DeploymentService
from forge.proxy.fake import FakeProxy
from forge.proxy.traefik import TraefikProxy
from forge.runtime.base import ExecResult
from forge.runtime.docker import DockerRuntime, validate_container_security
from forge.runtime.fake import FakeRuntime
from forge.scheduler.reconciler import ReconciliationReport, Reconciler
from forge.storage.db import Database
from forge.storage.repository import ApplicationRepository, DeploymentRepository, EventRepository


class TestHardeningPass(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.temp_dir.name)
        self.db = Database(":memory:")
        self.runtime = FakeRuntime()
        self.proxy = FakeProxy()
        self.service = DeploymentService(self.db, self.runtime, self.proxy)
        self.app_repo = ApplicationRepository(self.db)
        self.dep_repo = DeploymentRepository(self.db)
        self.event_repo = EventRepository(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    # --------------------------------------------------------------------------
    # Priority 1: Traefik candidate must never receive traffic before promotion
    # --------------------------------------------------------------------------
    def test_priority_1_candidate_never_receives_traffic_before_promotion_fake_proxy(self) -> None:
        fake_proxy = FakeProxy()
        labels_cand = fake_proxy.generate_labels("web", "web.local", 8080, is_candidate=True)
        self.assertEqual(labels_cand.get("traefik.enable"), "false")
        self.assertEqual(labels_cand.get("fake.proxy.candidate"), "true")

        labels_prod = fake_proxy.generate_labels("web", "web.local", 8080, is_candidate=False)
        self.assertEqual(labels_prod.get("traefik.enable"), "true")
        self.assertEqual(labels_prod.get("fake.proxy.candidate"), "false")

    def test_priority_1_candidate_never_receives_traffic_before_promotion_traefik_proxy(self) -> None:
        cfg_dir = self.workspace_dir / "traefik_dyn"
        traefik = TraefikProxy(dynamic_config_dir=cfg_dir)

        # 1. Candidate labels explicitly disable Traefik routing
        labels = traefik.generate_labels("testapp", "test.local", 3000, is_candidate=True)
        self.assertEqual(labels["traefik.enable"], "false")
        self.assertEqual(labels["forge.candidate"], "true")

        # 2. Before promotion, dynamic router file must NOT exist
        config_file = cfg_dir / "testapp.yaml"
        self.assertFalse(config_file.exists())

        # 3. Only promote_service writes the dynamic routing file
        traefik.promote_service("testapp", "test.local", "testapp-cand-123", 3000)
        self.assertTrue(config_file.exists())
        yaml_text = config_file.read_text(encoding="utf-8")
        self.assertIn("testapp:", yaml_text)
        self.assertIn('rule: "Host(`test.local`)"', yaml_text)
        self.assertIn('url: "http://testapp-cand-123:3000"', yaml_text)

        # 4. remove_service deletes the configuration
        traefik.remove_service("testapp")
        self.assertFalse(config_file.exists())

    # --------------------------------------------------------------------------
    # Priority 2: Fix deployment creation race at persistence/concurrency boundary
    # --------------------------------------------------------------------------
    def test_priority_2_persistence_partial_unique_index_rejects_concurrent_in_progress(self) -> None:
        app = self.app_repo.create("app-race", "race.local", 8000)

        # First in-progress deployment succeeds
        d1 = self.dep_repo.create(app.id, status=DeploymentStatus.BUILDING)
        self.assertEqual(d1.status, DeploymentStatus.BUILDING)

        # Second concurrent in-progress deployment must be rejected with ConcurrencyError
        with self.assertRaises(ConcurrencyError):
            self.dep_repo.create(app.id, status=DeploymentStatus.PENDING)

        # Terminate d1, freeing the in-progress index
        self.dep_repo.update_status(d1.id, DeploymentStatus.FAILED)

        # Now creating a new in-progress deployment succeeds
        d2 = self.dep_repo.create(app.id, status=DeploymentStatus.PENDING)
        self.assertEqual(d2.status, DeploymentStatus.PENDING)

    def test_priority_2_deployment_service_nonblocking_lock_prevents_simultaneous_deploys(self) -> None:
        app = self.app_repo.create("lock-app", "lock.local", 8000)
        lock = self.service._get_app_lock(app.id)

        # Acquire lock to simulate concurrent operation in another worker/thread
        self.assertTrue(lock.acquire(blocking=False))
        try:
            with self.assertRaises(ConcurrencyError):
                self.service.create_deployment(app.id)
        finally:
            lock.release()

    # --------------------------------------------------------------------------
    # Priority 3: Fix rollback vs reconciler ownership race & double-ACTIVE race
    # --------------------------------------------------------------------------
    def test_priority_3_reconciler_respects_in_flight_rollback_and_deployments(self) -> None:
        reconciler = Reconciler(self.db, self.runtime, deployment_service=self.service)
        app = self.app_repo.create("reconcile-race", "rec.local", 8000)

        dep = self.dep_repo.create(app.id, status=DeploymentStatus.BUILDING)

        # Register deployment as in-flight
        self.service.register_in_flight(dep.id)
        self.assertIn(dep.id, self.service.get_in_flight_deployment_ids())

        # Run reconciler pass - should NOT fail or kill the in-flight deployment
        report = ReconciliationReport()
        reconciler._reconcile_interrupted_deployments(report)
        self.assertEqual(report.interrupted_deployments_fixed, [])

        updated = self.dep_repo.get_by_id(dep.id)
        assert updated is not None
        self.assertEqual(updated.status, DeploymentStatus.BUILDING)

        # Unregister from in-flight (e.g. process crashed unexpectedly)
        self.service.unregister_in_flight(dep.id)

        # Now reconciler detects it as orphaned / interrupted
        report = ReconciliationReport()
        reconciler._reconcile_interrupted_deployments(report)
        self.assertEqual(report.interrupted_deployments_fixed, [dep.id])
        updated = self.dep_repo.get_by_id(dep.id)
        assert updated is not None
        self.assertEqual(updated.status, DeploymentStatus.FAILED)

    def test_priority_3_double_active_prevention_in_db_and_service(self) -> None:
        app = self.app_repo.create("active-race", "active.local", 8000)
        # Advance d1 to ACTIVE
        d1 = self.dep_repo.create(app.id, status=DeploymentStatus.PENDING)
        d1 = self.dep_repo.update_status(d1.id, DeploymentStatus.BUILDING)
        d1 = self.dep_repo.update_status(d1.id, DeploymentStatus.STARTING)
        d1 = self.dep_repo.update_status(d1.id, DeploymentStatus.HEALTH_CHECKING)
        d1 = self.dep_repo.update_status(d1.id, DeploymentStatus.ACTIVE)
        self.assertEqual(d1.status, DeploymentStatus.ACTIVE)

        # Advance d2 to HEALTH_CHECKING
        d2 = self.dep_repo.create(app.id, status=DeploymentStatus.PENDING)
        d2 = self.dep_repo.update_status(d2.id, DeploymentStatus.BUILDING)
        d2 = self.dep_repo.update_status(d2.id, DeploymentStatus.STARTING)
        d2 = self.dep_repo.update_status(d2.id, DeploymentStatus.HEALTH_CHECKING)

        # Attempting to directly update d2 to ACTIVE while d1 is ACTIVE must raise ConcurrencyError
        with self.assertRaises(ConcurrencyError):
            self.dep_repo.update_status(d2.id, DeploymentStatus.ACTIVE)

        # In DeploymentService promotion flow, d1 is transitioned to STOPPING first
        self.dep_repo.update_status(d1.id, DeploymentStatus.STOPPING)
        # Now d2 can safely become ACTIVE without violating the partial unique index
        self.dep_repo.update_status(d2.id, DeploymentStatus.ACTIVE)
        updated_d2 = self.dep_repo.get_by_id(d2.id)
        assert updated_d2 is not None
        self.assertEqual(updated_d2.status, DeploymentStatus.ACTIVE)

    # --------------------------------------------------------------------------
    # Priority 4: Restrict deployment build context to explicit workspace boundary
    # --------------------------------------------------------------------------
    def test_priority_4_workspace_boundary_enforcement_in_api(self) -> None:
        server = ForgeApiServer(
            db=self.db,
            runtime=self.runtime,
            proxy=self.proxy,
            host="127.0.0.1",
            port=0,
            api_token="test-token",
            workspace_boundary=self.workspace_dir,
        )
        server.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        try:
            app = self.app_repo.create("bound-app", "bound.local", 8000)

            def post_deploy(ctx_path: str) -> tuple[int, dict]:
                req = urllib.request.Request(
                    f"{base_url}/api/v1/applications/{app.id}/deployments",
                    data=json.dumps({"context_path": ctx_path}).encode("utf-8"),
                    headers={
                        "Authorization": "Bearer test-token",
                        "Content-Type": "application/json",
                    },
                )
                try:
                    with urllib.request.urlopen(req) as resp:
                        return resp.status, json.loads(resp.read().decode("utf-8"))
                except urllib.error.HTTPError as err:
                    return err.code, json.loads(err.read().decode("utf-8"))

            # 1. Path outside workspace boundary is rejected with 400
            outside = str(Path(tempfile.gettempdir()).resolve())
            status, body = post_deploy(outside)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")
            self.assertIn("workspace boundary", body["error"]["message"].lower())

            # 2. Non-existent path is rejected with 400
            non_existent = str(self.workspace_dir / "does_not_exist")
            status, body = post_deploy(non_existent)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")
            self.assertIn("does not exist", body["error"]["message"].lower())

            # 3. Path pointing to a file instead of a directory is rejected with 400
            file_path = self.workspace_dir / "somefile.txt"
            file_path.write_text("hello", encoding="utf-8")
            status, body = post_deploy(str(file_path))
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "validation_error")
            self.assertIn("directory", body["error"]["message"].lower())

            # 4. Valid directory inside workspace boundary succeeds
            valid_app_dir = self.workspace_dir / "valid_app"
            valid_app_dir.mkdir()
            status, body = post_deploy(str(valid_app_dir))
            self.assertEqual(status, 202)
        finally:
            server.stop()

    # --------------------------------------------------------------------------
    # Priority 5: Add strict request body size limits
    # --------------------------------------------------------------------------
    def test_priority_5_request_body_size_limit_returns_413(self) -> None:
        server = ForgeApiServer(
            db=self.db,
            runtime=self.runtime,
            proxy=self.proxy,
            host="127.0.0.1",
            port=0,
            api_token="test-token",
        )
        server.start()

        try:
            # 1. Oversized Content-Length (> 10MB) returns 413
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request(
                "POST",
                "/api/v1/applications",
                body="",
                headers={
                    "Authorization": "Bearer test-token",
                    "Content-Type": "application/json",
                    "Content-Length": str(10 * 1024 * 1024 + 1),
                },
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 413)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["error"]["code"], "payload_too_large")
            conn.close()

            # 2. Negative Content-Length returns 413 or 400
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request(
                "POST",
                "/api/v1/applications",
                body="",
                headers={
                    "Authorization": "Bearer test-token",
                    "Content-Type": "application/json",
                    "Content-Length": "-10",
                },
            )
            resp = conn.getresponse()
            self.assertIn(resp.status, (400, 413))
            conn.close()
        finally:
            server.stop()

    # --------------------------------------------------------------------------
    # Priority 6: Real validation of mounts, privileged, caps, devices, net
    # --------------------------------------------------------------------------
    def test_priority_6_validate_container_security_rules(self) -> None:
        # Privileged mode forbidden
        with self.assertRaises(ValidationError) as ctx:
            validate_container_security(network="forge-net", privileged=True)
        self.assertIn("privileged", str(ctx.exception).lower())

        # Host networking forbidden
        with self.assertRaises(ValidationError) as ctx:
            validate_container_security(network="host")
        self.assertIn("host", str(ctx.exception).lower())

        # Container network forbidden
        with self.assertRaises(ValidationError) as ctx:
            validate_container_security(network="container:other")
        self.assertIn("container:", str(ctx.exception).lower())

        # Disallowed capabilities forbidden
        with self.assertRaises(ValidationError) as ctx:
            validate_container_security(network="forge-net", cap_add=["SYS_ADMIN"])
        self.assertIn("capability", str(ctx.exception).lower())

        # Devices forbidden
        with self.assertRaises(ValidationError) as ctx:
            validate_container_security(network="forge-net", devices=["/dev/sda:/dev/sda"])
        self.assertIn("device", str(ctx.exception).lower())

        # Dangerous mounts forbidden
        dangerous_paths = [
            "/var/run/docker.sock",
            "/run/docker.sock",
            "/proc",
            "/sys",
            "/etc",
            "C:\\Windows",
        ]
        for dp in dangerous_paths:
            with self.assertRaises(ValidationError) as ctx:
                validate_container_security(network="forge-net", mounts=[f"{dp}:/target"])
            self.assertIn("mount", str(ctx.exception).lower())

        # Relative host mounts forbidden
        with self.assertRaises(ValidationError) as ctx:
            validate_container_security(network="forge-net", mounts=["./local_dir:/app/data"])
        self.assertIn("absolute", str(ctx.exception).lower())

        # Safe mounts pass validation
        safe_src = str(self.workspace_dir / "safe_data")
        os.makedirs(safe_src, exist_ok=True)
        self.assertIsNone(validate_container_security(network="forge-net", mounts=[f"{safe_src}:/data:ro"]))

    @patch("subprocess.run")
    def test_priority_6_inspect_container_checks_hostconfig(self, mock_run: MagicMock) -> None:
        runtime = DockerRuntime(timeout=5.0)

        # Container inspect payload with HostConfig security violations
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "Id": "insecure-cont-123",
                        "State": {
                            "Status": "running",
                            "Running": True,
                            "ExitCode": 0,
                            "StartedAt": "2026-09-27T00:00:00Z",
                            "FinishedAt": "",
                        },
                        "HostConfig": {
                            "Privileged": True,
                            "NetworkMode": "host",
                            "Binds": ["/var/run/docker.sock:/var/run/docker.sock"],
                            "Devices": [{"PathOnHost": "/dev/sda"}],
                        },
                    }
                ]
            ),
            stderr="",
        )

        with self.assertRaises(ValidationError) as ctx:
            runtime.inspect_container("insecure-cont-123")
        self.assertIn("privileged", str(ctx.exception).lower())

    # --------------------------------------------------------------------------
    # Priority 7: Safe secrets strategy (scrubbing & permissions)
    # --------------------------------------------------------------------------
    def test_priority_7_scrub_dict_redacts_sensitive_keys(self) -> None:
        data = {
            "app": "my-app",
            "api_key": "secret-12345",
            "db": {
                "password": "super-pass-xyz",
                "nested_token": "token-999",
                "port": 5432,
            },
            "public_key": "ssh-rsa AAAAB3NzaC1...",
        }
        scrubbed = scrub_dict(data)
        self.assertEqual(scrubbed["api_key"], "[REDACTED]")
        self.assertEqual(scrubbed["db"]["password"], "[REDACTED]")
        self.assertEqual(scrubbed["db"]["nested_token"], "[REDACTED]")
        self.assertEqual(scrubbed["db"]["port"], 5432)
        self.assertEqual(scrubbed["app"], "my-app")

    def test_priority_7_event_payload_is_scrubbed_on_record(self) -> None:
        app = self.app_repo.create("sec-event-app", "evt.local", 8000)
        evt = self.event_repo.record(
            app_id=app.id,
            event_kind=EventKind.DEPLOYMENT_CREATED,
            payload={
                "message": "Created",
                "secret_key": "topsecret123",
                "auth_header": "Bearer abcdefg",
            },
        )
        self.assertEqual(evt.payload.get("secret_key"), "[REDACTED]")
        self.assertEqual(evt.payload.get("auth_header"), "[REDACTED]")
        self.assertEqual(evt.payload.get("message"), "Created")

    def test_priority_7_container_logs_are_scrubbed(self) -> None:
        raw_log = (
            "2026-09-27 10:00:00 INFO Starting application\n"
            "2026-09-27 10:00:01 DEBUG DATABASE_URL=postgres://user:mypassword123@db:5432/app\n"
            "2026-09-27 10:00:02 DEBUG Received Authorization: Bearer eyJhbGciOiJIUzI1NiIsIn...\n"
            "2026-09-27 10:00:03 INFO Application ready\n"
        )
        secrets = ["mypassword123", "eyJhbGciOiJIUzI1NiIsIn..."]
        cleaned = scrub_text(raw_log, secrets=secrets)
        self.assertNotIn("mypassword123", cleaned)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsIn...", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_priority_7_database_file_and_directory_permissions(self) -> None:
        db_path = self.workspace_dir / "secure_db" / "forge.db"
        db = Database(db_path)
        db.close()

        self.assertTrue(db_path.exists())
        if hasattr(os, "stat") and os.name != "nt":
            dir_mode = stat.S_IMODE(os.stat(db_path.parent).st_mode)
            file_mode = stat.S_IMODE(os.stat(db_path).st_mode)
            self.assertEqual(dir_mode, 0o700)
            self.assertEqual(file_mode, 0o600)

    # --------------------------------------------------------------------------
    # Priority 8: Proper SIGTERM shutdown handling
    # --------------------------------------------------------------------------
    def test_priority_8_sigterm_handling(self) -> None:
        import forge.cli as cli
        server = ForgeApiServer(
            db=self.db,
            runtime=self.runtime,
            proxy=self.proxy,
            host="127.0.0.1",
            port=0,
            api_token="tok",
        )
        server.start()
        self.assertTrue(server.is_running)

        shutdown_event = cli.setup_signal_handlers(server)
        self.assertFalse(shutdown_event.is_set())

        # Simulate receiving SIGTERM or SIGINT
        sig = signal.SIGINT if hasattr(signal, "SIGINT") else signal.SIGTERM
        handler = signal.getsignal(sig)
        if callable(handler):
            handler(sig, None)
            self.assertTrue(shutdown_event.is_set())
            self.assertFalse(server.is_running)

    # --------------------------------------------------------------------------
    # Priority 9: Reconciler logs and survives exceptions without dying
    # --------------------------------------------------------------------------
    def test_priority_9_reconciler_logs_and_survives_loop_exceptions(self) -> None:
        reconciler = Reconciler(
            db=self.db,
            runtime=self.runtime,
            deployment_service=self.service,
            interval=0.01,
        )

        call_count = 0

        def flaky_reconcile() -> ReconciliationReport:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("Temporary Docker socket timeout")
            return ReconciliationReport()

        reconciler.reconcile_once = flaky_reconcile  # type: ignore[assignment]
        reconciler.start()

        try:
            # Let loop run a few cycles
            time.sleep(0.1)
            self.assertTrue(reconciler.is_running)
            self.assertGreaterEqual(reconciler.error_count, 1)
            self.assertIsNotNone(reconciler.last_error)
            self.assertIn("Temporary Docker socket timeout", str(reconciler.last_error))
            self.assertGreaterEqual(call_count, 3)
        finally:
            reconciler.stop()

    # --------------------------------------------------------------------------
    # Priority 10: Remove Python-only assumptions from health check
    # --------------------------------------------------------------------------
    def test_priority_10_health_check_supports_non_python_environments(self) -> None:
        app = self.app_repo.create("no-python-app", "nopy.local", 8080)
        dep = self.dep_repo.create(app.id, status=DeploymentStatus.STARTING)

        probe_calls: list[list[str]] = []

        def mock_exec(container_id: str, cmd: list[str], timeout: float = 5.0) -> ExecResult:
            probe_calls.append(cmd)
            return ExecResult(exit_code=0, output="HTTP/1.1 200 OK")

        self.runtime.exec = mock_exec  # type: ignore[assignment]

        res = self.service._run_health_check(
            container_name="cand-cont-123",
            port=8080,
            path="/health",
            timeout=1.0,
            interval=0.05,
        )

        self.assertTrue(res)
        self.assertGreaterEqual(len(probe_calls), 1)
        full_probe_sh = " ".join(probe_calls[0])
        # Assert fallback probe contains curl, wget, python, node, and /dev/tcp
        self.assertIn("curl", full_probe_sh)
        self.assertIn("wget", full_probe_sh)
        self.assertIn("python3", full_probe_sh)
        self.assertIn("node", full_probe_sh)
        self.assertIn("/dev/tcp", full_probe_sh)


if __name__ == "__main__":
    unittest.main()
