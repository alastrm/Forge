from typing import Any

from forge.proxy.base import Proxy


class FakeProxy(Proxy):
    def __init__(self) -> None:
        self.ensured: bool = False
        self.configured_routes: dict[str, dict[str, str]] = {}
        self.promoted_services: dict[str, dict[str, Any]] = {}
        self.promotion_history: list[dict[str, Any]] = []
        self.removed_services: list[str] = []

    def ensure_proxy(self) -> None:
        self.ensured = True

    def generate_labels(
        self,
        app_name: str,
        domain: str,
        port: int,
        is_candidate: bool = False,
    ) -> dict[str, str]:
        if is_candidate:
            labels = {
                "traefik.enable": "false",
                "fake.proxy.app": app_name,
                "fake.proxy.candidate": "true",
            }
        else:
            labels = {
                "traefik.enable": "true",
                "fake.proxy.app": app_name,
                "fake.proxy.domain": domain,
                "fake.proxy.port": str(port),
                "fake.proxy.candidate": "false",
            }
        self.configured_routes[app_name] = labels
        return labels

    def promote_service(
        self,
        app_name: str,
        domain: str,
        container_name: str,
        port: int,
    ) -> None:
        record = {
            "app_name": app_name,
            "domain": domain,
            "container_name": container_name,
            "port": port,
        }
        self.promoted_services[app_name] = record
        self.promotion_history.append(record)

    def remove_service(self, app_name: str) -> None:
        self.removed_services.append(app_name)
        self.configured_routes.pop(app_name, None)
        self.promoted_services.pop(app_name, None)
