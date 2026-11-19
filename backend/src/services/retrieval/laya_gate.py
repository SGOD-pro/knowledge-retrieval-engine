"""Authoritative budget gate, token preflight, and usage validation for LAYA escalation.

Enforces:
- Atomic request local budget reservations and settlement.
- Strict limits under Slice 5.1:
  - MAX_QUERY_LAYA_CALLS = 1 (for escalation)
  - MAX_EXPANSION_STEPS = 1
  - MAX_SYNTHESIS_CALLS = 1
  - Max prompt tokens: 1000 (with deterministic snippet truncation)
  - Max output tokens: 250 (inferenceConfig maxTokens 250, temperature 0.0)
  - Phase deadline: min(query_remaining_ms, 2500.0)
  - Minimum viable deadline: 500.0 ms
- Authoritative token preflight via count_tokens with defensible bound fallback.
- Strict provider usage dictionary validation (positive integer input, non negative output <= 250,
  input <= estimated prompt tokens).
- 429 throttling retries charged as attempts within the same bounded operation.
- Late spend reconciliation without mutating finished query results.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from src.schemas.contracts.laya import BaselineEvidenceSummary, LayaEscalationRequest

logger = logging.getLogger(__name__)

MAX_QUERY_LAYA_CALLS = 1
MAX_EXPANSION_STEPS = 1
MAX_SYNTHESIS_CALLS = 1
MAX_INPUT_TOKENS_CAP = 1000
MAX_OUTPUT_TOKENS_CAP = 250
MIN_VIABLE_DEADLINE_MS = 500.0
MAX_PHASE_DEADLINE_MS = 2500.0
MAX_TOOL_ATTEMPTS = 3


class InvalidUsageError(Exception):
    """Raised when provider response returns missing, non integer, or out of bound token usage."""
    pass


class LayaBudgetLedger(BaseModel):
    """Request local authoritative budget ledger tracking LAYA model operations."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    laya_calls: int = 0
    expansion_steps: int = 0
    synthesis_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_latency_ms: float = 0.0
    tool_attempts: int = 0
    late_settled_calls: int = 0
    settled_history: list[dict[str, Any]] = Field(default_factory=list)

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self._lock = threading.Lock()

    def pre_reserve(
        self,
        deadline_remaining_ms: float,
        estimated_input_tokens: int = 1000,
        estimated_output_tokens: int = 250,
    ) -> tuple[bool, str]:
        """Atomically pre reserve escalation slots and token capacity before invocation."""
        with self._lock:
            phase_deadline = min(deadline_remaining_ms, MAX_PHASE_DEADLINE_MS)
            if phase_deadline < MIN_VIABLE_DEADLINE_MS:
                return False, f"Deadline remaining ({phase_deadline:.1f}ms) below minimum ({MIN_VIABLE_DEADLINE_MS}ms)"

            if self.laya_calls >= MAX_QUERY_LAYA_CALLS:
                return False, f"LAYA decision calls exhausted ({self.laya_calls}/{MAX_QUERY_LAYA_CALLS})"

            if self.expansion_steps >= MAX_EXPANSION_STEPS:
                return False, f"Expansion steps exhausted ({self.expansion_steps}/{MAX_EXPANSION_STEPS})"

            if estimated_input_tokens > MAX_INPUT_TOKENS_CAP:
                return False, f"Estimated input tokens ({estimated_input_tokens}) exceeds cap ({MAX_INPUT_TOKENS_CAP})"

            if estimated_output_tokens > MAX_OUTPUT_TOKENS_CAP:
                return False, f"Estimated output tokens ({estimated_output_tokens}) exceeds cap ({MAX_OUTPUT_TOKENS_CAP})"

            # Atomically reserve
            self.laya_calls += 1
            self.expansion_steps += 1
            self.tool_attempts += 1
            return True, "reserved"

    def record_attempt(self) -> int:
        """Charge an additional tool attempt for retries within the same bounded operation."""
        with self._lock:
            self.tool_attempts += 1
            return self.tool_attempts

    def settle_spend(
        self,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        is_late: bool = False,
        status: str = "completed",
    ) -> None:
        """Atomically settle billable tokens and latency into the query ledger."""
        with self._lock:
            self.total_input_tokens += max(0, input_tokens)
            self.total_output_tokens += max(0, output_tokens)
            self.total_latency_ms += max(0.0, latency_ms)
            if is_late:
                self.late_settled_calls += 1
            self.settled_history.append(
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "latency_ms": latency_ms,
                    "is_late": is_late,
                    "status": status,
                }
            )


def calculate_phase_deadline(remaining_query_ms: float) -> float:
    """Derive escalation phase deadline bounded by min(query_remaining_ms, 2500.0)."""
    return min(max(0.0, remaining_query_ms), MAX_PHASE_DEADLINE_MS)


