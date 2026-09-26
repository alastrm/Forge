import json
import subprocess
from forge.core.errors import ForgeError
from forge.proxy.base import Proxy


class ProxyError(ForgeError):
    """Raised when an operation on the proxy fails."""


class TraefikProxy(Proxy):
    def __init__(self, network: str = "forge-net", timeout: float = 60.0) -> None:
        self.network = network
        self.timeout = timeout
        self.proxy_name = "forge-proxy"

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
                subprocess.run(["docker", "rm", "-f", self.proxy_name], capture_output=True, timeout=10.0)
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
            "80:80",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-e",
            "DOCKER_API_VERSION=1.44",
            "traefik:latest",
            "--providers.docker=true",
            "--providers.docker.exposedbydefault=false",
            f"--providers.docker.network={self.network}",
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
        router_name = app_name
        service_name = app_name
        return {
            "traefik.enable": "true",
            f"traefik.http.routers.{router_name}.rule": f'Host("{domain}")',
            f"traefik.http.services.{service_name}.loadbalancer.server.port": str(port),
            "forge.app": app_name,
        }

    def remove_service(self, app_name: str) -> None:
        # Traefik Docker provider dynamically cleans up routes when containers are removed
        pass
