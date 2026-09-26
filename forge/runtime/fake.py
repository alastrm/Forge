import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from forge.core.errors import ForgeError
from forge.runtime.base import ContainerRuntimeState, ExecResult, Runtime


class FakeRuntimeError(ForgeError):
    """Raised when an operation on FakeRuntime fails."""


class FakeRuntime(Runtime):
    def __init__(self) -> None:
        self.networks: set[str] = set()
        self.built_images: list[str] = []
        self.containers: dict[str, dict[str, Any]] = {}
        self.stopped_containers: list[tuple[str, int]] = []
        self.removed_containers: list[str] = []

        # Failure injection flags for testing
        self.fail_build: bool = False
        self.fail_create: bool = False
        self.fail_start: bool = False
        self.fail_inspect_unhealthy: bool = False
        self.fail_health_exec: bool = False
        self.fail_stop: bool = False

    def ensure_network(self, network_name: str) -> None:
        self.networks.add(network_name)

    def build_image_stream(self, context_path: Path, tag: str) -> Iterator[str]:
        if self.fail_build:
            yield "Step 1/2: FROM base\n"
            yield "Error: fake build failure\n"
            raise FakeRuntimeError("Docker build failed")
        yield f"Building {tag} from {context_path}\n"
        yield "Successfully built image\n"
        self.built_images.append(tag)

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
        if self.fail_create:
            raise FakeRuntimeError(f"Failed to create container '{container_name}'")

        cont_id = f"cid-{uuid.uuid4().hex[:8]}"
        self.containers[container_name] = {
            "id": cont_id,
            "name": container_name,
            "image": image_tag,
            "network": network,
            "labels": labels,
            "env_vars": dict(env_vars),
            "port": port,
            "restart_policy": restart_policy,
            "memory_limit": memory_limit,
            "cpu_limit": cpu_limit,
            "pids_limit": pids_limit,
            "running": False,
            "status": "created",
            "exit_code": 0,
            "logs": f"Container {container_name} created",
        }
        return container_name

    def start_container(self, container_id: str) -> None:
        if self.fail_start:
            if container_id in self.containers:
                self.containers[container_id]["running"] = False
                self.containers[container_id]["status"] = "exited"
                self.containers[container_id]["exit_code"] = 1
            raise FakeRuntimeError(f"Failed to start container '{container_id}'")

        if container_id not in self.containers:
            raise FakeRuntimeError(f"Container '{container_id}' not found")

        self.containers[container_id]["running"] = True
        self.containers[container_id]["status"] = "running"
        self.containers[container_id]["exit_code"] = 0

    def stop_container(self, container_id: str, timeout: int = 10) -> None:
        self.stopped_containers.append((container_id, timeout))
        if self.fail_stop:
            raise FakeRuntimeError(f"Failed to stop container '{container_id}'")
        if container_id in self.containers:
            self.containers[container_id]["running"] = False
            self.containers[container_id]["status"] = "exited"

    def remove_container(self, container_id: str, force: bool = True) -> None:
        self.removed_containers.append(container_id)
        self.containers.pop(container_id, None)

    def inspect_container(self, container_id: str) -> ContainerRuntimeState:
        if container_id not in self.containers:
            raise FakeRuntimeError(f"Container '{container_id}' not found")
        if self.fail_inspect_unhealthy:
            return ContainerRuntimeState(running=False, status="exited", exit_code=137)
        data = self.containers[container_id]
        return ContainerRuntimeState(
            running=data["running"],
            status=data["status"],
            exit_code=data["exit_code"],
        )

    def exec(self, container_id: str, cmd: list[str], timeout: float = 10.0) -> ExecResult:
        if self.fail_health_exec:
            return ExecResult(exit_code=1, output="Connection refused")
        return ExecResult(exit_code=0, output="OK")

    def logs(self, container_id: str, tail: int = 100) -> str:
        if container_id in self.containers:
            return str(self.containers[container_id].get("logs", ""))
        return ""

    def list_containers(self, label_filters: dict[str, str] | None = None) -> list[str]:
        result: list[str] = []
        for name, data in self.containers.items():
            if label_filters is None:
                result.append(name)
                continue
            labels = data.get("labels", {})
            if all(labels.get(k) == v for k, v in label_filters.items()):
                result.append(name)
        return result
