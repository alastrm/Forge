import json
import subprocess
from pathlib import Path
from forge.core.errors import ForgeError
from forge.proxy.base import Proxy


class ProxyError(ForgeError):
    """Raised when an operation on the proxy fails."""


class TraefikProxy(Proxy):
    def __init__(
        self,
        network: str = "forge-net",
        dynamic_config_dir: Path | str | None = None,
        timeout: float = 60.0,
        proxy_name: str = "forge-proxy",
        host_port: int = 80,
    ) -> None:
        self.network = network
        self.timeout = timeout
        self.proxy_name = proxy_name
        self.host_port = host_port
        if dynamic_config_dir is None:
            self.dynamic_config_dir = Path.home() / ".forge" / "traefik_dynamic"
        else:
            self.dynamic_config_dir = Path(dynamic_config_dir)
        self.dynamic_config_dir.mkdir(parents=True, exist_ok=True)

    def stop_proxy(self) -> None:
        """Stop and remove the Traefik proxy container."""
        try:
            subprocess.run(["docker", "rm", "-f", self.proxy_name], capture_output=True, timeout=10.0)
        except Exception:
            pass

    def ensure_proxy(self) -> None:
        inspect_cmd = ["docker", "inspect", self.proxy_name]
        try:
            res = subprocess.run(
                inspect_cmd,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )
            if res.returncode == 0:
                data = json.loads(res.stdout)
                if data and data[0]["State"]["Running"]:
                    return
                # Stale stopped proxy container, remove it
        except Exception:
            pass

        try:
            subprocess.run(["docker", "network", "create", self.network], capture_output=True, timeout=10.0)
        except Exception:
            pass

        run_cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self.proxy_name,
            "--restart",
            "unless-stopped",
            "--network",
            self.network,
            "-p",
            f"{self.host_port}:80",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            f"{self.dynamic_config_dir.as_posix()}:/etc/traefik/dynamic",
            "-e",
            "DOCKER_API_VERSION=1.44",
            "traefik:latest",
            "--providers.docker=true",
            "--providers.docker.exposedbydefault=false",
            f"--providers.docker.network={self.network}",
            "--providers.file.directory=/etc/traefik/dynamic",
            "--providers.file.watch=true",
            "--entrypoints.web.address=:80",
        ]

        try:
            res = subprocess.run(
                run_cmd,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )
            if res.returncode != 0:
                raise ProxyError(f"Failed to start Traefik proxy: {res.stderr.strip()}")
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ProxyError(f"Error starting Traefik proxy: {exc}") from exc

    def generate_labels(
        self,
        app_name: str,
        domain: str,
        port: int,
        is_candidate: bool = False,
    ) -> dict[str, str]:
        # Priority 1: Traefik candidate must never receive traffic before promotion.
        # Candidate containers explicitly disable Traefik routing to prevent premature traffic ingestion.
        if is_candidate:
            return {
                "traefik.enable": "false",
                "forge.app": app_name,
                "forge.candidate": "true",
            }

        router_name = app_name
        service_name = app_name
        return {
            "traefik.enable": "true",
            f"traefik.http.routers.{router_name}.rule": f"Host(`{domain}`)",
            f"traefik.http.services.{service_name}.loadbalancer.server.port": str(port),
            "forge.app": app_name,
            "forge.candidate": "false",
        }

    def promote_service(
        self,
        app_name: str,
        domain: str,
        container_name: str,
        port: int,
    ) -> None:
        """Dynamically configure Traefik to route production traffic to the promoted container."""
        yaml_content = (
            f"http:\n"
            f"  routers:\n"
            f"    {app_name}:\n"
            f'      rule: "Host(`{domain}`)"\n'
            f"      service: {app_name}\n"
            f"      entryPoints:\n"
            f"        - web\n"
            f"  services:\n"
            f"    {app_name}:\n"
            f"      loadBalancer:\n"
            f"        servers:\n"
            f'          - url: "http://{container_name}:{port}"\n'
        )
        cfg_file = self.dynamic_config_dir / f"{app_name}.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")

        # Trigger container inotify reload for environments (e.g. Docker on Windows) where NTFS host events don't cross into the Linux container
        try:
            subprocess.run(
                ["docker", "exec", self.proxy_name, "touch", f"/etc/traefik/dynamic/{app_name}.yaml"],
                capture_output=True,
                timeout=5.0,
            )
            subprocess.run(
                ["docker", "exec", self.proxy_name, "touch", "/etc/traefik/dynamic"],
                capture_output=True,
                timeout=5.0,
            )
        except Exception:
            pass

    def remove_service(self, app_name: str) -> None:
        """Remove dynamic proxy routing for an application."""
        cfg_file = self.dynamic_config_dir / f"{app_name}.yaml"
        if cfg_file.is_file():
            cfg_file.unlink(missing_ok=True)
        try:
            subprocess.run(
                ["docker", "exec", self.proxy_name, "rm", "-f", f"/etc/traefik/dynamic/{app_name}.yaml"],
                capture_output=True,
                timeout=5.0,
            )
            subprocess.run(
                ["docker", "exec", self.proxy_name, "touch", "/etc/traefik/dynamic"],
                capture_output=True,
                timeout=5.0,
            )
        except Exception:
            pass
