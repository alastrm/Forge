import re
from typing import Any

SENSITIVE_KEY_PATTERNS = (
    "secret",
    "token",
    "password",
    "auth",
    "private_key",
    "env_vars",
    "api_key",
)

BEARER_REGEX = re.compile(r"Bearer\s+([a-zA-Z0-9_\-\.]+)", re.IGNORECASE)
KEY_VAL_SECRET_REGEX = re.compile(
    r"(?i)(password|secret|token|api[_-]?key|access[_-]?token)\s*[:=]\s*([^\s,;]+)"
)


def scrub_dict(data: Any) -> Any:
    """Recursively scrub known sensitive fields from dictionaries and lists."""
    if isinstance(data, dict):
        scrubbed = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(p in k_lower for p in SENSITIVE_KEY_PATTERNS):
                scrubbed[k] = "[REDACTED]"
            else:
                scrubbed[k] = scrub_dict(v)
        return scrubbed
    elif isinstance(data, list):
        return [scrub_dict(item) for item in data]
    return data


def scrub_text(text: str, secrets: list[str] | set[str] | None = None) -> str:
    """Scrub sensitive secrets and known patterns from raw logs or text."""
    if not text:
        return text

    scrubbed = text

    # Scrub exact configured secret values (min length 3 to avoid wiping out single characters/numbers)
    if secrets:
        for secret in sorted(secrets, key=len, reverse=True):
            if secret and len(str(secret).strip()) >= 3:
                scrubbed = scrubbed.replace(str(secret), "[REDACTED]")

    # Scrub Bearer tokens
    scrubbed = BEARER_REGEX.sub("Bearer [REDACTED]", scrubbed)

    # Scrub KEY=VAL or KEY: VAL secrets in output
    scrubbed = KEY_VAL_SECRET_REGEX.sub(r"\1=[REDACTED]", scrubbed)

    return scrubbed
