"""LAYA decision service executing bounded model routing and code validation.

Enforces:
- Structured Bedrock Converse invocation with inferenceConfig (maxTokens=250, temperature=0.0).
- Strict output validation rejecting answer prose, invalid schemas, and discriminator mismatches.
- Explicit unavailability of non lexical routes in Slice 5.1.
- Mandatory routing confidence between 0.0 and 1.0 (never used as evidence sufficiency).
- Deterministic fallback to Section 35 policy on errors, timeouts, 429s, or invalid proposals.
- Bounded admission, capacity retention, and late spend settlement via LayaProviderConcurrencyController.
- Offline test double injection with zero paid provider calls.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from pydantic import ValidationError

from config import settings
from src.schemas.contracts.laya import (
    LayaEscalationDecision,
    LayaEscalationRequest,
)
from src.services.retrieval.laya_concurrency import (
    LayaProviderConcurrencyController,
    get_laya_concurrency_controller,
)
from src.services.retrieval.laya_gate import (
    InvalidUsageError,
    LayaBudgetLedger,
    calculate_phase_deadline,
    preflight_token_count,
    truncate_evidence_summary_for_preflight,
    validate_provider_usage,
)
from src.services.retrieval.laya_policy import (
    AVAILABLE_ROUTES_SLICE_5_1,
    get_deterministic_fallback_decision,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are LAYA, a bounded routing decision model for the Knowledge Retrieval Engine.
Your role is exclusively to inspect missing evidence requirements and propose ONE targeted expansion route.
You must NEVER write answer prose, synthesize text, or converse with the user.
You must return only a valid JSON object matching the LayaEscalationDecision schema.
The JSON must include:
- decision_id: string
- route: one of "lexical_expansion", "dense_expansion", "structural_expansion", "graph_expansion", "structured_expansion", "none"
- parameters: matching object with route_type identical to route
- confidence: mandatory float between 0.0 and 1.0 indicating routing choice confidence
- rationale: brief explanation (max 200 chars)
"""


