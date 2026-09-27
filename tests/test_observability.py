import io
import json
import tempfile
import unittest
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

from forge.api.server import ForgeApiServer
from forge.cli import ForgeApiClient, cmd_logs
from forge.core.models import DeploymentStatus
from forge.proxy.fake import FakeProxy
from forge.runtime.docker import DockerRuntime
from forge.runtime.fake import FakeRuntime
from forge.storage.db import Database
from forge.storage.repository import (
    ApplicationRepository,
    DeploymentRepository,
    EnvironmentRepository,
)


class TestObservability(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.runtime = FakeRuntime()
        self.proxy = FakeProxy()
        self.app_repo = ApplicationRepository(self.db)
        self.dep_repo = DeploymentRepository(self.db)
        self.env_repo = EnvironmentRepository(self.db)

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

        # Seed an application
        self.app = self.app_repo.create(
            name="stream-app",
            domain="stream-app.localhost",
            container_port=8080,
        )

    def tearDown(self) -> None:
        self.server.stop()
        self.db.close()
        self.temp_dir.cleanup()

    def _create_active_deployment(self, container_id: str):
        dep = self.dep_repo.create(app_id=self.app.id, status=DeploymentStatus.PENDING)
        self.dep_repo.update_status(dep.id, DeploymentStatus.BUILDING)
        self.dep_repo.update_status(dep.id, DeploymentStatus.STARTING)
        self.dep_repo.update_status(dep.id, DeploymentStatus.HEALTH_CHECKING)
        return self.dep_repo.update_status(
            dep.id,
            DeploymentStatus.ACTIVE,
            active_container_id=container_id,
        )

    def test_fake_runtime_logs_stream(self) -> None:
        self.runtime.containers["c-123"] = {"logs": "line 1\nline 2\nline 3\n"}
        stream = self.runtime.logs_stream("c-123")
        self.assertIsInstance(stream, Iterator)
        lines = list(stream)
        self.assertEqual(lines, ["line 1\n", "line 2\n", "line 3\n"])

    def test_api_logs_snapshot_compatibility(self) -> None:
        self._create_active_deployment(container_id="cont-active-1")
        self.runtime.containers["cont-active-1"] = {"logs": "standard non-stream logs\n"}

        req = urllib.request.Request(f"{self.base_url}/api/v1/applications/{self.app.name}/logs")
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["app_id"], self.app.id)
            self.assertEqual(data["logs"], "standard non-stream logs\n")

    def test_api_logs_streaming_follow(self) -> None:
        self._create_active_deployment(container_id="cont-active-2")
        self.runtime.containers["cont-active-2"] = {"logs": "stream 1\nstream 2\nstream 3\n"}

        req = urllib.request.Request(f"{self.base_url}/api/v1/applications/{self.app.name}/logs?follow=true")
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "text/plain; charset=utf-8")
            lines = [line.decode("utf-8") for line in resp]
            self.assertEqual(lines, ["stream 1\n", "stream 2\n", "stream 3\n"])

    def test_api_logs_streaming_secret_scrubbing(self) -> None:
        # Register a secret env var
        self.env_repo.set_var(self.app.id, "SUPER_SECRET", "p@ssword123")

        self._create_active_deployment(container_id="cont-active-sec")
        self.runtime.containers["cont-active-sec"] = {
            "logs": "Connecting using p@ssword123 credentials...\nDone.\n"
        }

        req = urllib.request.Request(f"{self.base_url}/api/v1/applications/{self.app.id}/logs?follow=true")
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            lines = [line.decode("utf-8") for line in resp]
            full_output = "".join(lines)
            self.assertNotIn("p@ssword123", full_output)
            self.assertIn("[REDACTED]", full_output)
            self.assertIn("Connecting using [REDACTED] credentials...", full_output)

    def test_api_logs_streaming_no_active_container(self) -> None:
        req = urllib.request.Request(f"{self.base_url}/api/v1/applications/{self.app.id}/logs?follow=true")
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8")
            self.assertIn("<no active container to stream logs>", body)

    def test_cli_client_streaming(self) -> None:
        self._create_active_deployment(container_id="cont-cli")
        self.runtime.containers["cont-cli"] = {"logs": "cli line 1\ncli line 2\n"}

        client = ForgeApiClient(base_url=self.base_url)
        lines = list(client.stream_logs(self.app.name))
        self.assertEqual(lines, ["cli line 1\n", "cli line 2\n"])

        # Test cmd_logs with follow=True writes to stdout
        output_buffer = io.StringIO()
        with patch("sys.stdout", output_buffer):
            cmd_logs(client, self.app.name, tail=50, follow=True)
        self.assertEqual(output_buffer.getvalue(), "cli line 1\ncli line 2\n")

    def test_docker_runtime_logs_stream_process_cleanup(self) -> None:
        runtime = DockerRuntime()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = iter(["log line 1\n", "log line 2\n", "log line 3\n"])

        with patch("subprocess.Popen", return_value=mock_proc):
            gen = runtime.logs_stream("container-test-id", tail=50)
            first_line = next(gen)
            self.assertEqual(first_line, "log line 1\n")
            # Close the generator prematurely (simulating client disconnect / break)
            gen.close()

            # Ensure cleanup ran: process terminated and wait called
            mock_proc.terminate.assert_called_once()
            mock_proc.wait.assert_called()


if __name__ == "__main__":
    unittest.main()
