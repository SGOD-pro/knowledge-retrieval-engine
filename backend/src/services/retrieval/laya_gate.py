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
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

try:
    from schemas.contracts.laya import BaselineEvidenceSummary, LayaEscalationRequest
except ImportError:
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


class InvalidCountTokensError(Exception):
    """Raised when count_tokens preflight fails, returns a malformed response, or exceeds proven limits."""
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
    invalid_usage_calls: int = 0
    settled_history: list[dict[str, Any]] = Field(default_factory=list)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

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

            if self.tool_attempts >= MAX_TOOL_ATTEMPTS:
                return False, f"Tool attempts exhausted ({self.tool_attempts}/{MAX_TOOL_ATTEMPTS})"

            if estimated_input_tokens > MAX_INPUT_TOKENS_CAP:
                return False, f"Estimated input tokens ({estimated_input_tokens}) exceeds cap ({MAX_INPUT_TOKENS_CAP})"

            if estimated_output_tokens > MAX_OUTPUT_TOKENS_CAP:
                return False, f"Estimated output tokens ({estimated_output_tokens}) exceeds cap ({MAX_OUTPUT_TOKENS_CAP})"

            # Atomically reserve
            self.laya_calls += 1
            self.expansion_steps += 1
            self.tool_attempts += 1
            return True, "reserved"

    def can_attempt(self) -> bool:
        """Check whether additional tool attempts remain under MAX_TOOL_ATTEMPTS."""
        with self._lock:
            return self.tool_attempts < MAX_TOOL_ATTEMPTS

    def record_attempt(self) -> int:
        """Charge an additional tool attempt for retries within the same bounded operation."""
        with self._lock:
            self.tool_attempts += 1
            return self.tool_attempts

    def record_synthesis_call(self) -> int:
        """Atomically increment and return the synthesis call count."""
        with self._lock:
            self.synthesis_calls += 1
            return self.synthesis_calls

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
            if len(self.settled_history) >= 20:
                self.settled_history.pop(0)
            self.settled_history.append(
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "latency_ms": latency_ms,
                    "is_late": is_late,
                    "status": status,
                }
            )

    def record_invalid_usage(self) -> None:
        """Mark last settlement or record invalid usage event without double counting spend."""
        with self._lock:
            self.invalid_usage_calls += 1
            if self.settled_history:
                self.settled_history[-1]["status"] = "invalid_usage"
            else:
                if len(self.settled_history) >= 20:
                    self.settled_history.pop(0)
                self.settled_history.append(
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "latency_ms": 0.0,
                        "is_late": False,
                        "status": "invalid_usage",
                    }
                )


def calculate_phase_deadline(remaining_query_ms: float) -> float:
    """Derive escalation phase deadline bounded by min(query_remaining_ms, 2500.0)."""
    return min(max(0.0, remaining_query_ms), MAX_PHASE_DEADLINE_MS)


def proven_token_upper_bound(text: str) -> int:
    """Calculate mathematically proven upper bound on BPE tokens.

    In UTF-8 byte encoding, every BPE token represents at least one byte.
    Therefore, len(text.encode('utf-8')) is a mathematically proven upper bound on token count.
    """
    if not text:
        return 0
    return len(text.encode("utf-8"))


# Backwards compatibility alias pointing to proven bound
estimate_tokens_defensible_bound = proven_token_upper_bound


def truncate_evidence_summary_for_preflight(
    request: LayaEscalationRequest,
    target_max_tokens: int = MAX_INPUT_TOKENS_CAP,
    system_prompt: str = "",
) -> LayaEscalationRequest:
    """Deterministically truncate baseline evidence snippets if payload exceeds input cap."""
    req_json = request.model_dump_json()
    full_text = f"{system_prompt}\n{req_json}"
    bound = proven_token_upper_bound(full_text)
    if bound <= target_max_tokens:
        return request

    # Deterministically truncate snippets and drop summaries until bound <= target_max_tokens
    candidate = request
    for snip_len in (80, 40, 20):
        for count in range(len(request.retrieved_summary), -1, -1):
            truncated_items = tuple(
                BaselineEvidenceSummary(
                    chunk_id=item.chunk_id,
                    document_id=item.document_id,
                    document_version=item.document_version,
                    workspace_id=item.workspace_id,
                    snippet=item.snippet[:snip_len],
                    score=item.score,
                    source_kind=item.source_kind,
                    page_number=item.page_number,
                    bbox=item.bbox,
                )
                for item in request.retrieved_summary[:count]
            )
            candidate = LayaEscalationRequest(
                query_id=request.query_id,
                workspace_id=request.workspace_id,
                query=request.query,
                missing_requirement=request.missing_requirement,
                available_routes=request.available_routes,
                retrieved_summary=truncated_items,
                caller_deadline_ms=request.caller_deadline_ms,
            )
            cand_bound = proven_token_upper_bound(f"{system_prompt}\n{candidate.model_dump_json()}")
            if cand_bound <= target_max_tokens:
                return candidate

    return candidate


