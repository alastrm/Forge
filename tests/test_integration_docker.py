import shutil
import subprocess
import time
import unittest
from pathlib import Path

from forge.core.models import DeploymentStatus, EventKind
from forge.deployments.service import DeploymentService
from forge.proxy.fake import FakeProxy
from forge.runtime.docker import DockerRuntime
from forge.scheduler.reconciler import Reconciler
from forge.storage.db import Database


def is_docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=5.0)
        return res.returncode == 0
    except Exception:
        return False


@unittest.skipUnless(is_docker_available(), "Docker daemon is not running or available")
class TestDockerIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.runtime = DockerRuntime(timeout=60.0)
        self.proxy = FakeProxy()
        self.service = DeploymentService(self.db, self.runtime, self.proxy)
        self.reconciler = Reconciler(
            db=self.db,
            runtime=self.runtime,
            deployment_service=self.service,
        )

        self.project_path = Path("test-app").resolve()
        self.app = self.service.app_repo.create(
            name="docker-integ-app",
            domain="docker-integ.localhost",
            container_port=8000,
        )
        self.deployed_containers: list[str] = []

    def tearDown(self) -> None:
        # Clean up any leftover test containers
        for cont_name in self.deployed_containers:
            try:
                self.runtime.remove_container(cont_name, force=True)
            except Exception:
                pass
        self.db.close()

    def test_real_docker_deployment_and_reconciliation(self) -> None:
        # 1. Deploy real application to Docker
        dep = self.service.deploy(
            app_id=self.app.id,
            context_path=self.project_path,
            health_check_path="/health",
            health_check_timeout=30.0,
            health_check_interval=1.0,
        )

        self.assertEqual(dep.status, DeploymentStatus.ACTIVE)
        active_cont = dep.active_container_id
        self.assertIsNotNone(active_cont)
        assert active_cont is not None
        self.deployed_containers.append(active_cont)

        # 2. Inspect container directly in Docker
        state = self.runtime.inspect_container(active_cont)
        self.assertTrue(state.running)
        self.assertEqual(state.status, "running")

        # 3. Simulate container crash by stopping it externally in Docker
        self.runtime.stop_container(active_cont, timeout=2)
        stopped_state = self.runtime.inspect_container(active_cont)
        self.assertFalse(stopped_state.running)

        # 4. Run reconciler pass - should detect that actual state != desired state
        report = self.reconciler.reconcile_once()

        self.assertIn(active_cont, report.dead_containers_detected)
        updated_dep = self.service.dep_repo.get_by_id(dep.id)
        assert updated_dep is not None
        self.assertEqual(updated_dep.status, DeploymentStatus.FAILED)
        self.assertIn("crashed or stopped", str(updated_dep.error_message).lower())

        # Check recorded events
        events = self.service.event_repo.list_by_app(self.app.id)
        event_kinds = [e.event_kind for e in events]
        self.assertIn(EventKind.DEPLOYMENT_CREATED, event_kinds)
        self.assertIn(EventKind.DEPLOYMENT_PROMOTED, event_kinds)
        self.assertIn(EventKind.CONTAINER_CRASHED, event_kinds)
        self.assertIn(EventKind.DEPLOYMENT_FAILED, event_kinds)


if __name__ == "__main__":
    unittest.main()
