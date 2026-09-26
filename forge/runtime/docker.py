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


def validate_container_security(
    network: str,
    privileged: bool = False,
    mounts: list[str] | None = None,
    cap_add: list[str] | None = None,
    devices: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> None:
    """Validate that container configuration adheres to strict security baselines:
    - No privileged mode
    - No host / container networking
    - No device mapping
    - No dangerous capabilities (only NET_BIND_SERVICE allowed)
    - No dangerous host mounts (docker.sock, root, etc.)
    """
    if privileged:
        raise ValidationError("Privileged mode is strictly forbidden for application containers")

    if network in ("host", "none") or network.startswith("container:"):
        raise ValidationError(
            f"Forbidden network mode '{network}'. Only isolated bridge networks (e.g. forge-net) are allowed."
        )

    if devices:
        raise ValidationError("Direct host device access is strictly forbidden for application containers")

    allowed_caps = {"NET_BIND_SERVICE"}
    if cap_add:
        for cap in cap_add:
            cap_upper = cap.upper().removeprefix("CAP_")
            if cap_upper == "ALL" or cap_upper not in allowed_caps:
                raise ValidationError(
                    f"Forbidden capability '{cap}'. Application containers only permit NET_BIND_SERVICE."
                )

    forbidden_mount_targets = (
        "docker.sock",
        "/var/run/docker.sock",
        "//./pipe/docker_engine",
        "/etc",
        "/proc",
        "/sys",
        "/root",
        "/bin",
        "/sbin",
        "/usr",
        "/var/run",
        "c:\\windows",
        "c:\\program files",
    )
    if mounts:
        for m in mounts:
            parts = m.split(":")
            if len(parts) >= 2 and len(parts[0]) == 1 and parts[0].isalpha() and parts[1].startswith(("\\", "/")):
                host_path = parts[0] + ":" + parts[1]
            else:
                host_path = parts[0]

            if not os.path.isabs(host_path):
                raise ValidationError(f"Mount host source must be an absolute path: '{m}'")

            m_lower = m.lower()
            for forbidden in forbidden_mount_targets:
                if forbidden in m_lower:
                    raise ValidationError(
                        f"Mounting '{m}' is strictly forbidden: violates container boundary isolation ({forbidden})"
                    )

    if labels:
        for key, val in labels.items():
            if "docker.sock" in str(key).lower() or "docker.sock" in str(val).lower():
                raise ValidationError("Mounting or referencing docker.sock is strictly forbidden")


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
        privileged: bool = False,
        mounts: list[str] | None = None,
        cap_add: list[str] | None = None,
        devices: list[str] | None = None,
    ) -> str:
        # Priority 6: Real validation of mounts, privileged mode, capabilities, devices and host networking.
        validate_container_security(
            network=network,
            privileged=privileged,
            mounts=mounts,
            cap_add=cap_add,
            devices=devices,
            labels=labels,
        )

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

        if mounts:
            for m in mounts:
                command.extend(["-v", m])

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
            # Priority 6: Real validation of runtime attributes from Docker inspect data
            host_config = data[0].get("HostConfig", {})
            if host_config.get("Privileged", False):
                raise ValidationError(f"Security violation: Container '{container_id}' is running in privileged mode")
            if host_config.get("NetworkMode") == "host":
                raise ValidationError(f"Security violation: Container '{container_id}' is using host networking")
            for bind in host_config.get("Binds") or []:
                if "docker.sock" in str(bind).lower():
                    raise ValidationError(f"Security violation: Container '{container_id}' has mounted docker.sock: {bind}")
            if host_config.get("Devices"):
                raise ValidationError(f"Security violation: Container '{container_id}' has direct host device access")

            state = data[0]["State"]
            return ContainerRuntimeState(
                running=bool(state["Running"]),
                status=str(state["Status"]),
                exit_code=int(state["ExitCode"]),
            )
        except ValidationError:
            raise
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

    def list_containers(self, label_filters: dict[str, str] | None = None) -> list[str]:
        command = ["docker", "ps", "-a", "--format", "{{.Names}}"]
        if label_filters:
            for k, v in label_filters.items():
                command.extend(["--filter", f"label={k}={v}"])
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )
            if result.returncode != 0:
                raise DockerRuntimeError(f"Failed to list containers: {result.stderr.strip()}")
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DockerRuntimeError(f"Failed to list containers: {exc}") from exc
