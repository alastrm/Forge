import argparse
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from forge.config import load_project_config, parse_env_file


class ForgeApiClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", api_token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token or os.environ.get("FORGE_API_TOKEN")

    def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method=method)
        req.add_header("Accept", "application/json")
        if self.api_token:
            req.add_header("Authorization", f"Bearer {self.api_token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(data).encode("utf-8")

        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                status = resp.status
                body = resp.read().decode("utf-8")
                return status, json.loads(body) if body else {}
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body) if body else {}
        except urllib.error.URLError as err:
            print(f"Error: Could not connect to Forge server at {self.base_url}: {err.reason}", file=sys.stderr)
            print("Tip: Make sure 'forge server' is running.", file=sys.stderr)
            sys.exit(1)

    def stream_logs(self, app_identifier: str, tail: int = 100) -> Iterator[str]:
        url = f"{self.base_url}/api/v1/applications/{app_identifier}/logs?tail={tail}&follow=true"
        req = urllib.request.Request(url, method="GET")
        if self.api_token:
            req.add_header("Authorization", f"Bearer {self.api_token}")
        try:
            with urllib.request.urlopen(req, timeout=None) as resp:
                for line in resp:
                    yield line.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
            try:
                err_json = json.loads(body)
                msg = err_json.get("error", {}).get("message", body)
            except Exception:
                msg = body
            print(f"Error ({err.code}): {msg}", file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as err:
            print(f"Error: Could not connect to Forge server at {self.base_url}: {err.reason}", file=sys.stderr)
            sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge Control Plane CLI",
    )
    parser.add_argument(
        "--server",
        default=os.environ.get("FORGE_SERVER_URL", "http://127.0.0.1:8000"),
        help="Forge server URL (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("FORGE_API_TOKEN"),
        help="Forge API Bearer token",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # forge server
    server_parser = subparsers.add_parser("server", help="Start the Forge Control Plane server daemon")
    server_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    server_parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    server_parser.add_argument("--db", default="forge.db", help="Path to SQLite database file")
    server_parser.add_argument("--workspace", default=None, help="Authorized workspace directory boundary")

    # forge app
    app_parser = subparsers.add_parser("app", help="Manage applications")
    app_sub = app_parser.add_subparsers(dest="app_command", help="Application subcommands")

    # forge app create
    app_create = app_sub.add_parser("create", help="Create a new application")
    app_create.add_argument("name", help="Application name")
    app_create.add_argument("--domain", required=True, help="Domain for routing")
    app_create.add_argument("--port", type=int, default=8000, help="Container port (default: 8000)")

    # forge app list
    app_sub.add_parser("list", help="List all applications")

    # forge deploy
    deploy_parser = subparsers.add_parser("deploy", help="Deploy an application directory")
    deploy_parser.add_argument(
        "project_path",
        nargs="?",
        default=Path("."),
        type=Path,
        help="Path to directory containing Dockerfile (default: current directory)",
    )
    deploy_parser.add_argument("--app", default="", help="Application name override")
    deploy_parser.add_argument("--domain", default="", help="Domain name for routing")
    deploy_parser.add_argument("--port", type=int, default=None, help="Container port")
    deploy_parser.add_argument("--env-file", type=Path, default=None, help="Path to custom .env file")

    # forge deployments
    deps_parser = subparsers.add_parser("deployments", help="List deployments for an application")
    deps_parser.add_argument("app_name", help="Application name or ID")

    # forge status
    status_parser = subparsers.add_parser("status", help="Get status of an application")
    status_parser.add_argument("app_name", help="Application name or ID")

    # forge logs
    logs_parser = subparsers.add_parser("logs", help="View logs for an application's active container")
    logs_parser.add_argument("app_name", help="Application name or ID")
    logs_parser.add_argument("--tail", type=int, default=100, help="Number of lines to show (default: 100)")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Stream logs in real-time")

    # forge rollback
    rollback_parser = subparsers.add_parser("rollback", help="Roll back application to previous deployment")
    rollback_parser.add_argument("app_name", help="Application name or ID")

    # forge env
    env_parser = subparsers.add_parser("env", help="Manage application environment variables")
    env_sub = env_parser.add_subparsers(dest="env_command", help="Environment subcommands")
    env_set = env_sub.add_parser("set", help="Set environment variable(s) for an application")
    env_set.add_argument("app_name", help="Application name or ID")
    env_set.add_argument("vars", nargs="+", help="KEY=VALUE pairs to set")

    # forge prune
    subparsers.add_parser("prune", help="Prune dangling container images to reclaim disk space")

    return parser


def cmd_server(args: argparse.Namespace) -> None:
    from forge.api.server import ForgeApiServer
    from forge.proxy.traefik import TraefikProxy
    from forge.runtime.docker import DockerRuntime
    from forge.storage.db import Database

    db = Database(args.db)
    runtime = DockerRuntime()
    proxy = TraefikProxy()

    server = ForgeApiServer(
        db=db,
        runtime=runtime,
        proxy=proxy,
        host=args.host,
        port=args.port,
        api_token=args.token,
        workspace_boundary=args.workspace,
    )
    print(f"Starting Forge Control Plane on http://{args.host}:{args.port}")
    server.start()

    shutdown_event = setup_signal_handlers(server)

    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        shutdown_event.set()

    print("\nShutting down Forge server...")
    server.stop()
    print("Forge server stopped.")


def setup_signal_handlers(server: Any) -> threading.Event:
    """Register SIGINT and SIGTERM handlers to gracefully stop the Forge server."""
    shutdown_event = threading.Event()

    def _sig_handler(signum: int, frame: Any) -> None:
        shutdown_event.set()
        try:
            server.stop()
        except Exception:
            pass

    signal.signal(signal.SIGINT, _sig_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig_handler)

    return shutdown_event


def cmd_app_create(client: ForgeApiClient, args: argparse.Namespace) -> None:
    payload = {
        "name": args.name,
        "domain": args.domain,
        "container_port": args.port,
    }
    status, res = client._request("POST", "/api/v1/applications", data=payload)
    if status == 201:
        print(f"Application created successfully: {res['name']} ({res['id']})")
    else:
        err = res.get("error", {}).get("message", res)
        print(f"Failed to create application: {err}", file=sys.stderr)
        sys.exit(1)


def cmd_app_list(client: ForgeApiClient) -> None:
    status, apps = client._request("GET", "/api/v1/applications")
    if status == 200:
        if not apps:
            print("No applications found.")
            return
        print(f"{'NAME':<20} {'DOMAIN':<30} {'PORT':<8} {'ID'}")
        print("-" * 75)
        for a in apps:
            print(f"{a['name']:<20} {a['domain']:<30} {a['container_port']:<8} {a['id']}")
    else:
        err = apps.get("error", {}).get("message", apps)
        print(f"Failed to list applications: {err}", file=sys.stderr)
        sys.exit(1)


def _normalize_app_name(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]", "-", name).lower()
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:63] or "app"


