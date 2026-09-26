import subprocess
import unittest
from unittest.mock import MagicMock, patch

from forge.core.errors import ValidationError
from forge.runtime.docker import DockerRuntime


class TestRuntimeDockerSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = DockerRuntime(timeout=5.0)

    @patch("subprocess.run")
    def test_docker_run_uses_env_file_not_command_line_args(self, mock_run: MagicMock) -> None:
        # SEC-08: Secrets must not be passed via CLI arguments (-e KEY=VAL)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="cont-123\n", stderr=""
        )

        env = {
            "SECRET_KEY": "super_secret_token_12345",
            "DATABASE_URL": "postgres://user:pass@db:5432/production",
        }

        self.runtime.create_container(
            image_tag="my-app:v1",
            container_name="my-app-cont",
            network="forge-net",
            labels={"forge.app": "my-app"},
            env_vars=env,
            port=8000,
        )

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]

        # Assert no plaintext secrets in cmd args
        self.assertNotIn("-e", cmd)
        self.assertNotIn("SECRET_KEY=super_secret_token_12345", cmd)
        self.assertNotIn("postgres://user:pass@db:5432/production", cmd)

        # Assert --env-file was used instead
        self.assertIn("--env-file", cmd)

    @patch("subprocess.run")
    def test_create_container_applies_security_flags(self, mock_run: MagicMock) -> None:
        # SEC-09: Docker hardening flags & quotas
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="cont-123\n", stderr=""
        )

        self.runtime.create_container(
            image_tag="my-app:v1",
            container_name="my-app-cont",
            network="forge-net",
            labels={"forge.app": "my-app"},
            env_vars={},
            port=8000,
            memory_limit="512m",
            cpu_limit="1.5",
            pids_limit=120,
        )

        cmd = mock_run.call_args[0][0]

        self.assertIn("--cap-drop=ALL", cmd)
        self.assertIn("--cap-add=NET_BIND_SERVICE", cmd)
        self.assertIn("--security-opt=no-new-privileges:true", cmd)
        self.assertIn("--memory=512m", cmd)
        self.assertIn("--cpus=1.5", cmd)
        self.assertIn("--pids-limit=120", cmd)

        # Forbidden flags
        self.assertNotIn("--privileged", cmd)
        self.assertNotIn("--net=host", cmd)

    @patch("subprocess.run")
    def test_candidate_container_starts_with_restart_no(self, mock_run: MagicMock) -> None:
        # SEC-10: Candidate starts strictly with restart_policy="no"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="cont-123\n", stderr=""
        )

        self.runtime.create_container(
            image_tag="my-app:v1",
            container_name="my-app-cont",
            network="forge-net",
            labels={},
            env_vars={},
            port=8000,
            restart_policy="no",
        )

        cmd = mock_run.call_args[0][0]
        self.assertIn("--restart=no", cmd)

    def test_docker_socket_mount_blocked(self) -> None:
        # SEC-11: Mounting docker.sock into user application is blocked
        with self.assertRaises(ValidationError):
            self.runtime.create_container(
                image_tag="my-app:v1",
                container_name="my-app-cont",
                network="forge-net",
                labels={"mount": "/var/run/docker.sock:/var/run/docker.sock"},
                env_vars={},
                port=8000,
            )


if __name__ == "__main__":
    unittest.main()