class LayaDecisionService:
    """Service orchestrating LAYA decision model invocation and authoritative code validation."""

    def __init__(
        self,
        client: Any | None = None,
        model_id: str | None = None,
        concurrency_controller: LayaProviderConcurrencyController | None = None,
    ) -> None:
        self.client = client
        self.model_id = model_id or getattr(settings, "LAYA_BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
        self.concurrency_controller = concurrency_controller or get_laya_concurrency_controller()

    def decide(
        self,
        request: LayaEscalationRequest,
        deadline_remaining_ms: float,
        ledger: LayaBudgetLedger,
    ) -> LayaEscalationDecision:
        """Evaluate missing evidence requirement and return validated escalation decision.

        Enforces:
        1. Phase deadline bounds (min(query_remaining_ms, 2500.0) >= 500.0ms).
        2. Atomic pre reservation in budget ledger.
        3. Token preflight with deterministic evidence truncation if input > 1000 tokens.
        4. Bounded concurrency admission and capacity retention.
        5. 429 throttling charged as attempts with at most 1 retry.
        6. Strict provider usage validation.
        7. Code validation of proposed route (rejecting unavailable routes in Slice 5.1).
        8. Deterministic fallback on any failure.
        """
        phase_deadline_ms = calculate_phase_deadline(deadline_remaining_ms)
        if phase_deadline_ms < 500.0:
            logger.warning(
                "laya.deadline_insufficient remaining_ms=%.1f min=500.0", phase_deadline_ms
            )
            fallback = get_deterministic_fallback_decision(request, "Deadline expired before escalation")
            return fallback.model_copy(update={"execution_status": "timeout"})

        # Step 1: Pre-reservation check
        reserved, reason = ledger.pre_reserve(
            deadline_remaining_ms=phase_deadline_ms,
            estimated_input_tokens=1000,
            estimated_output_tokens=250,
        )
        if not reserved:
            logger.warning("laya.pre_reservation_failed reason=%s", reason)
            fallback = get_deterministic_fallback_decision(request, f"Pre-reservation failed: {reason}")
            return fallback.model_copy(update={"execution_status": "budget_exhausted"})

        # Step 2: Token preflight and deterministic truncation
        truncated_req = truncate_evidence_summary_for_preflight(
            request, target_max_tokens=1000, system_prompt=SYSTEM_PROMPT
        )
        req_json = truncated_req.model_dump_json()

        est_prompt_tokens = preflight_token_count(
            client=self.client,
            model_id=self.model_id,
            system_prompt=SYSTEM_PROMPT,
            user_content=req_json,
        )
        if est_prompt_tokens > 1000:
            logger.warning(
                "laya.prompt_exceeds_token_cap est_tokens=%d cap=1000", est_prompt_tokens
            )
            fallback = get_deterministic_fallback_decision(request, "Payload exceeds 1000 token cap")
            return fallback.model_copy(update={"execution_status": "rejected_by_code"})

        # Step 3: Bedrock Converse invocation with concurrency controller
        timeout_s = phase_deadline_ms / 1000.0

        def _spend_reconcile_cb(usage: dict[str, Any], latency_ms: float, status: str) -> None:
            in_tok = usage.get("inputTokens", 0) if isinstance(usage, dict) else 0
            out_tok = usage.get("outputTokens", 0) if isinstance(usage, dict) else 0
            is_late = status != "completed"
            ledger.settle_spend(in_tok, out_tok, latency_ms, is_late=is_late, status=status)

        raw_response, status = self._invoke_with_retry(
            truncated_req=truncated_req,
            est_prompt_tokens=est_prompt_tokens,
            timeout_s=timeout_s,
            on_spend_reconcile=_spend_reconcile_cb,
            ledger=ledger,
        )

        if status == "admission_rejected":
            logger.warning("laya.admission_rejected, falling back to deterministic policy")
            fallback = get_deterministic_fallback_decision(request, "Admission queue saturated")
            return fallback.model_copy(update={"execution_status": "rejected_by_code"})

        if status == "caller_timeout" or raw_response is None:
            logger.warning("laya.invocation_timeout status=%s, falling back to deterministic policy", status)
            fallback = get_deterministic_fallback_decision(request, "Provider timeout")
            return fallback.model_copy(update={"execution_status": "timeout"})

        if status == "call_failed" or isinstance(raw_response, Exception):
            logger.warning("laya.call_failed err=%s, falling back to deterministic policy", raw_response)
            fallback = get_deterministic_fallback_decision(request, "Provider call failed")
            return fallback.model_copy(update={"execution_status": "rejected_by_code"})

        # Step 4: Strict usage validation
        usage = raw_response.get("usage")
        try:
            in_tok, out_tok = validate_provider_usage(usage, est_prompt_tokens)
        except InvalidUsageError as e:
            logger.error("laya.invalid_usage error=%s usage=%s", e, usage)
            # Settle known spent tokens fail closed
            ledger.settle_spend(
                in_tok=usage.get("inputTokens", 0) if isinstance(usage, dict) and isinstance(usage.get("inputTokens"), int) else 0,
                out_tok=usage.get("outputTokens", 0) if isinstance(usage, dict) and isinstance(usage.get("outputTokens"), int) else 0,
                latency_ms=0.0,
                is_late=False,
                status="invalid_usage",
            )
            fallback = get_deterministic_fallback_decision(request, f"Invalid provider usage: {e}")
            return fallback.model_copy(update={"execution_status": "rejected_by_code"})

        # Step 5: Decision parsing and code validation
        decision = self._parse_and_validate_decision(raw_response, request)
        return decision

    def _invoke_with_retry(
        self,
        truncated_req: LayaEscalationRequest,
        est_prompt_tokens: int,
        timeout_s: float,
        on_spend_reconcile: Any,
        ledger: LayaBudgetLedger,
    ) -> tuple[Any, str]:
        """Invoke provider with 429 rate limit retry charging."""
        t0 = time.perf_counter()

        def _single_call() -> dict[str, Any]:
            if self.client is None:
                # Offline test mode or mock
                raise RuntimeError("No provider client configured")

            messages = [{"role": "user", "content": [{"text": truncated_req.model_dump_json()}]}]
            system = [{"text": SYSTEM_PROMPT}]

            # Bedrock Converse call
            return self.client.converse(
                modelId=self.model_id,
                messages=messages,
                system=system,
                inferenceConfig={"maxTokens": 250, "temperature": 0.0},
            )

        resp, status = self.concurrency_controller.run_provider_call(
            _single_call,
            timeout_s=timeout_s,
            on_spend_reconcile=on_spend_reconcile,
        )

        # Handle 429 throttling retry
        is_429 = False
        if status == "call_failed" and isinstance(resp, Exception):
            err_msg = str(resp).lower()
            if "429" in err_msg or "throttling" in err_msg or "rate limit" in err_msg:
                is_429 = True

        if is_429:
            ledger.record_attempt()
            elapsed_s = time.perf_counter() - t0
            rem_s = timeout_s - elapsed_s
            if rem_s >= 0.5:
                logger.info("laya.429_throttled retrying within remaining deadline rem_s=%.2f", rem_s)
                time.sleep(0.05)  # brief backoff
                resp, status = self.concurrency_controller.run_provider_call(
                    _single_call,
                    timeout_s=rem_s,
                    on_spend_reconcile=on_spend_reconcile,
                )

        return resp, status

    def _parse_and_validate_decision(
        self,
        raw_response: dict[str, Any],
        request: LayaEscalationRequest,
    ) -> LayaEscalationDecision:
        """Parse structured Converse output into LayaEscalationDecision and validate against Slice 5.1 rules."""
        try:
            # Extract text from Converse response
            output_msg = raw_response.get("output", {}).get("message", {})
            content_list = output_msg.get("content", [])
            text_blocks = [c.get("text", "") for c in content_list if "text" in c]
            raw_text = "".join(text_blocks).strip()

            if not raw_text:
                logger.warning("laya.empty_text_returned, falling back to deterministic policy")
                return get_deterministic_fallback_decision(request, "Empty model response text")

            # Extract JSON block if wrapped in markdown fences
            if "```json" in raw_text:
                raw_text = raw_text.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_text:
                raw_text = raw_text.split("```")[1].split("```")[0].strip()

            parsed = json.loads(raw_text)
            if not isinstance(parsed, dict):
                logger.warning("laya.non_dict_json_returned: %s", raw_text)
                return get_deterministic_fallback_decision(request, "Non dictionary JSON returned")

            # Validate confidence is explicit float between 0.0 and 1.0
            conf = parsed.get("confidence")
            if conf is None or not isinstance(conf, (int, float)) or isinstance(conf, bool):
                logger.warning("laya.missing_or_invalid_confidence: %s", conf)
                return get_deterministic_fallback_decision(request, "Missing or invalid confidence score")

            if conf < 0.0 or conf > 1.0:
                logger.warning("laya.out_of_bounds_confidence: %f", conf)
                return get_deterministic_fallback_decision(request, f"Confidence {conf} out of bounds")

            # Validate Pydantic schema
            decision = LayaEscalationDecision.model_validate(parsed)

            # Code Validation: In Slice 5.1, non lexical routes are explicitly unavailable
            if decision.route not in AVAILABLE_ROUTES_SLICE_5_1:
                logger.info(
                    "laya.route_unavailable_in_slice5_1 route=%s, falling back to deterministic policy",
                    decision.route,
                )
                fallback = get_deterministic_fallback_decision(
                    request,
                    f"Route '{decision.route}' unavailable in Slice 5.1; falling back to lexical",
                )
                return fallback

            # Decision is approved
            return decision.model_copy(update={"execution_status": "approved"})

        except (json.JSONDecodeError, ValidationError, ValueError, KeyError) as e:
            logger.warning("laya.validation_failed error=%s, falling back to deterministic policy", e)
            return get_deterministic_fallback_decision(request, f"Decision validation failed: {e}")