def _detect_dockerfile_port(project_path: Path) -> int | None:
    for filename in ("Dockerfile", "dockerfile"):
        df = project_path / filename
        if df.is_file():
            try:
                for line in df.read_text(encoding="utf-8", errors="replace").splitlines():
                    match = re.match(r"^\s*EXPOSE\s+(\d+)", line, re.IGNORECASE)
                    if match:
                        p = int(match.group(1))
                        if 1 <= p <= 65535:
                            return p
            except Exception:
                pass
            break
    return None


def _find_or_create_app(
    client: ForgeApiClient,
    project_path: Path,
    domain: str,
    port: int | None,
    app_override: str = "",
) -> str:
    manifest, _ = load_project_config(project_path)

    # 1. App Name: --app -> forge.json["app_name"] -> normalized directory name
    if app_override and app_override.strip():
        app_name = app_override.strip()
    elif manifest.get("app_name"):
        app_name = str(manifest["app_name"]).strip()
    else:
        app_name = _normalize_app_name(project_path.resolve().name)

    # 2. Domain: --domain -> forge.json["domain"] -> {app_name}.localhost
    if domain and domain.strip():
        domain_name = domain.strip()
    elif manifest.get("domain"):
        domain_name = str(manifest["domain"]).strip()
    else:
        domain_name = f"{_normalize_app_name(app_name)}.localhost"

    # 3. Port: --port -> forge.json["container_port"] -> EXPOSE in Dockerfile -> 8000
    if port is not None:
        port_num = port
    elif manifest.get("container_port") is not None:
        try:
            port_num = int(manifest["container_port"])
        except (ValueError, TypeError):
            exposed = _detect_dockerfile_port(project_path)
            port_num = exposed if exposed is not None else 8000
    else:
        exposed = _detect_dockerfile_port(project_path)
        port_num = exposed if exposed is not None else 8000

    # Look up if app exists
    status, apps = client._request("GET", "/api/v1/applications")
    if status == 200:
        for a in apps:
            if a["name"] == app_name or a["id"] == app_name:
                return a["id"]

    # Create app if not found
    status, new_app = client._request(
        "POST",
        "/api/v1/applications",
        data={"name": app_name, "domain": domain_name, "container_port": port_num},
    )
    if status == 201:
        return new_app["id"]
    else:
        err = new_app.get("error", {}).get("message", new_app)
        print(f"Failed to ensure application: {err}", file=sys.stderr)
        sys.exit(1)


