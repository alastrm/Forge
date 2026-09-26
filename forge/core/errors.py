class ForgeError(Exception):
    """Base domain exception for Forge platform."""


class ValidationError(ForgeError):
    """Raised when configuration, application name, or input data fails validation."""


class InvalidStateTransitionError(ForgeError):
    """Raised when attempting an illegal deployment state transition."""

    def __init__(self, current_status: str, target_status: str, message: str = "") -> None:
        msg = message or f"Cannot transition deployment from '{current_status}' to '{target_status}'"
        super().__init__(msg)
        self.current_status = current_status
        self.target_status = target_status


class EntityNotFoundError(ForgeError):
    """Raised when a requested database entity does not exist."""


class ConcurrencyError(ForgeError):
    """Raised when a concurrent modification or active deployment conflict occurs."""


class StorageError(ForgeError):
    """Raised when an underlying database operation fails."""


class PayloadTooLargeError(ForgeError):
    """Raised when an incoming HTTP request exceeds the maximum allowed body size."""