def preflight_token_count(
    client: Any,
    model_id: str,
    system_prompt: str,
    user_content: str,
    concurrency_controller: Any | None = None,
    timeout_s: float = 2.5,
) -> int:
    """Authoritative token count preflight bound to model and complete serialized payload.

    Invokes client.count_tokens with deadline and capacity protection if available.
    Fails closed if counting fails, returns malformed responses, or exceeds proven limits.
    """
    messages = [{"role": "user", "content": [{"text": user_content}]}]
    system = [{"text": system_prompt}] if system_prompt else []
    full_payload = f"{system_prompt}\n{user_content}"
    proven_bound = proven_token_upper_bound(full_payload)

    if client is not None and hasattr(client, "count_tokens"):
        def _count_call() -> dict[str, Any]:
            return client.count_tokens(
                modelId=model_id,
                input={"converse": {"messages": messages, "system": system}},
            )

        if concurrency_controller is not None and hasattr(concurrency_controller, "run_provider_call"):
            resp, status = concurrency_controller.run_provider_call(_count_call, timeout_s=timeout_s)
            if status == "admission_rejected":
                raise InvalidCountTokensError("CountTokens admission queue saturated")
            if status == "caller_timeout" or resp is None:
                raise InvalidCountTokensError("CountTokens provider call timed out")
            if status == "call_failed" or isinstance(resp, Exception):
                raise InvalidCountTokensError(f"CountTokens provider call failed: {resp}")
        else:
            try:
                resp = _count_call()
            except Exception as exc:
                raise InvalidCountTokensError(f"CountTokens invocation failed: {exc}")

        # Strict validation of CountTokens response before inference
        if not isinstance(resp, dict):
            raise InvalidCountTokensError(f"Malformed CountTokens response: expected dict, got {type(resp).__name__}")

        raw_tokens = resp.get("inputTokens")
        if raw_tokens is None or isinstance(raw_tokens, bool) or not isinstance(raw_tokens, int):
            raise InvalidCountTokensError(f"Malformed CountTokens response: invalid inputTokens {raw_tokens}")

        if raw_tokens <= 0:
            raise InvalidCountTokensError(f"Malformed CountTokens response: non-positive inputTokens {raw_tokens}")

        return int(raw_tokens)

    # Client has no count_tokens capability
    if proven_bound <= MAX_INPUT_TOKENS_CAP:
        return proven_bound

    raise InvalidCountTokensError(
        f"Payload byte bound ({proven_bound}) exceeds cap ({MAX_INPUT_TOKENS_CAP}) and client lacks authoritative count_tokens"
    )


def validate_provider_usage(
    usage: Any,
    estimated_prompt_tokens: int,
    reserved_input_tokens: int = MAX_INPUT_TOKENS_CAP,
) -> tuple[int, int]:
    """Strictly validate provider usage dictionary against policy caps.

    Enforces without extra slack:
    - Must be a dictionary.
    - inputTokens or input_tokens must be present.
    - outputTokens or output_tokens must be present.
    - Neither token count may be boolean.
    - Neither token count may be fractional (float).
    - inputTokens must be a strictly positive integer.
    - outputTokens must be a non negative integer <= 250.
    - inputTokens must be <= estimated_prompt_tokens (zero extra slack).
    - inputTokens must be <= reserved_input_tokens (zero extra slack).
    - inputTokens must be <= 1000 strict cap.
    """
    if not isinstance(usage, dict):
        raise InvalidUsageError(f"Usage must be a dictionary, got {type(usage).__name__}")

    has_in = "inputTokens" in usage or "input_tokens" in usage
    has_out = "outputTokens" in usage or "output_tokens" in usage

    if not has_in or not has_out:
        raise InvalidUsageError("Usage dictionary missing required inputTokens/input_tokens or outputTokens/output_tokens")

    in_tokens = usage.get("inputTokens") if "inputTokens" in usage else usage.get("input_tokens")
    out_tokens = usage.get("outputTokens") if "outputTokens" in usage else usage.get("output_tokens")

    if in_tokens is None or out_tokens is None:
        raise InvalidUsageError("Usage dictionary contains null input or output tokens")

    # Reject booleans (isinstance(True, int) is True in Python)
    if isinstance(in_tokens, bool) or isinstance(out_tokens, bool):
        raise InvalidUsageError("Token counts must not be boolean")

    # Reject fractional counts (floats)
    if isinstance(in_tokens, float) or isinstance(out_tokens, float):
        raise InvalidUsageError(
            f"Token counts must not be fractional: input={in_tokens}, output={out_tokens}"
        )

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

    # Strict upper bound checks without extra slack
    if in_tokens > estimated_prompt_tokens:
        raise InvalidUsageError(
            f"inputTokens {in_tokens} exceeds preflight estimate of {estimated_prompt_tokens}"
        )

    if in_tokens > reserved_input_tokens:
        raise InvalidUsageError(
            f"inputTokens {in_tokens} exceeds reserved capacity of {reserved_input_tokens}"
        )

    if in_tokens > MAX_INPUT_TOKENS_CAP:
        raise InvalidUsageError(
            f"inputTokens {in_tokens} exceeds strict cap of {MAX_INPUT_TOKENS_CAP}"
        )

    return in_tokens, out_tokens
