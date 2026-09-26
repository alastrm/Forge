import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from forge.core.errors import ForgeError, ValidationError
from forge.runtime.base import ContainerRuntimeState, ExecResult, Runtime


class DockerRuntimeError(ForgeError):
    """Raised when an operation on DockerRuntime fails."""


class DockerRuntime(Runtime):
    def __init__(self, timeout: float = 120.0) -> None:
        self.timeout = timeout

    def ensure_network(self, network_name: str = "forge-net") -> None:
        inspect_cmd = ["docker", "network", "inspect", network_name]
        try:
            res = subprocess.run(
                inspect_cmd,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )
            if res.returncode == 0:
                return
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DockerRuntimeError(f"Failed to inspect network '{network_name}': {exc}") from exc

        create_cmd = ["docker", "network", "create", network_name]
        try:
            res = subprocess.run(
                create_cmd,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )
            if res.returncode != 0 and "already exists" not in res.stderr.lower():
                raise DockerRuntimeError(f"Failed to create network '{network_name}': {res.stderr.strip()}")
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DockerRuntimeError(f"Failed to create network '{network_name}': {exc}") from exc

    def build_image_stream(self, context_path: Path, tag: str) -> Iterator[str]:
        command = ["docker", "build", "-t", tag, str(context_path)]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except (FileNotFoundError, OSError) as exc:
            raise DockerRuntimeError(f"Failed to start docker build: {exc}") from exc

        assert process.stdout is not None
        try:
            for line in process.stdout:
                yield line
        finally:
            if process.stdout is not None and hasattr(process.stdout, "close"):
                process.stdout.close()
            return_code = process.wait()
            if return_code != 0:
                raise DockerRuntimeError(f"Docker build failed with exit code {return_code}")

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
        # SEC-11: Docker socket mount denied
        for key, val in labels.items():
            if "docker.sock" in key or "docker.sock" in val:
                raise ValidationError("Mounting docker.sock into application containers is strictly forbidden")

        # SEC-09: Docker hardening flags & quotas
        command = [
            "docker",
            "create",
            "--name",
            container_name,
            "--network",
            network,
            f"--restart={restart_policy}",
            "--cap-drop=ALL",
            "--cap-add=NET_BIND_SERVICE",
            "--security-opt=no-new-privileges:true",
            f"--memory={memory_limit}",
            f"--cpus={cpu_limit}",
            f"--pids-limit={pids_limit}",
        ]

        # Apply labels
        for k, v in labels.items():
            command.extend(["--label", f"{k}={v}"])

        env_file_path: Path | None = None
        # SEC-08: Zero-leak env file. Do NOT pass secrets on command line (-e KEY=VAL)
        if env_vars:
            fd, tmp_path_str = tempfile.mkstemp(prefix="forge_env_", suffix=".tmp")
            env_file_path = Path(tmp_path_str)
            try:
                # Set permissions to 0600 (owner read/write only)
                try:
                    os.chmod(env_file_path, 0o600)
                except OSError:
                    pass
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    for k, v in env_vars.items():
                        f.write(f"{k}={v}\n")
                command.extend(["--env-file", str(env_file_path)])
            except Exception:
                if env_file_path and env_file_path.is_file():
                    env_file_path.unlink(missing_ok=True)
                raise

        command.append(image_tag)

        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )
            if result.returncode != 0:
                raise DockerRuntimeError(
                    f"Failed to create container '{container_name}': {result.stderr.strip()}"
                )
            return container_name
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DockerRuntimeError(f"Error creating container '{container_name}': {exc}") from exc
        finally:
            # SEC-08: Ensure temporary env-file is purged immediately after creation
            if env_file_path and env_file_path.is_file():
                env_file_path.unlink(missing_ok=True)

    def start_container(self, container_id: str) -> None:
        command = ["docker", "start", container_id]
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )
            if result.returncode != 0:
                raise DockerRuntimeError(
                    f"Failed to start container '{container_id}': {result.stderr.strip()}"
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DockerRuntimeError(f"Error starting container '{container_id}': {exc}") from exc

    def stop_container(self, container_id: str, timeout: int = 10) -> None:
        command = ["docker", "stop", "-t", str(timeout), container_id]
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=max(self.timeout, float(timeout) + 5.0),
            )
            if result.returncode != 0:
                raise DockerRuntimeError(
                    f"Failed to stop container '{container_id}': {result.stderr.strip()}"
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DockerRuntimeError(f"Error stopping container '{container_id}': {exc}") from exc

    def remove_container(self, container_id: str, force: bool = True) -> None:
        command = ["docker", "rm"]
        if force:
            command.append("-f")
        command.append(container_id)
        try:
            subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DockerRuntimeError(f"Error removing container '{container_id}': {exc}") from exc

    def inspect_container(self, container_id: str) -> ContainerRuntimeState:
        command = ["docker", "inspect", container_id]
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )
            if result.returncode != 0:
                raise DockerRuntimeError(
                    f"Failed to inspect container '{container_id}': {result.stderr.strip()}"
                )
            data = json.loads(result.stdout)
            state = data[0]["State"]
            return ContainerRuntimeState(
                running=bool(state["Running"]),
                status=str(state["Status"]),
                exit_code=int(state["ExitCode"]),
            )
        except (json.JSONDecodeError, KeyError, IndexError, subprocess.TimeoutExpired, OSError) as exc:
            raise DockerRuntimeError(f"Malformed inspect for '{container_id}': {exc}") from exc

    def exec(self, container_id: str, cmd: list[str], timeout: float = 10.0) -> ExecResult:
        command = ["docker", "exec", container_id] + cmd
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            return ExecResult(
                exit_code=result.returncode,
                output=(result.stdout + result.stderr).strip(),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return ExecResult(exit_code=1, output=f"Exec failed: {exc}")

    def logs(self, container_id: str, tail: int = 100) -> str:
        command = ["docker", "logs", "--tail", str(tail), container_id]
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )
            return (result.stdout + result.stderr).strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DockerRuntimeError(f"Failed to fetch logs for '{container_id}': {exc}") from exc
