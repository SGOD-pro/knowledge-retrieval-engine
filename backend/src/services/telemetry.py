"""Request-scoped provider call telemetry.

Uses contextvars.ContextVar to isolate call counts per request, ensuring
concurrent requests cannot mix counters.
"""

from contextvars import ContextVar
from dataclasses import asdict, dataclass


@dataclass
class RequestTelemetry:
    bedrock_embedding_calls: int = 0
    bge_lambda_calls: int = 0
    reranker_remote_calls: int = 0
    generation_calls: int = 0
    structured_aggregate_calls: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


_current_telemetry: ContextVar[RequestTelemetry | None] = ContextVar(
    "request_telemetry", default=None
)


def init_request_telemetry() -> RequestTelemetry:
    """Initialize a fresh request telemetry context for the current request/coroutine."""
    telemetry = RequestTelemetry()
    _current_telemetry.set(telemetry)
    return telemetry


def get_request_telemetry() -> RequestTelemetry:
    """Get active request telemetry, creating one if not yet initialized."""
    telemetry = _current_telemetry.get()
    if telemetry is None:
        telemetry = RequestTelemetry()
        _current_telemetry.set(telemetry)
    return telemetry


def record_bedrock_embedding() -> None:
    telemetry = _current_telemetry.get()
    if telemetry is not None:
        telemetry.bedrock_embedding_calls += 1


def record_bge_lambda() -> None:
    telemetry = _current_telemetry.get()
    if telemetry is not None:
        telemetry.bge_lambda_calls += 1


def record_reranker() -> None:
    telemetry = _current_telemetry.get()
    if telemetry is not None:
        telemetry.reranker_remote_calls += 1


def record_generation() -> None:
    telemetry = _current_telemetry.get()
    if telemetry is not None:
        telemetry.generation_calls += 1


def record_structured_aggregate() -> None:
    telemetry = _current_telemetry.get()
    if telemetry is not None:
        telemetry.structured_aggregate_calls += 1

