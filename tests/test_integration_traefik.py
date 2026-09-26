import http.client
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from forge.proxy.traefik import TraefikProxy
from forge.runtime.docker import DockerRuntime


def is_docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=5.0)
        return res.returncode == 0
    except Exception:
        return False


@unittest.skipUnless(is_docker_available(), "Docker daemon is not running or available")
class TestTraefikRealIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.test_port = 18080
        self.proxy_name = "forge-test-traefik"
        self.app_name = "integ-web"
        self.domain = "integ-web.localhost"
        self.container_name = "integ-web-cont"
        self.containers_to_clean: list[str] = [self.container_name]

        # Clean any stale test containers from previous interrupted runs
        subprocess.run(["docker", "rm", "-f", self.proxy_name, self.container_name], capture_output=True)

        self.dyn_dir = Path.home() / ".forge" / "test_traefik_dyn"
        self.dyn_dir.mkdir(parents=True, exist_ok=True)
        for f in self.dyn_dir.glob("*"):
            f.unlink()

        self.runtime = DockerRuntime(timeout=30.0)
        self.network = "forge-net"
        self.runtime.ensure_network(self.network)

        self.proxy = TraefikProxy(
            network=self.network,
            dynamic_config_dir=self.dyn_dir,
            proxy_name=self.proxy_name,
            host_port=self.test_port,
            timeout=30.0,
        )
        self.proxy.ensure_proxy()

        # Ensure image is built
        list(self.runtime.build_image_stream(Path("test-app").resolve(), "forge-test-web:latest"))

    def tearDown(self) -> None:
        for c in self.containers_to_clean:
            try:
                self.runtime.remove_container(c, force=True)
            except Exception:
                pass
        self.proxy.stop_proxy()
        for f in self.dyn_dir.glob("*"):
            try:
                f.unlink()
            except Exception:
                pass

    def test_candidate_isolation_and_dynamic_promotion_with_real_traefik(self) -> None:
        # 1. Start a real backend application container in Docker
        port = 8000
        labels = self.proxy.generate_labels(
            app_name=self.app_name,
            domain=self.domain,
            port=port,
            is_candidate=True,
        )
        self.assertEqual(labels["traefik.enable"], "false")

        # Run container with full security baseline
        self.runtime.create_container(
            image_tag="forge-test-web:latest",
            container_name=self.container_name,
            network=self.network,
            labels=labels,
            env_vars={"APP_ENV": "test"},
            port=port,
        )
        self.runtime.start_container(self.container_name)

        # 2. Query Traefik for candidate before promotion -> Traefik MUST return 404 (candidate receives 0 traffic)
        time.sleep(2.0)
        conn = http.client.HTTPConnection("127.0.0.1", self.test_port, timeout=5.0)
        conn.request("GET", "/", headers={"Host": self.domain})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 404)
        conn.close()

        # 3. Promote candidate to production
        self.proxy.promote_service(
            app_name=self.app_name,
            domain=self.domain,
            container_name=self.container_name,
            port=port,
        )

        # 4. Wait for Traefik dynamic file provider to reload configuration
        promoted_ok = False
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.test_port, timeout=3.0)
                conn.request("GET", "/", headers={"Host": self.domain})
                resp = conn.getresponse()
                resp.read()
                if resp.status == 200:
                    promoted_ok = True
                    conn.close()
                    break
                conn.close()
            except Exception:
                pass
            time.sleep(1.0)

        self.assertTrue(promoted_ok, "Traefik failed to dynamically route traffic to promoted container")

        # 5. Remove service configuration -> Traefik reverts to 404
        self.proxy.remove_service(self.app_name)
        removed_ok = False
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.test_port, timeout=3.0)
                conn.request("GET", "/", headers={"Host": self.domain})
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status == 404:
                    removed_ok = True
                    break
            except Exception:
                pass
            time.sleep(1.0)

        self.assertTrue(removed_ok, "Traefik failed to unroute service after removal")


if __name__ == "__main__":
    unittest.main()
