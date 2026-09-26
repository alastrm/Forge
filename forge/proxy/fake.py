from forge.proxy.base import Proxy


class FakeProxy(Proxy):
    def __init__(self) -> None:
        self.ensured: bool = False
        self.configured_routes: dict[str, dict[str, str]] = {}
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
        labels = {
            "fake.proxy.app": app_name,
            "fake.proxy.domain": domain,
            "fake.proxy.port": str(port),
            "fake.proxy.candidate": str(is_candidate).lower(),
        }
        self.configured_routes[app_name] = labels
        return labels

    def remove_service(self, app_name: str) -> None:
        self.removed_services.append(app_name)
        self.configured_routes.pop(app_name, None)