def cmd_deploy(client: ForgeApiClient, args: argparse.Namespace) -> None:
    project_path = args.project_path.resolve()
    has_dockerfile = (project_path / "Dockerfile").is_file() or (project_path / "dockerfile").is_file()
    if not has_dockerfile:
        print(f"Error: Dockerfile not found in '{project_path}'", file=sys.stderr)
        print("Tip: Run this command inside your project directory or pass the path: forge deploy ./path/to/project", file=sys.stderr)
        sys.exit(1)

    app_id = _find_or_create_app(
        client,
        project_path,
        args.domain,
        args.port,
        app_override=getattr(args, "app", ""),
    )

    # Load and sync environment variables from .env and forge.json
    env_file = getattr(args, "env_file", None)
    env_file_path = env_file.resolve() if env_file else (project_path / ".env")
    env_vars: dict[str, str] = {}
    if env_file_path.is_file():
        env_vars.update(parse_env_file(env_file_path))

    manifest, _ = load_project_config(project_path)
    if isinstance(manifest.get("env"), dict):
        merged = {str(k): str(v) for k, v in manifest["env"].items()}
        merged.update(env_vars)
        env_vars = merged

    if env_vars:
        st, _ = client._request("POST", f"/api/v1/applications/{app_id}/env", data={"env_vars": env_vars})
        if st == 200:
            source_desc = f"'{env_file_path.name}'" if env_file_path.is_file() else "manifest"
            print(f"Loaded {len(env_vars)} environment variables from {source_desc}")

    print(f"Deploying application '{project_path.name}' to Forge control plane...")
    payload = {
        "context_path": str(project_path),
        "health_check_path": "/health",
    }
    status, submit_res = client._request("POST", f"/api/v1/applications/{app_id}/deployments", data=payload)
    if status != 202:
        err = submit_res.get("error", {}).get("message", submit_res)
        print(f"Deployment rejected: {err}", file=sys.stderr)
        sys.exit(1)

    dep_id = submit_res["deployment_id"]
    print(f"Deployment enqueued: {dep_id}")
    print("Waiting for deployment to complete...")

    last_status = None
    while True:
        status, dep = client._request("GET", f"/api/v1/deployments/{dep_id}")
        if status != 200:
            print("Failed to fetch deployment status.", file=sys.stderr)
            sys.exit(1)

        current_status = dep.get("status")
        if current_status != last_status:
            print(f"Status: {current_status}")
            last_status = current_status

        if current_status == "ACTIVE":
            print(f"\nDeployment successful! Active container: {dep.get('active_container_id')}")
            break
        elif current_status == "FAILED":
            err_msg = dep.get("error_message", "Unknown error")
            print(f"\nDeployment failed: {err_msg}", file=sys.stderr)
            if "--- Container output ---" not in err_msg:
                _, logs_data = client._request("GET", f"/api/v1/applications/{app_id}/logs")
                if logs_data and logs_data.get("logs") and logs_data["logs"].strip():
                    print("--- Container output ---")
                    print(logs_data["logs"].strip())
                    print("------------------------")
            sys.exit(1)

        time.sleep(0.5)


def cmd_status(client: ForgeApiClient, app_identifier: str) -> None:
    # Resolve app_id
    app_id = app_identifier
    status, app = client._request("GET", f"/api/v1/applications/{app_id}")
    if status != 200:
        # Search by name
        status, apps = client._request("GET", "/api/v1/applications")
        if status == 200:
            for a in apps:
                if a["name"] == app_identifier:
                    status, app = client._request("GET", f"/api/v1/applications/{a['id']}")
                    break

    if status == 200:
        print(f"Application: {app['name']} ({app['id']})")
        print(f"Domain:      {app['domain']}")
        print(f"Port:        {app['container_port']}")
        act = app.get("active_deployment")
        if act:
            print(f"Active Dep:  {act['id']} (container: {act.get('active_container_id')})")
        else:
            print("Active Dep:  None")
        print(f"Configured Env: {len(app.get('env_vars', []))} variables")
    else:
        print(f"Application '{app_identifier}' not found.", file=sys.stderr)
        sys.exit(1)


