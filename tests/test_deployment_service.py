import tempfile
import unittest
from pathlib import Path

from forge.core.errors import ConcurrencyError, EntityNotFoundError
from forge.core.models import DeploymentStatus, EventKind
from forge.deployments.service import DeploymentFailedError, DeploymentService
from forge.proxy.fake import FakeProxy
from forge.runtime.fake import FakeRuntime
from forge.storage.db import Database


class TestDeploymentService(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.runtime = FakeRuntime()
        self.proxy = FakeProxy()
        self.service = DeploymentService(self.db, self.runtime, self.proxy)

        # Create dummy project directory with a Dockerfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_path = Path(self.temp_dir.name)
        (self.project_path / "Dockerfile").write_text("FROM alpine\nCMD echo ok\n", encoding="utf-8")

        # Create application in DB
        self.app = self.service.app_repo.create(
            name="test-service",
            domain="test-service.localhost",
            container_port=8080,
        )

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_successful_deployment_lifecycle_and_events(self) -> None:
        build_lines: list[str] = []
        dep = self.service.deploy(
            app_id=self.app.id,
            context_path=self.project_path,
            build_log_callback=build_lines.append,
        )

        self.assertEqual(dep.status, DeploymentStatus.ACTIVE)
        self.assertIsNotNone(dep.active_container_id)
        self.assertIn("Building", "".join(build_lines))

        # Check events recorded in SQLite
        events = self.service.event_repo.list_by_app(self.app.id)
        event_kinds = [e.event_kind for e in events]

        self.assertIn(EventKind.DEPLOYMENT_CREATED, event_kinds)
        self.assertIn(EventKind.DEPLOYMENT_BUILD_STARTED, event_kinds)
        self.assertIn(EventKind.DEPLOYMENT_BUILD_SUCCEEDED, event_kinds)
        self.assertIn(EventKind.DEPLOYMENT_CONTAINER_STARTED, event_kinds)
        self.assertIn(EventKind.DEPLOYMENT_HEALTH_CHECK_PASSED, event_kinds)
        self.assertIn(EventKind.DEPLOYMENT_PROMOTED, event_kinds)

    def test_blue_green_decommissions_old_version(self) -> None:
        # Deploy v1
        dep1 = self.service.deploy(app_id=self.app.id, context_path=self.project_path)
        self.assertEqual(dep1.status, DeploymentStatus.ACTIVE)
        cont1 = dep1.active_container_id
        assert cont1 is not None

        # Deploy v2
        dep2 = self.service.deploy(app_id=self.app.id, context_path=self.project_path)
        self.assertEqual(dep2.status, DeploymentStatus.ACTIVE)
        cont2 = dep2.active_container_id
        assert cont2 is not None

        self.assertNotEqual(cont1, cont2)

        # Check that old container was stopped with graceful shutdown timeout 10 and removed
        self.assertIn((cont1, 10), self.runtime.stopped_containers)
        self.assertIn(cont1, self.runtime.removed_containers)

        # Check that old deployment in DB is marked STOPPED
        updated_dep1 = self.service.dep_repo.get_by_id(dep1.id)
        self.assertIsNotNone(updated_dep1)
        assert updated_dep1 is not None
        self.assertEqual(updated_dep1.status, DeploymentStatus.STOPPED)

    def test_build_failure_transitions_to_failed(self) -> None:
        self.runtime.fail_build = True

        with self.assertRaises(DeploymentFailedError):
            self.service.deploy(app_id=self.app.id, context_path=self.project_path)

        deployments = self.service.dep_repo.list_by_app(self.app.id)
        self.assertEqual(len(deployments), 1)
        self.assertEqual(deployments[0].status, DeploymentStatus.FAILED)
        self.assertIn("build failed", str(deployments[0].error_message).lower())

    def test_startup_crash_cleans_candidate_and_preserves_active(self) -> None:
        # Deploy initial healthy app
        dep1 = self.service.deploy(app_id=self.app.id, context_path=self.project_path)
        initial_cont = dep1.active_container_id

        # Trigger startup failure on candidate
        self.runtime.fail_inspect_unhealthy = True

        with self.assertRaises(DeploymentFailedError):
            self.service.deploy(app_id=self.app.id, context_path=self.project_path)

        # Active deployment must remain dep1
        active = self.service.dep_repo.get_active_deployment(self.app.id)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.id, dep1.id)
        self.assertEqual(active.active_container_id, initial_cont)

    def test_health_check_failure_cleans_candidate_and_preserves_active(self) -> None:
        # Deploy initial healthy app
        dep1 = self.service.deploy(app_id=self.app.id, context_path=self.project_path)

        # Trigger health check failure
        self.runtime.fail_health_exec = True

        with self.assertRaises(DeploymentFailedError):
            self.service.deploy(
                app_id=self.app.id,
                context_path=self.project_path,
                health_check_timeout=0.2,
                health_check_interval=0.1,
            )

        active = self.service.dep_repo.get_active_deployment(self.app.id)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.id, dep1.id)

    def test_concurrency_lock_rejects_simultaneous_deployments(self) -> None:
        lock = self.service._get_app_lock(self.app.id)
        lock.acquire()
        try:
            with self.assertRaises(ConcurrencyError):
                self.service.deploy(app_id=self.app.id, context_path=self.project_path)
        finally:
            lock.release()

    def test_missing_app_raises_entity_not_found(self) -> None:
        with self.assertRaises(EntityNotFoundError):
            self.service.deploy(app_id="non-existent-app-id", context_path=self.project_path)

    def test_missing_dockerfile_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as empty_dir:
            with self.assertRaises(FileNotFoundError):
                self.service.deploy(app_id=self.app.id, context_path=Path(empty_dir))


if __name__ == "__main__":
    unittest.main()
