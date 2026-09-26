import unittest

from forge.core.errors import ValidationError
from forge.core.models import (
    Application,
    Event,
    EventKind,
    validate_app_name,
    validate_domain,
    validate_health_check_path,
)


class TestSecurityValidation(unittest.TestCase):
    def test_app_name_valid(self) -> None:
        valid_names = ["my-app", "app123", "service_v1", "a", "cool-service-2026"]
        for name in valid_names:
            validate_app_name(name)

    def test_app_name_injection_and_invalid_rejected(self) -> None:
        # SEC-04: Rejects characters capable of path traversal, shell injection, or traefik label breaking
        invalid_names = [
            "My-App",  # uppercase forbidden
            "app/name",  # path traversal
            "../etc",  # path traversal
            'app"name',  # quote injection
            "app name",  # space
            "-leading-dash",  # leading dash forbidden
            "_leading_underscore",  # leading underscore forbidden
            "app;rm -rf",  # command separator
            "app\nline",  # newline
        ]
        for name in invalid_names:
            with self.assertRaises(ValidationError, msg=f"Should reject invalid name '{name}'"):
                validate_app_name(name)

    def test_domain_valid(self) -> None:
        valid_domains = ["app.localhost", "api.example.com", "sub-domain.service.internal", "localhost"]
        for domain in valid_domains:
            validate_domain(domain)

    def test_domain_traefik_rule_injection_rejected(self) -> None:
        # SEC-04: Prevents injecting quotes, closing parentheses or boolean operators into Traefik labels
        malicious_domains = [
            'app.localhost") || PathPrefix("/evil',
            'domain.com" || Host("other.com',
            "domain.com; evil.com",
            "domain.com\nHost(evil.com)",
            "domain name.com",
            "domain'quote.com",
        ]
        for domain in malicious_domains:
            with self.assertRaises(ValidationError, msg=f"Should reject malicious domain '{domain}'"):
                validate_domain(domain)

    def test_health_check_valid_paths(self) -> None:
        valid_paths = ["/health", "/healthz", "/api/v1/ping", "/live_check", "/"]
        for path in valid_paths:
            validate_health_check_path(path)

    def test_health_check_ssrf_path_rejected(self) -> None:
        # SEC-05: Prevents SSRF vectors, schemes, IP targets, credentials, query parameters
        malicious_paths = [
            "http://169.254.169.254/latest/meta-data/",
            "https://internal-vault:8200/v1/secret",
            "ftp://files.internal/",
            "@attacker.com",
            "//attacker.com",
            "/health?param=value",  # query param disallowed
            "/health#fragment",  # fragment disallowed
            "/health\r\nHost: evil.com",  # CRLF injection
            "relative-path-without-slash",
        ]
        for path in malicious_paths:
            with self.assertRaises(ValidationError, msg=f"Should reject malicious health check path '{path}'"):
                validate_health_check_path(path)

    def test_event_payload_does_not_contain_secrets(self) -> None:
        # SEC-06: Verifies that attempting to record raw environment dictionary into Event payload is blocked
        with self.assertRaises(ValidationError):
            Event(
                id="evt-1",
                app_id="app-1",
                event_kind=EventKind.DEPLOYMENT_CREATED,
                payload={"env": {"SECRET_KEY": "raw-password-123"}},
            )

        with self.assertRaises(ValidationError):
            Event(
                id="evt-2",
                app_id="app-1",
                event_kind=EventKind.DEPLOYMENT_CREATED,
                payload={"env_vars": {"DATABASE_URL": "postgres://user:pass@db:5432"}},
            )


if __name__ == "__main__":
    unittest.main()
