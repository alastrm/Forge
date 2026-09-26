import unittest

from forge.core.errors import InvalidStateTransitionError, StorageError
from forge.core.models import DeploymentStatus, EventKind
from forge.storage.db import Database
from forge.storage.repository import (
    ApplicationRepository,
    DeploymentRepository,
    EnvironmentRepository,
    EventRepository,
)


class TestStorage(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.app_repo = ApplicationRepository(self.db)
        self.dep_repo = DeploymentRepository(self.db)
        self.event_repo = EventRepository(self.db)
        self.env_repo = EnvironmentRepository(self.db)

    def tearDown(self) -> None:
        self.db.close()

    def test_application_lifecycle_crud(self) -> None:
        app = self.app_repo.create("my-service", "my-service.localhost", container_port=8080)
        self.assertTrue(app.id.startswith("app-"))
        self.assertEqual(app.name, "my-service")
        self.assertEqual(app.domain, "my-service.localhost")
        self.assertEqual(app.container_port, 8080)

        # Retrieve by id
        fetched = self.app_repo.get_by_id(app.id)
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.name, "my-service")

        # Retrieve by name
        fetched_name = self.app_repo.get_by_name("my-service")
        self.assertIsNotNone(fetched_name)
        assert fetched_name is not None
        self.assertEqual(fetched_name.id, app.id)

        # Duplicate name raises StorageError
        with self.assertRaises(StorageError):
            self.app_repo.create("my-service", "other.localhost")

        # List all
        apps = self.app_repo.list_all()
        self.assertEqual(len(apps), 1)

        # Delete
        deleted = self.app_repo.delete(app.id)
        self.assertTrue(deleted)
        self.assertIsNone(self.app_repo.get_by_id(app.id))

    def test_deployment_lifecycle_and_state_transitions(self) -> None:
        app = self.app_repo.create("web-app", "web.localhost")
        dep = self.dep_repo.create(app.id, status=DeploymentStatus.PENDING)
        self.assertEqual(dep.status, DeploymentStatus.PENDING)

        # Update to BUILDING
        dep = self.dep_repo.update_status(dep.id, DeploymentStatus.BUILDING)
        self.assertEqual(dep.status, DeploymentStatus.BUILDING)
        self.assertIsNotNone(dep.started_at)

        # Update to STARTING with candidate container id
        dep = self.dep_repo.update_status(
            dep.id,
            DeploymentStatus.STARTING,
            candidate_container_id="cont-candidate-123",
        )
        self.assertEqual(dep.status, DeploymentStatus.STARTING)
        self.assertEqual(dep.candidate_container_id, "cont-candidate-123")

        # Update to HEALTH_CHECKING
        dep = self.dep_repo.update_status(dep.id, DeploymentStatus.HEALTH_CHECKING)
        self.assertEqual(dep.status, DeploymentStatus.HEALTH_CHECKING)

        # Update to ACTIVE with active container id
        dep = self.dep_repo.update_status(
            dep.id,
            DeploymentStatus.ACTIVE,
            active_container_id="cont-candidate-123",
        )
        self.assertEqual(dep.status, DeploymentStatus.ACTIVE)
        self.assertEqual(dep.active_container_id, "cont-candidate-123")
        self.assertIsNotNone(dep.finished_at)

        # Verify active deployment query
        active = self.dep_repo.get_active_deployment(app.id)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.id, dep.id)

        # Attempt illegal transition from ACTIVE -> BUILDING
        with self.assertRaises(InvalidStateTransitionError):
            self.dep_repo.update_status(dep.id, DeploymentStatus.BUILDING)

    def test_events_recording_and_listing(self) -> None:
        app = self.app_repo.create("event-app", "event.localhost")
        dep = self.dep_repo.create(app.id)

        evt1 = self.event_repo.record(
            app.id,
            EventKind.DEPLOYMENT_CREATED,
            payload={"version": "v1"},
            deployment_id=dep.id,
        )
        self.assertTrue(evt1.id.startswith("evt-"))
        self.assertEqual(evt1.event_kind, EventKind.DEPLOYMENT_CREATED)

        evt2 = self.event_repo.record(
            app.id,
            EventKind.DEPLOYMENT_HEALTH_CHECK_PASSED,
            payload={"latency_ms": 42},
            deployment_id=dep.id,
        )

        events = self.event_repo.list_by_app(app.id)
        self.assertEqual(len(events), 2)
        # Verify ordering is newest first
        self.assertEqual(events[0].id, evt2.id)
        self.assertEqual(events[1].id, evt1.id)

    def test_environment_variables_crud_and_masking(self) -> None:
        app = self.app_repo.create("env-app", "env.localhost")

        self.env_repo.set_var(app.id, "API_KEY", "super-secret-key-123")
        self.env_repo.set_var(app.id, "PORT", "9000")

        # Raw vars (internal use)
        raw = self.env_repo.get_vars(app.id)
        self.assertEqual(raw["API_KEY"], "super-secret-key-123")
        self.assertEqual(raw["PORT"], "9000")

        # SEC-07: Masked vars (for API responses)
        masked = self.env_repo.get_masked_vars(app.id)
        self.assertEqual(len(masked), 2)
        keys = [item["key"] for item in masked]
        self.assertIn("API_KEY", keys)
        self.assertIn("PORT", keys)
        for item in masked:
            self.assertTrue(item["is_set"])
            self.assertNotIn("value", item)  # Value is NEVER present in masked format!


if __name__ == "__main__":
    unittest.main()
