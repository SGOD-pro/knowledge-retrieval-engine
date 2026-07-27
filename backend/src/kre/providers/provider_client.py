import os


class ConfigurationError(Exception):
    """Raised when environment or provider configuration rules are violated."""

    pass


class ProviderMismatchError(Exception):
    """Raised when query provider does not match corpus embedding provider."""

    pass


def get_active_provider() -> str:
    """Return active provider configuration ("dev" or "prod").

    Enforces Rule 29: MODEL_PROVIDER=dev is prohibited in production environment.
    """
    environment = os.environ.get("ENVIRONMENT", "development").lower()
    provider = os.environ.get("MODEL_PROVIDER", "dev").lower()

    if environment == "production" and provider == "dev":
        raise ConfigurationError("MODEL_PROVIDER=dev is strictly prohibited in production environment (Rule 29).")

    return provider
