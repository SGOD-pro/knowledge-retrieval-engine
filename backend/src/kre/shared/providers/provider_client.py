import os
import time
import threading


class RateLimiter:
    def __init__(self, calls: int, period: float):
        self.calls = calls
        self.period = period
        self.timestamps = []
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            self.timestamps = [t for t in self.timestamps if now - t < self.period]
            if len(self.timestamps) >= self.calls:
                sleep_time = self.period - (now - self.timestamps[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
                now = time.time()
                self.timestamps = [t for t in self.timestamps if now - t < self.period]
            self.timestamps.append(now)

_limiters = {}
_limiters_lock = threading.Lock()

def enforce_rate_limit(model_id: str):
    """Enforces a maximum of 15 calls per minute for each model."""
    with _limiters_lock:
        if model_id not in _limiters:
            _limiters[model_id] = RateLimiter(15, 60.0)
        limiter = _limiters[model_id]
    limiter.wait()


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
