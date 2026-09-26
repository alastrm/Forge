import json
import unittest
import urllib.error
import urllib.request
from typing import Any

from forge.api.auth import ApiAuth
from forge.api.server import ForgeApiServer
from forge.core.errors import ValidationError
from forge.proxy.fake import FakeProxy
from forge.runtime.fake import FakeRuntime
from forge.storage.db import Database


class TestApiSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.runtime = FakeRuntime()
        self.proxy = FakeProxy()
        self.api_token = "sec-auth-token-98765"
        self.server = ForgeApiServer(
            db=self.db,
            runtime=self.runtime,
            proxy=self.proxy,
            host="127.0.0.1",
            port=0,
            api_token=self.api_token,
        )
        self.server.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.db.close()

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

    def test_api_auth_invalid_token_rejected(self) -> None:
        # SEC-01: Reject request with missing token
        status, body = self._request("GET", "/api/v1/applications")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

        # Reject request with invalid token
        status, body = self._request("GET", "/api/v1/applications", token="invalid-token")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

        # Allow request with valid Bearer token
        status, body = self._request("GET", "/api/v1/applications", token=self.api_token)
        self.assertEqual(status, 200)

    def test_api_timing_safe_compare(self) -> None:
        # SEC-01: Direct verification of ApiAuth timing-safe behavior
        auth = ApiAuth("topsecretkey123")
        self.assertTrue(auth.verify_token("Bearer topsecretkey123"))
        self.assertFalse(auth.verify_token("Bearer wrongkey"))
        self.assertFalse(auth.verify_token("Bearer topsecretkey12"))
        self.assertFalse(auth.verify_token("Bearer "))
        self.assertFalse(auth.verify_token(""))
        self.assertFalse(auth.verify_token(None))
        self.assertFalse(auth.verify_token("Basic topsecretkey123"))

    def test_server_binds_strictly_to_127_0_0_1(self) -> None:
        # SEC-04: Non-localhost bind addresses must be rejected
        with self.assertRaises(ValidationError):
            ForgeApiServer(
                db=self.db,
                runtime=self.runtime,
                proxy=self.proxy,
                host="0.0.0.0",
                port=8000,
            ).start()

        with self.assertRaises(ValidationError):
            ForgeApiServer(
                db=self.db,
                runtime=self.runtime,
                proxy=self.proxy,
                host="192.168.1.50",
                port=8000,
            ).start()

    def test_get_application_masks_environment_secrets(self) -> None:
        # SEC-07: Never return raw secret values in API metadata responses
        app = self.server.db_app = self.db
        from forge.storage.repository import ApplicationRepository, EnvironmentRepository
        app_repo = ApplicationRepository(self.db)
        env_repo = EnvironmentRepository(self.db)

        app = app_repo.create(name="sec-app", domain="sec.localhost", container_port=8000)
        env_repo.set_var(app.id, "DATABASE_PASSWORD", "super_secret_db_pass_12345")
        env_repo.set_var(app.id, "STRIPE_SECRET_KEY", "sk_live_abcdef123456789")

        status, body = self._request("GET", f"/api/v1/applications/{app.id}", token=self.api_token)
        self.assertEqual(status, 200)

        raw_json = json.dumps(body)
        self.assertNotIn("super_secret_db_pass_12345", raw_json)
        self.assertNotIn("sk_live_abcdef123456789", raw_json)

        env_vars = body["env_vars"]
        self.assertEqual(len(env_vars), 2)
        keys = [v["key"] for v in env_vars]
        self.assertIn("DATABASE_PASSWORD", keys)
        self.assertIn("STRIPE_SECRET_KEY", keys)
        self.assertTrue(all(v["is_set"] for v in env_vars))


if __name__ == "__main__":
    unittest.main()
