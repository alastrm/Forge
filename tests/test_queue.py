import tempfile
import time
import unittest
from pathlib import Path

from forge.core.errors import ConcurrencyError
from forge.core.models import DeploymentStatus, EventKind
from forge.deployments.service import DeploymentService
from forge.proxy.fake import FakeProxy
from forge.runtime.fake import FakeRuntime
from forge.scheduler.queue import DeploymentJob, DeploymentQueue
from forge.storage.db import Database


class TestDeploymentQueue(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.runtime = FakeRuntime()
        self.proxy = FakeProxy()
        self.service = DeploymentService(self.db, self.runtime, self.proxy)
        self.queue = DeploymentQueue(self.db, self.service, max_workers=1)

        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_path = Path(self.temp_dir.name)
        (self.project_path / "Dockerfile").write_text("FROM alpine\nCMD echo ok\n", encoding="utf-8")

        self.app = self.service.app_repo.create(
            name="queue-app",
            domain="queue-app.localhost",
            container_port=8080,
        )

    def tearDown(self) -> None:
        if self.queue.is_running:
            self.queue.stop(timeout=1.0)
        self.db.close()
        self.temp_dir.cleanup()

    def test_queue_executes_deployment_asynchronously(self) -> None:
        self.queue.start()
        self.assertTrue(self.queue.is_running)

        dep, job_id = self.queue.submit_deployment(
            app_id=self.app.id,
            context_path=self.project_path,
        )

        self.assertEqual(dep.status, DeploymentStatus.PENDING)
        self.assertTrue(self.queue.is_app_busy(self.app.id))

        # Wait for worker to finish
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and self.queue.is_app_busy(self.app.id):
            time.sleep(0.05)

        self.assertFalse(self.queue.is_app_busy(self.app.id))

        updated_dep = self.service.dep_repo.get_by_id(dep.id)
        self.assertIsNotNone(updated_dep)
        assert updated_dep is not None
        self.assertEqual(updated_dep.status, DeploymentStatus.ACTIVE)
        self.assertIsNotNone(updated_dep.active_container_id)

        # Check events
        events = self.service.event_repo.list_by_app(self.app.id)
        event_kinds = [e.event_kind for e in events]
        self.assertIn(EventKind.DEPLOYMENT_CREATED, event_kinds)
        self.assertIn(EventKind.DEPLOYMENT_PROMOTED, event_kinds)

    def test_queue_concurrency_protection(self) -> None:
        # Enqueue first job without starting worker
        dep1 = self.service.create_deployment(self.app.id)
        job1 = DeploymentJob(deployment_id=dep1.id, app_id=self.app.id, context_path=self.project_path)
        self.queue.enqueue(job1)

        self.assertTrue(self.queue.is_app_busy(self.app.id))

        # Second enqueue for same app must be rejected immediately
        with self.assertRaises(ConcurrencyError):
            self.queue.submit_deployment(app_id=self.app.id, context_path=self.project_path)

    def test_queue_handles_worker_failure(self) -> None:
        self.runtime.fail_build = True
        self.queue.start()

        dep, _ = self.queue.submit_deployment(
            app_id=self.app.id,
            context_path=self.project_path,
        )

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and self.queue.is_app_busy(self.app.id):
            time.sleep(0.05)

        self.assertFalse(self.queue.is_app_busy(self.app.id))

        updated_dep = self.service.dep_repo.get_by_id(dep.id)
        self.assertIsNotNone(updated_dep)
        assert updated_dep is not None
        self.assertEqual(updated_dep.status, DeploymentStatus.FAILED)
        self.assertIn("build failed", str(updated_dep.error_message).lower())

    def test_queue_start_and_stop(self) -> None:
        self.assertFalse(self.queue.is_running)
        self.queue.start()
        self.assertTrue(self.queue.is_running)
        self.queue.stop(timeout=1.0)
        self.assertFalse(self.queue.is_running)