def cmd_logs(client: ForgeApiClient, app_identifier: str, tail: int, follow: bool = False) -> None:
    if follow:
        try:
            for line in client.stream_logs(app_identifier, tail=tail):
                sys.stdout.write(line)
                sys.stdout.flush()
        except KeyboardInterrupt:
            pass
        return

    status, logs_data = client._request("GET", f"/api/v1/applications/{app_identifier}/logs?tail={tail}")
    if status == 200:
        logs = logs_data.get("logs", "")
        if logs:
            print(logs)
        else:
            print("<no logs recorded>")
    else:
        print(f"Failed to fetch logs for '{app_identifier}'", file=sys.stderr)
        sys.exit(1)


def cmd_rollback(client: ForgeApiClient, app_identifier: str) -> None:
    # Get app to find active deployment
    status, app = client._request("GET", f"/api/v1/applications/{app_identifier}")
    if status != 200:
        status, apps = client._request("GET", "/api/v1/applications")
        if status == 200:
            for a in apps:
                if a["name"] == app_identifier:
                    status, app = client._request("GET", f"/api/v1/applications/{a['id']}")
                    break

    if status != 200 or not app.get("active_deployment"):
        print(f"No active deployment found to roll back for '{app_identifier}'", file=sys.stderr)
        sys.exit(1)

    act_id = app["active_deployment"]["id"]
    status, res = client._request("POST", f"/api/v1/deployments/{act_id}/rollback")
    if status == 200:
        print(f"Rollback succeeded! Active container is now: {res.get('active_container_id')}")
    else:
        err = res.get("error", {}).get("message", res)
        print(f"Rollback failed: {err}", file=sys.stderr)
        sys.exit(1)


def cmd_env_set(client: ForgeApiClient, app_identifier: str, key_val_pairs: list[str]) -> None:
    env_vars: dict[str, str] = {}
    for pair in key_val_pairs:
        if "=" not in pair:
            print(f"Error: Invalid variable format '{pair}'. Expected KEY=VALUE", file=sys.stderr)
            sys.exit(1)
        k, v = pair.split("=", 1)
        k = k.strip()
        if not k:
            print(f"Error: Empty variable name in '{pair}'", file=sys.stderr)
            sys.exit(1)
        env_vars[k] = v

    status, res = client._request("POST", f"/api/v1/applications/{app_identifier}/env", data={"env_vars": env_vars})
    if status == 200:
        print(f"Successfully configured {len(env_vars)} environment variables for '{app_identifier}'.")
    else:
        err = res.get("error", {}).get("message", res)
        print(f"Failed to set environment variables: {err}", file=sys.stderr)
        sys.exit(1)


def cmd_prune(client: ForgeApiClient) -> None:
    print("Pruning unused Docker images...")
    status, res = client._request("POST", "/api/v1/system/prune")
    if status == 200:
        out = res.get("output", "")
        if out:
            print(out)
        else:
            print("Successfully pruned images.")
    else:
        err = res.get("error", {}).get("message", res)
        print(f"Prune failed: {err}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "server":
        cmd_server(args)
        return

    client = ForgeApiClient(base_url=args.server, api_token=args.token)

    if args.command == "app":
        if args.app_command == "create":
            cmd_app_create(client, args)
        elif args.app_command == "list":
            cmd_app_list(client)
        else:
            parser.parse_args(["app", "--help"])
    elif args.command == "deploy":
        cmd_deploy(client, args)
    elif args.command == "status":
        cmd_status(client, args.app_name)
    elif args.command == "logs":
        cmd_logs(client, args.app_name, args.tail, follow=getattr(args, "follow", False))
    elif args.command == "rollback":
        cmd_rollback(client, args.app_name)
    elif args.command == "env":
        if getattr(args, "env_command", None) == "set":
            cmd_env_set(client, args.app_name, args.vars)
        else:
            parser.parse_args(["env", "--help"])
    elif args.command == "prune":
        cmd_prune(client)
    elif args.command == "deployments":
        status, deps = client._request("GET", f"/api/v1/applications/{args.app_name}/deployments")
        if status == 200:
            if not deps:
                print(f"No deployments found for application '{args.app_name}'.")
                return
            print(f"{'ID':<16} {'STATUS':<16} {'CONTAINER':<24} {'CREATED AT'}")
            print("-" * 75)
            for d in deps:
                cont = d.get("active_container_id") or d.get("candidate_container_id") or "-"
                print(f"{d['id']:<16} {d['status']:<16} {cont:<24} {d['created_at']}")
        else:
            print(f"Failed to list deployments: {deps}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()