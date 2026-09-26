import abc


class Proxy(abc.ABC):
    @abc.abstractmethod
    def ensure_proxy(self) -> None:
        """Ensure the reverse proxy is running and configured."""

    @abc.abstractmethod
    def generate_labels(
        self,
        app_name: str,
        domain: str,
        port: int,
        is_candidate: bool = False,
    ) -> dict[str, str]:
        """Generate proxy routing labels for container."""

    @abc.abstractmethod
    def remove_service(self, app_name: str) -> None:
        """Remove proxy routing for an application."""
