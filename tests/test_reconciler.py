import tempfile
import time
import unittest
from pathlib import Path

from forge.core.models import DeploymentStatus, EventKind
from forge.deployments.service import DeploymentService
from forge.proxy.fake import FakeProxy
from forge.runtime.fake import FakeRuntime
from forge.scheduler.queue import DeploymentJob, DeploymentQueue
from forge.scheduler.reconciler import Reconciler
from forge.storage.db import Database


class TestReconciler(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.runtime = FakeRuntime()
        self.proxy = FakeProxy()
        self.service = DeploymentService(self.db, self.runtime, self.proxy)
        self.queue = DeploymentQueue(self.db, self.service)
        self.reconciler = Reconciler(
            db=self.db,
            runtime=self.runtime,
            deployment_service=self.service,
            job_queue=self.queue,
            interval=0.1,
        )

        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_path = Path(self.temp_dir.name)
        (self.project_path / "Dockerfile").write_text("FROM alpine\nCMD echo ok\n", encoding="utf-8")

        self.app = self.service.app_repo.create(
            name="reconciler-app",
            domain="reconciler-app.localhost",
            container_port=8080,
        )

    def tearDown(self) -> None:
        if self.reconciler.is_running:
            self.reconciler.stop(timeout=1.0)
        if self.queue.is_running:
            self.queue.stop(timeout=1.0)
        self.db.close()
        self.temp_dir.cleanup()

    def test_reconciler_fixes_interrupted_deployment_after_restart(self) -> None:
        # Simulate Forge process crash while a deployment was in STARTING
        dep = self.service.dep_repo.create(self.app.id, status=DeploymentStatus.PENDING)
        dep = self.service.dep_repo.update_status(dep.id, DeploymentStatus.BUILDING)
        dep = self.service.dep_repo.update_status(
            dep.id,
            DeploymentStatus.STARTING,
            candidate_container_id="stale-candidate-1",
        )

        # Candidate container was created in runtime before crash
        self.runtime.containers["stale-candidate-1"] = {
            "name": "stale-candidate-1",
            "running": True,
            "status": "running",
            "exit_code": 0,
            "labels": {"forge.managed": "true"},
        }

        # Run reconciliation pass (simulating server restart)
        report = self.reconciler.reconcile_once()

        self.assertIn(dep.id, report.interrupted_deployments_fixed)
        self.assertIn("stale-candidate-1", self.runtime.removed_containers)

        updated_dep = self.service.dep_repo.get_by_id(dep.id)
        self.assertIsNotNone(updated_dep)
        assert updated_dep is not None
        self.assertEqual(updated_dep.status, DeploymentStatus.FAILED)
        self.assertIn("interrupted by server restart", str(updated_dep.error_message).lower())

        events = self.service.event_repo.list_by_app(self.app.id)
        event_kinds = [e.event_kind for e in events]
        self.assertIn(EventKind.DEPLOYMENT_FAILED, event_kinds)

    def test_reconciler_does_not_interfere_with_in_flight_job(self) -> None:
        # Create deployment and mark it in-flight in queue
        dep = self.service.dep_repo.create(self.app.id, status=DeploymentStatus.PENDING)
        dep = self.service.dep_repo.update_status(dep.id, DeploymentStatus.BUILDING)

        job = DeploymentJob(deployment_id=dep.id, app_id=self.app.id, context_path=self.project_path)
        with self.queue._active_mutex:
            self.queue._active_jobs[self.app.id] = job

        report = self.reconciler.reconcile_once()

        # Should not touch the in-flight deployment
        self.assertEqual(len(report.interrupted_deployments_fixed), 0)
        updated_dep = self.service.dep_repo.get_by_id(dep.id)
        assert updated_dep is not None
        self.assertEqual(updated_dep.status, DeploymentStatus.BUILDING)

    def test_reconciler_detects_dead_active_container(self) -> None:
        # Deploy healthy application
        dep = self.service.deploy(app_id=self.app.id, context_path=self.project_path)
        active_cont = dep.active_container_id
        assert active_cont is not None

        # Simulate container crash in Docker
        self.runtime.containers[active_cont]["running"] = False
        self.runtime.containers[active_cont]["exit_code"] = 137

        report = self.reconciler.reconcile_once()

        self.assertIn(active_cont, report.dead_containers_detected)
        updated_dep = self.service.dep_repo.get_by_id(dep.id)
        assert updated_dep is not None
        self.assertEqual(updated_dep.status, DeploymentStatus.FAILED)
        self.assertIn("crashed or stopped", str(updated_dep.error_message).lower())

        events = self.service.event_repo.list_by_app(self.app.id)
        event_kinds = [e.event_kind for e in events]
        self.assertIn(EventKind.CONTAINER_CRASHED, event_kinds)
        self.assertIn(EventKind.DEPLOYMENT_FAILED, event_kinds)

    def test_reconciler_cleans_orphaned_containers(self) -> None:
        # Deploy healthy application
        dep = self.service.deploy(app_id=self.app.id, context_path=self.project_path)
        live_cont = dep.active_container_id
        assert live_cont is not None

        # Create an orphaned Forge-managed container
        orphan_name = f"{self.app.name}-stale-orphan-abc"
        self.runtime.containers[orphan_name] = {
            "name": orphan_name,
            "running": True,
            "status": "running",
            "exit_code": 0,
            "labels": {"forge.managed": "true"},
        }

        report = self.reconciler.reconcile_once()

        self.assertIn(orphan_name, report.orphans_removed)
        self.assertIn(orphan_name, self.runtime.removed_containers)
        self.assertNotIn(orphan_name, self.runtime.containers)
        # Expected container remains untouched
        self.assertIn(live_cont, self.runtime.containers)

        events = self.service.event_repo.list_by_app(self.app.id)
        event_kinds = [e.event_kind for e in events]
        self.assertIn(EventKind.CONTAINER_ORPHAN_CLEANED, event_kinds)

    def test_reconciler_background_loop_and_graceful_stop(self) -> None:
        self.assertFalse(self.reconciler.is_running)
        self.reconciler.start()
        self.assertTrue(self.reconciler.is_running)

        # Allow loop to execute at least once
        time.sleep(0.15)

        self.reconciler.stop(timeout=1.0)
        self.assertFalse(self.reconciler.is_running)
