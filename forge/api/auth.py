import hmac
from typing import Optional


class ApiAuth:
    """Timing-safe Bearer token authentication handler for Forge API."""

    def __init__(self, token: Optional[str] = None) -> None:
        self._token = token

    @property
    def is_enabled(self) -> bool:
        return bool(self._token)

    def verify_token(self, auth_header: Optional[str]) -> bool:
        """Verify the Authorization header using constant-time comparison to prevent timing attacks."""
        if not self.is_enabled:
            return True
        if not auth_header:
            return False

        parts = auth_header.strip().split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False

        provided_token = parts[1].strip()
        assert self._token is not None
        return hmac.compare_digest(provided_token, self._token)
