import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from forge.cli import (
    _detect_dockerfile_port,
    _find_or_create_app,
    _normalize_app_name,
    build_parser,
)
from forge.core.models import Application, DeploymentStatus
from forge.deployments.service import DeploymentFailedError, DeploymentService
from forge.proxy.fake import FakeProxy
from forge.runtime.base import ContainerRuntimeState
from forge.runtime.fake import FakeRuntime
from forge.storage.db import Database
from forge.storage.repository import ApplicationRepository, DeploymentRepository


class TestDeployUX(unittest.TestCase):
    def test_normalize_app_name(self) -> None:
        self.assertEqual(_normalize_app_name("demo_stream_app"), "demo-stream-app")
        self.assertEqual(_normalize_app_name("My_Awesome_Service"), "my-awesome-service")
        self.assertEqual(_normalize_app_name("___test___app___"), "test-app")
        self.assertEqual(_normalize_app_name("valid-name-123"), "valid-name-123")
        self.assertEqual(_normalize_app_name("special!@#$%chars"), "special-chars")
        self.assertEqual(_normalize_app_name(""), "app")

    def test_cli_parser_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["deploy"])
        self.assertEqual(args.project_path, Path("."))
        self.assertEqual(args.app, "")
        self.assertIsNone(args.port)
        self.assertEqual(args.domain, "")

    def test_cli_parser_app_override(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["deploy", "./some_dir", "--app", "custom-app", "--port", "3000"])
        self.assertEqual(args.app, "custom-app")
        self.assertEqual(args.port, 3000)
        self.assertEqual(args.project_path, Path("./some_dir"))

    def test_detect_dockerfile_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            proj = Path(tmp_dir)
            # 1. No dockerfile
            self.assertIsNone(_detect_dockerfile_port(proj))

            # 2. Standard EXPOSE in Dockerfile
            (proj / "Dockerfile").write_text("FROM python:3.11\nEXPOSE 3000\n", encoding="utf-8")
            self.assertEqual(_detect_dockerfile_port(proj), 3000)

            # 3. Lowercase dockerfile with protocol suffix
            (proj / "Dockerfile").unlink()
            (proj / "dockerfile").write_text("FROM alpine\n  expose   8080/tcp\n", encoding="utf-8")
            self.assertEqual(_detect_dockerfile_port(proj), 8080)

            # 4. No EXPOSE instruction
            (proj / "dockerfile").write_text("FROM alpine\nCMD echo hi\n", encoding="utf-8")
            self.assertIsNone(_detect_dockerfile_port(proj))

    def test_app_name_priority_chain(self) -> None:
        # Priority: --app -> forge.json["app_name"] -> normalized directory name
        client = MagicMock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            proj = Path(tmp_dir) / "my_project_dir"
            proj.mkdir()

            # Priority 3: Fallback to normalized directory name
            client._request.side_effect = [(200, []), (201, {"id": "app-1"})]
            _find_or_create_app(client, proj, domain="", port=None)
            payload = client._request.call_args_list[1][1]["data"]
            self.assertEqual(payload["name"], "my-project-dir")

            # Priority 2: forge.json["app_name"] overrides directory name
            (proj / "forge.json").write_text(json.dumps({"app_name": "manifest-app"}), encoding="utf-8")
            client._request.reset_mock()
            client._request.side_effect = [(200, []), (201, {"id": "app-2"})]
            _find_or_create_app(client, proj, domain="", port=None)
            payload = client._request.call_args_list[1][1]["data"]
            self.assertEqual(payload["name"], "manifest-app")

            # Priority 1: --app overrides forge.json and directory name
            client._request.reset_mock()
            client._request.side_effect = [(200, []), (201, {"id": "app-3"})]
            _find_or_create_app(client, proj, domain="", port=None, app_override="cli-app")
            payload = client._request.call_args_list[1][1]["data"]
            self.assertEqual(payload["name"], "cli-app")

    def test_domain_priority_chain(self) -> None:
        # Priority: --domain -> forge.json["domain"] -> {app_name}.localhost
        client = MagicMock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            proj = Path(tmp_dir) / "demo_app"
            proj.mkdir()

            # Priority 3: {app_name}.localhost
            client._request.side_effect = [(200, []), (201, {"id": "app-1"})]
            _find_or_create_app(client, proj, domain="", port=None)
            payload = client._request.call_args_list[1][1]["data"]
            self.assertEqual(payload["domain"], "demo-app.localhost")

            # Priority 2: forge.json["domain"]
            (proj / "forge.json").write_text(json.dumps({"domain": "custom.example.org"}), encoding="utf-8")
            client._request.reset_mock()
            client._request.side_effect = [(200, []), (201, {"id": "app-2"})]
            _find_or_create_app(client, proj, domain="", port=None)
            payload = client._request.call_args_list[1][1]["data"]
            self.assertEqual(payload["domain"], "custom.example.org")

            # Priority 1: --domain flag
            client._request.reset_mock()
            client._request.side_effect = [(200, []), (201, {"id": "app-3"})]
            _find_or_create_app(client, proj, domain="cli.example.org", port=None)
            payload = client._request.call_args_list[1][1]["data"]
            self.assertEqual(payload["domain"], "cli.example.org")

    def test_port_priority_chain(self) -> None:
        # Priority: --port -> forge.json["container_port"] -> EXPOSE in Dockerfile -> 8000
        client = MagicMock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            proj = Path(tmp_dir) / "test_app"
            proj.mkdir()

            # Priority 4: Default 8000
            client._request.side_effect = [(200, []), (201, {"id": "app-1"})]
            _find_or_create_app(client, proj, domain="", port=None)
            payload = client._request.call_args_list[1][1]["data"]
            self.assertEqual(payload["container_port"], 8000)

            # Priority 3: Dockerfile EXPOSE
            (proj / "Dockerfile").write_text("FROM alpine\nEXPOSE 4000\n", encoding="utf-8")
            client._request.reset_mock()
            client._request.side_effect = [(200, []), (201, {"id": "app-2"})]
            _find_or_create_app(client, proj, domain="", port=None)
            payload = client._request.call_args_list[1][1]["data"]
            self.assertEqual(payload["container_port"], 4000)

            # Priority 2: forge.json["container_port"]
            (proj / "forge.json").write_text(json.dumps({"container_port": 5000}), encoding="utf-8")
            client._request.reset_mock()
            client._request.side_effect = [(200, []), (201, {"id": "app-3"})]
            _find_or_create_app(client, proj, domain="", port=None)
            payload = client._request.call_args_list[1][1]["data"]
            self.assertEqual(payload["container_port"], 5000)

            # Priority 1: --port flag
            client._request.reset_mock()
            client._request.side_effect = [(200, []), (201, {"id": "app-4"})]
            _find_or_create_app(client, proj, domain="", port=9000)
            payload = client._request.call_args_list[1][1]["data"]
            self.assertEqual(payload["container_port"], 9000)

    def test_failed_deployment_includes_container_output(self) -> None:
        db = Database(":memory:")
        runtime = FakeRuntime()
        proxy = FakeProxy()
        service = DeploymentService(db, runtime, proxy)
        app_repo = ApplicationRepository(db)
        dep_repo = DeploymentRepository(db)

        app = app_repo.create(name="fail-test-app", domain="fail-test.localhost", container_port=8080)

        # Set up a candidate container that exits immediately with an error log
        def fake_start(cid: str) -> None:
            runtime.containers[cid] = {
                "running": False,
                "state": ContainerRuntimeState(running=False, status="exited", exit_code=1),
                "logs": "Traceback (most recent call last):\n  File 'app.py', line 1, in <module>\nNameError: name 'bad' is not defined\n",
            }

        runtime.start_container = fake_start
        runtime.inspect_container = lambda cid: runtime.containers[cid]["state"]

        with tempfile.TemporaryDirectory() as tmp_dir:
            proj = Path(tmp_dir)
            (proj / "Dockerfile").write_text("FROM alpine\nCMD exit 1\n", encoding="utf-8")

            dep = service.create_deployment(app.id)
            with self.assertRaises(DeploymentFailedError) as ctx:
                service.execute_deployment(dep.id, context_path=proj)

            # Check that DeploymentFailedError and database error_message contain the container output
            self.assertIn("--- Container output ---", str(ctx.exception))
            self.assertIn("NameError: name 'bad' is not defined", str(ctx.exception))

            failed_dep = dep_repo.get_by_id(dep.id)
            self.assertIsNotNone(failed_dep)
            self.assertEqual(failed_dep.status, DeploymentStatus.FAILED)
            self.assertIn("--- Container output ---", failed_dep.error_message)
            self.assertIn("NameError: name 'bad' is not defined", failed_dep.error_message)

    def test_env_set_forbids_cli_key_value(self) -> None:
        from forge.cli import cmd_env_set

        client = MagicMock()
        # Attempting KEY=VALUE must be strictly rejected
        with self.assertRaises(SystemExit) as ctx:
            cmd_env_set(client, "myapp", ["SECRET_KEY=leaked_in_ps_aux"])
        self.assertEqual(ctx.exception.code, 1)
        client._request.assert_not_called()

    def test_env_set_interactive_prompt(self) -> None:
        from unittest.mock import patch
        from forge.cli import cmd_env_set

        client = MagicMock()
        client._request.return_value = (200, {"success": True, "count": 1})

        with patch("getpass.getpass", return_value="supersecret"):
            cmd_env_set(client, "myapp", ["DATABASE_PASSWORD"])

        client._request.assert_called_once_with(
            "POST",
            "/api/v1/applications/myapp/env",
            data={"env_vars": {"DATABASE_PASSWORD": "supersecret"}},
        )

    def test_env_set_file(self) -> None:
        from forge.cli import cmd_env_set

        client = MagicMock()
        client._request.return_value = (200, {"success": True, "count": 2})

        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".env") as f:
            f.write("KEY1=VAL1\nKEY2=VAL2\n")
            f_path = Path(f.name)

        try:
            cmd_env_set(client, "myapp", keys=[], file_path=f_path)
            client._request.assert_called_once_with(
                "POST",
                "/api/v1/applications/myapp/env",
                data={"env_vars": {"KEY1": "VAL1", "KEY2": "VAL2"}},
            )
        finally:
            f_path.unlink(missing_ok=True)

    def test_prune_runtime_and_command(self) -> None:
        from forge.cli import cmd_prune

        runtime = FakeRuntime()
        runtime.built_images = ["img1", "img2"]
        out = runtime.prune_images()
        self.assertIn("Total reclaimed images: 2", out)
        self.assertEqual(len(runtime.built_images), 0)

        client = MagicMock()
        client._request.return_value = (200, {"success": True, "output": "Total reclaimed images: 2"})
        cmd_prune(client)
        client._request.assert_called_once_with("POST", "/api/v1/system/prune")

    def test_cli_parser_env_and_prune(self) -> None:
        parser = build_parser()
        # env set interactive keys
        args = parser.parse_args(["env", "set", "myapp", "FOO", "BAR"])
        self.assertEqual(args.command, "env")
        self.assertEqual(args.env_command, "set")
        self.assertEqual(args.app_name, "myapp")
        self.assertEqual(args.keys, ["FOO", "BAR"])
        self.assertIsNone(args.file)

        # env set --file
        args_file = parser.parse_args(["env", "set", "myapp", "--file", "./secrets.env"])
        self.assertEqual(args_file.file, Path("./secrets.env"))
        self.assertEqual(args_file.keys, [])

        # prune
        args_prune = parser.parse_args(["prune"])
        self.assertEqual(args_prune.command, "prune")

        # deploy with --env-file
        args_deploy = parser.parse_args(["deploy", "--env-file", "./custom.env"])
        self.assertEqual(args_deploy.env_file, Path("./custom.env"))


if __name__ == "__main__":
    unittest.main()

