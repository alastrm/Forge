import io
import unittest
from contextlib import redirect_stdout

from forge.api.server import ForgeApiServer
from forge.cli import ForgeApiClient, build_parser, cmd_app_create, cmd_app_list, cmd_status
from forge.proxy.fake import FakeProxy
from forge.runtime.fake import FakeRuntime
from forge.storage.db import Database


class TestForgeCli(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.runtime = FakeRuntime()
        self.proxy = FakeProxy()
        self.server = ForgeApiServer(
            db=self.db,
            runtime=self.runtime,
            proxy=self.proxy,
            host="127.0.0.1",
            port=0,
        )
        self.server.start()
        self.client = ForgeApiClient(base_url=f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self) -> None:
        self.server.stop()
        self.db.close()

    def test_parser_commands(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["server", "--port", "9000"])
        self.assertEqual(args.command, "server")
        self.assertEqual(args.port, 9000)

        args = parser.parse_args(["app", "create", "test-app", "--domain", "test.local", "--port", "3000"])
        self.assertEqual(args.command, "app")
        self.assertEqual(args.app_command, "create")
        self.assertEqual(args.name, "test-app")
        self.assertEqual(args.domain, "test.local")
        self.assertEqual(args.port, 3000)

        args = parser.parse_args(["deploy", "./sample", "--domain", "sample.local"])
        self.assertEqual(args.command, "deploy")
        self.assertEqual(str(args.project_path), "sample")
        self.assertEqual(args.domain, "sample.local")

    def test_cli_app_create_list_and_status(self) -> None:
        parser = build_parser()

        # Create app via CLI function
        args_create = parser.parse_args(["app", "create", "my-web", "--domain", "my-web.local", "--port", "8080"])
        out_buf = io.StringIO()
        with redirect_stdout(out_buf):
            cmd_app_create(self.client, args_create)
        self.assertIn("Application created successfully: my-web", out_buf.getvalue())

        # List apps via CLI function
        out_buf = io.StringIO()
        with redirect_stdout(out_buf):
            cmd_app_list(self.client)
        output = out_buf.getvalue()
        self.assertIn("my-web", output)
        self.assertIn("my-web.local", output)

        # Status via CLI function
        out_buf = io.StringIO()
        with redirect_stdout(out_buf):
            cmd_status(self.client, "my-web")
        output = out_buf.getvalue()
        self.assertIn("Application: my-web", output)
        self.assertIn("Domain:      my-web.local", output)


if __name__ == "__main__":
    unittest.main()
