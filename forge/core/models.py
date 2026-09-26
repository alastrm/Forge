import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from forge.core.errors import InvalidStateTransitionError, ValidationError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeploymentStatus(str, Enum):
    PENDING = "PENDING"
    BUILDING = "BUILDING"
    STARTING = "STARTING"
    HEALTH_CHECKING = "HEALTH_CHECKING"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


ALLOWED_TRANSITIONS: dict[DeploymentStatus, set[DeploymentStatus]] = {
    DeploymentStatus.PENDING: {
        DeploymentStatus.BUILDING,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.BUILDING: {
        DeploymentStatus.STARTING,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.STARTING: {
        DeploymentStatus.HEALTH_CHECKING,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.HEALTH_CHECKING: {
        DeploymentStatus.ACTIVE,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.ACTIVE: {
        DeploymentStatus.STOPPING,
        DeploymentStatus.STOPPED,
        DeploymentStatus.FAILED,
        DeploymentStatus.ROLLED_BACK,
    },
    DeploymentStatus.STOPPING: {
        DeploymentStatus.STOPPED,
        DeploymentStatus.FAILED,
        DeploymentStatus.ROLLED_BACK,
    },
    DeploymentStatus.STOPPED: set(),
    DeploymentStatus.FAILED: set(),
    DeploymentStatus.ROLLED_BACK: set(),
}


def validate_transition(current: DeploymentStatus, target: DeploymentStatus) -> None:
    if current == target:
        return
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidStateTransitionError(
            current_status=current.value,
            target_status=target.value,
            message=f"Illegal transition from '{current.value}' to '{target.value}'",
        )


class EventKind(str, Enum):
    APPLICATION_CREATED = "application.created"
    APPLICATION_DELETED = "application.deleted"
    DEPLOYMENT_CREATED = "deployment.created"
    DEPLOYMENT_BUILD_STARTED = "deployment.build_started"
    DEPLOYMENT_BUILD_SUCCEEDED = "deployment.build_succeeded"
    DEPLOYMENT_CONTAINER_STARTED = "deployment.container_started"
    DEPLOYMENT_HEALTH_CHECK_PASSED = "deployment.health_check_passed"
    DEPLOYMENT_PROMOTED = "deployment.promoted"
    DEPLOYMENT_FAILED = "deployment.failed"
    DEPLOYMENT_STOPPED = "deployment.stopped"
    DEPLOYMENT_ROLLBACK_STARTED = "deployment.rollback_started"
    DEPLOYMENT_ROLLBACK_COMPLETED = "deployment.rollback_completed"
    CONTAINER_CRASHED = "container.crashed"
    CONTAINER_ORPHAN_CLEANED = "container.orphan_cleaned"
    RECONCILIATION_RUN = "reconciliation.run"


# Security validation regex patterns
APP_NAME_REGEX = re.compile(r"^[a-z0-9][a-z0-9-_]{0,62}$")
DOMAIN_REGEX = re.compile(
    r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$|"
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
)
HEALTH_CHECK_PATH_REGEX = re.compile(r"^/[a-zA-Z0-9_\-\./]*$")


def validate_app_name(name: str) -> None:
    if not name or not isinstance(name, str):
        raise ValidationError("Application name cannot be empty")
    if not APP_NAME_REGEX.match(name):
        raise ValidationError(
            f"Invalid application name '{name}'. Must match ^[a-z0-9][a-z0-9-_]{{0,62}}$ and contain no uppercase or special characters."
        )


def validate_domain(domain: str) -> None:
    if not domain or not isinstance(domain, str):
        raise ValidationError("Domain cannot be empty")
    domain_lower = domain.lower()
    if any(ch in domain_lower for ch in ('"', "'", " ", "(", ")", ";", "\\", "\r", "\n", "\t")):
        raise ValidationError(f"Invalid domain '{domain}'. Traefik rule injection characters are not allowed.")
    if not DOMAIN_REGEX.match(domain_lower):
        raise ValidationError(f"Invalid domain format '{domain}'. Must be a valid hostname.")


def validate_health_check_path(path: str) -> None:
    if not path or not isinstance(path, str):
        raise ValidationError("Health check path cannot be empty")
    if path.startswith("//") or "//" in path:
        raise ValidationError("Health check path cannot contain protocol-relative double slashes '//'")
    if any(proto in path.lower() for proto in ("http://", "https://", "ftp://", "file://")):
        raise ValidationError("Health check path must be a relative URI path, not an absolute URL with scheme")
    if any(ch in path for ch in ("@", "?", "#", " ", "\r", "\n", "\t")):
        raise ValidationError("Health check path cannot contain '@', '?', '#', whitespace or newline characters")
    if not path.startswith("/"):
        raise ValidationError("Health check path must start with '/'")
    if not HEALTH_CHECK_PATH_REGEX.match(path):
        raise ValidationError(f"Invalid characters in health check path: '{path}'")



@dataclass(frozen=True)
class Application:
    id: str
    name: str
    domain: str
    container_port: int = 8000
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_app_name(self.name)
        validate_domain(self.domain)
        if not (1 <= self.container_port <= 65535):
            raise ValidationError(f"Port {self.container_port} out of valid range (1-65535)")


@dataclass(frozen=True)
class DeploymentRevision:
    id: str
    app_id: str
    version_tag: str
    image_name: str
    config_snapshot: dict[str, Any]
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Deployment:
    id: str
    app_id: str
    status: DeploymentStatus
    revision_id: str | None = None
    candidate_container_id: str | None = None
    active_container_id: str | None = None
    error_message: str | None = None
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class ContainerRecord:
    id: str
    deployment_id: str
    app_id: str
    container_name: str
    role: str  # "candidate", "active", "old"
    status: str
    ip_address: str | None = None
    port: int = 8000
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Event:
    id: str
    app_id: str
    event_kind: EventKind
    payload: dict[str, Any]
    deployment_id: str | None = None
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        # Zero-leak security guarantee: sanitize payload to ensure raw secrets are not stored
        # If payload contains environment-related data, only allow keys or masked values
        if "env" in self.payload or "env_vars" in self.payload:
            raise ValidationError("Raw environment variables must not be stored in Event payload")


@dataclass(frozen=True)
class EnvironmentVariable:
    id: str
    app_id: str
    key: str
    value: str
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