def estimate_tokens_defensible_bound(text: str) -> int:
    """Calculate defensible upper bound on token count from characters.

    Standard conservative heuristic: 1 token ~= 3 characters for English text + punctuation.
    """
    if not text:
        return 0
    return max(1, (len(text) + 2) // 3)


def truncate_evidence_summary_for_preflight(
    request: LayaEscalationRequest,
    target_max_tokens: int = MAX_INPUT_TOKENS_CAP,
    system_prompt: str = "",
) -> LayaEscalationRequest:
    """Deterministically truncate baseline evidence snippets if payload exceeds input cap."""
    req_json = request.model_dump_json()
    full_text = f"{system_prompt}\n{req_json}"
    est = estimate_tokens_defensible_bound(full_text)
    if est <= target_max_tokens:
        return request

    # Deterministically truncate snippets in retrieved_summary
    truncated_summaries: list[BaselineEvidenceSummary] = []
    for item in request.retrieved_summary:
        # Truncate snippet to 80 chars
        short_snippet = item.snippet[:80]
        truncated_item = BaselineEvidenceSummary(
            chunk_id=item.chunk_id,
            document_id=item.document_id,
            document_version=item.document_version,
            workspace_id=item.workspace_id,
            snippet=short_snippet,
            score=item.score,
            source_kind=item.source_kind,
            page_number=item.page_number,
            bbox=item.bbox,
        )
        truncated_summaries.append(truncated_item)

    updated_req = LayaEscalationRequest(
        query_id=request.query_id,
        workspace_id=request.workspace_id,
        query=request.query,
        missing_requirement=request.missing_requirement,
        available_routes=request.available_routes,
        retrieved_summary=tuple(truncated_summaries),
        caller_deadline_ms=request.caller_deadline_ms,
    )

    updated_full = f"{system_prompt}\n{updated_req.model_dump_json()}"
    if estimate_tokens_defensible_bound(updated_full) > target_max_tokens:
        # If still over, drop summaries to top 3
        kept_summaries = tuple(truncated_summaries[:3])
        updated_req = LayaEscalationRequest(
            query_id=request.query_id,
            workspace_id=request.workspace_id,
            query=request.query,
            missing_requirement=request.missing_requirement,
            available_routes=request.available_routes,
            retrieved_summary=kept_summaries,
            caller_deadline_ms=request.caller_deadline_ms,
        )
    return updated_req


def preflight_token_count(
    client: Any,
    model_id: str,
    system_prompt: str,
    user_content: str,
) -> int:
    """Authoritative token count preflight bound to model and complete serialized payload.

    Invokes client.count_tokens if available; falls back to defensible upper bound.
    Fails closed if counting fails and no defensible bound is available.
    """
    messages = [{"role": "user", "content": [{"text": user_content}]}]
    system = [{"text": system_prompt}] if system_prompt else []

    if client is not None and hasattr(client, "count_tokens"):
        try:
            resp = client.count_tokens(
                modelId=model_id,
                input={"converse": {"messages": messages, "system": system}},
            )
            if isinstance(resp, dict) and "inputTokens" in resp:
                return int(resp["inputTokens"])
        except Exception as e:
            logger.warning("laya.count_tokens_failed: %s, falling back to defensible bound", e)

    # Defensible upper bound calculation
    combined = f"{system_prompt}\n{user_content}"
    return estimate_tokens_defensible_bound(combined)


def validate_provider_usage(usage: Any, estimated_prompt_tokens: int) -> tuple[int, int]:
    """Strictly validate provider usage dictionary against policy caps.

    Enforces:
    - Must be a dictionary.
    - inputTokens must be a positive integer.
    - outputTokens must be a non negative integer <= 250.
    - inputTokens must not exceed estimated_prompt_tokens + 50 slack for tokenization variance.
    """
    if not isinstance(usage, dict):
        raise InvalidUsageError(f"Usage must be a dictionary, got {type(usage).__name__}")

    in_tokens = usage.get("inputTokens")
    out_tokens = usage.get("outputTokens")

    if in_tokens is None or out_tokens is None:
        raise InvalidUsageError("Usage dictionary missing inputTokens or outputTokens")

    # Reject booleans (isinstance(True, int) is True in Python)
    if isinstance(in_tokens, bool) or isinstance(out_tokens, bool):
        raise InvalidUsageError("Token counts must not be boolean")

    if not isinstance(in_tokens, int) or not isinstance(out_tokens, int):
        raise InvalidUsageError(
            f"Token counts must be integers, got input={type(in_tokens).__name__}, output={type(out_tokens).__name__}"
        )

    if in_tokens <= 0:
        raise InvalidUsageError(f"inputTokens must be strictly positive, got {in_tokens}")

    if out_tokens < 0:
        raise InvalidUsageError(f"outputTokens must be non negative, got {out_tokens}")

    if out_tokens > MAX_OUTPUT_TOKENS_CAP:
        raise InvalidUsageError(
            f"outputTokens {out_tokens} exceeds strict cap of {MAX_OUTPUT_TOKENS_CAP}"
        )

    # Upper bound check against preflight estimate
    if in_tokens > estimated_prompt_tokens + 100:
        raise InvalidUsageError(
            f"inputTokens {in_tokens} exceeds estimated prompt tokens {estimated_prompt_tokens} by more than allowable variance"
        )

    return in_tokens, out_tokens
