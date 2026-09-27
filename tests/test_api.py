import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from forge.api.server import ForgeApiServer
from forge.core.models import DeploymentStatus
from forge.proxy.fake import FakeProxy
from forge.runtime.fake import FakeRuntime
from forge.storage.db import Database


class TestForgeApi(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.runtime = FakeRuntime()
        self.proxy = FakeProxy()
        # Bind to port=0 for ephemeral OS-allocated port
        self.server = ForgeApiServer(
            db=self.db,
            runtime=self.runtime,
            proxy=self.proxy,
            host="127.0.0.1",
            port=0,
            reconcile_interval=0.2,
        )
        self.server.start()

        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_path = Path(self.temp_dir.name)
        (self.project_path / "Dockerfile").write_text("FROM alpine\nCMD echo ok\n", encoding="utf-8")

        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.db.close()
        self.temp_dir.cleanup()

    def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method=method)
        req.add_header("Accept", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(data).encode("utf-8")

        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                body = resp.read().decode("utf-8")
                return status, json.loads(body) if body else {}
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body) if body else {}

    def test_health_endpoint(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "ok")

    def test_crud_applications(self) -> None:
        # Create app
        create_data = {
            "name": "api-test-service",
            "domain": "api-test.localhost",
            "container_port": 8080,
        }
        status, app = self._request("POST", "/api/v1/applications", data=create_data)
        self.assertEqual(status, 201)
        self.assertEqual(app["name"], "api-test-service")
        app_id = app["id"]

        # List apps
        status, apps = self._request("GET", "/api/v1/applications")
        self.assertEqual(status, 200)
        self.assertTrue(any(a["id"] == app_id for a in apps))

        # Get app details
        status, fetched = self._request("GET", f"/api/v1/applications/{app_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["id"], app_id)

        # Delete app
        status, del_resp = self._request("DELETE", f"/api/v1/applications/{app_id}")
        self.assertEqual(status, 200)
        self.assertTrue(del_resp.get("deleted"))

        # Confirm 404 after deletion
        status, err = self._request("GET", f"/api/v1/applications/{app_id}")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "not_found")

    def test_submit_deployment_async_and_poll_status(self) -> None:
        # Create app
        _, app = self._request(
            "POST",
            "/api/v1/applications",
            data={"name": "async-app", "domain": "async.localhost", "container_port": 8000},
        )
        app_id = app["id"]

        # Submit deployment
        deploy_payload = {
            "context_path": str(self.project_path),
            "health_check_path": "/health",
        }
        status, dep_submit = self._request(
            "POST",
            f"/api/v1/applications/{app_id}/deployments",
            data=deploy_payload,
        )
        self.assertEqual(status, 202)
        dep_id = dep_submit["deployment_id"]
        self.assertEqual(dep_submit["status"], "PENDING")

        # Poll deployment until ACTIVE
        deadline = time.monotonic() + 5.0
        active_reached = False
        while time.monotonic() < deadline:
            status, dep_info = self._request("GET", f"/api/v1/deployments/{dep_id}")
            self.assertEqual(status, 200)
            if dep_info["status"] == DeploymentStatus.ACTIVE.value:
                active_reached = True
                break
            time.sleep(0.05)

        self.assertTrue(active_reached, "Deployment did not reach ACTIVE state in time")

        # Check events endpoint
        status, events = self._request("GET", f"/api/v1/applications/{app_id}/events")
        self.assertEqual(status, 200)
        self.assertTrue(len(events) > 0)

        # Check logs endpoint
        status, logs_data = self._request("GET", f"/api/v1/applications/{app_id}/logs")
        self.assertEqual(status, 200)
        self.assertEqual(logs_data["app_id"], app_id)

    def test_rollback_endpoint(self) -> None:
        _, app = self._request(
            "POST",
            "/api/v1/applications",
            data={"name": "rollback-app", "domain": "rb.localhost", "container_port": 8000},
        )
        app_id = app["id"]

        # Deploy v1
        self._request(
            "POST",
            f"/api/v1/applications/{app_id}/deployments",
            data={"context_path": str(self.project_path)},
        )
        time.sleep(0.3)

        # Deploy v2
        _, dep2_info = self._request(
            "POST",
            f"/api/v1/applications/{app_id}/deployments",
            data={"context_path": str(self.project_path)},
        )
        dep2_id = dep2_info["deployment_id"]
        time.sleep(0.3)

        # Rollback v2
        status, rb_resp = self._request("POST", f"/api/v1/deployments/{dep2_id}/rollback")
        self.assertEqual(status, 200)
        self.assertEqual(rb_resp["status"], DeploymentStatus.ACTIVE.value)
        self.assertEqual(rb_resp["message"], "Rollback successful")

    def test_set_env_endpoint(self) -> None:
        _, app = self._request(
            "POST",
            "/api/v1/applications",
            data={"name": "env-app", "domain": "env.localhost", "container_port": 8000},
        )
        app_id = app["id"]

        status, resp = self._request(
            "POST",
            f"/api/v1/applications/{app_id}/env",
            data={"env_vars": {"DB_HOST": "localhost", "API_KEY": "secret123"}},
        )
        self.assertEqual(status, 200)
        self.assertTrue(resp["success"])
        self.assertEqual(resp["count"], 2)

        # GET app returns masked vars
        status, app_data = self._request("GET", f"/api/v1/applications/{app_id}")
        self.assertEqual(status, 200)
        keys = [item["key"] for item in app_data["env_vars"]]
        self.assertIn("DB_HOST", keys)
        self.assertIn("API_KEY", keys)

    def test_system_prune_endpoint(self) -> None:
        status, resp = self._request("POST", "/api/v1/system/prune")
        self.assertEqual(status, 200)
        self.assertTrue(resp["success"])
        self.assertIn("Total reclaimed images", resp["output"])


if __name__ == "__main__":
    unittest.main()

