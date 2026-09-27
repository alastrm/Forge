import abc
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContainerRuntimeState:
    running: bool
    status: str
    exit_code: int


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str


class Runtime(abc.ABC):
    @abc.abstractmethod
    def ensure_network(self, network_name: str) -> None:
        """Ensure an isolated container bridge network exists."""

    @abc.abstractmethod
    def build_image_stream(self, context_path: Path, tag: str) -> Iterator[str]:
        """Build a container image and stream output lines."""

    @abc.abstractmethod
    def create_container(
        self,
        image_tag: str,
        container_name: str,
        network: str,
        labels: dict[str, str],
        env_vars: dict[str, str],
        port: int,
        restart_policy: str = "no",
        memory_limit: str = "512m",
        cpu_limit: str = "1.0",
        pids_limit: int = 150,
    ) -> str:
        """Create a hardened container without starting it, returning container identifier."""

    @abc.abstractmethod
    def start_container(self, container_id: str) -> None:
        """Start a previously created container."""

    @abc.abstractmethod
    def stop_container(self, container_id: str, timeout: int = 10) -> None:
        """Gracefully stop a container with SIGTERM and timeout."""

    @abc.abstractmethod
    def remove_container(self, container_id: str, force: bool = True) -> None:
        """Remove a container."""

    @abc.abstractmethod
    def inspect_container(self, container_id: str) -> ContainerRuntimeState:
        """Inspect runtime state of a container."""

    @abc.abstractmethod
    def exec(self, container_id: str, cmd: list[str], timeout: float = 10.0) -> ExecResult:
        """Execute a command inside the container."""

    @abc.abstractmethod
    def logs(self, container_id: str, tail: int = 100) -> str:
        """Fetch container logs."""

    @abc.abstractmethod
    def logs_stream(self, container_id: str, tail: int = 100) -> Iterator[str]:
        """Stream container logs line by line."""

    @abc.abstractmethod
    def list_containers(self, label_filters: dict[str, str] | None = None) -> list[str]:
        """List container names, optionally filtered by labels."""

    @abc.abstractmethod
    def prune_images(self) -> str:
        """Prune dangling or unused container images, returning summary output."""

