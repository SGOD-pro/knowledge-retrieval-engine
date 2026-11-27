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
try:
    from schemas.contracts.laya import (
        DenseExpansionParams,
        GraphExpansionParams,
        LayaEscalationDecision,
        LayaEscalationRequest,
        LexicalExpansionParams,
        NoneExpansionParams,
        StructuralExpansionParams,
        StructuredExpansionParams,
    )
    from services.retrieval.laya_concurrency import (
        LayaProviderConcurrencyController,
        get_laya_concurrency_controller,
    )
    from services.retrieval.laya_gate import (
        InvalidCountTokensError,
        InvalidUsageError,
        LayaBudgetLedger,
        calculate_phase_deadline,
        estimate_laya_tokens,
        preflight_token_count,
        truncate_evidence_summary_for_preflight,
        validate_provider_usage,
    )
    from services.retrieval.laya_policy import (
        AVAILABLE_ROUTES_SLICE_5_1,
        extract_domain_query_terms,
        get_deterministic_fallback_decision,
    )
except ImportError:
    from src.schemas.contracts.laya import (
        DenseExpansionParams,
        GraphExpansionParams,
        LayaEscalationDecision,
        LayaEscalationRequest,
        LexicalExpansionParams,
        NoneExpansionParams,
        StructuralExpansionParams,
        StructuredExpansionParams,
    )
    from src.services.retrieval.laya_concurrency import (
        LayaProviderConcurrencyController,
        get_laya_concurrency_controller,
    )
    from src.services.retrieval.laya_gate import (
        InvalidCountTokensError,
        InvalidUsageError,
        LayaBudgetLedger,
        calculate_phase_deadline,
        estimate_laya_tokens,
        preflight_token_count,
        truncate_evidence_summary_for_preflight,
        validate_provider_usage,
    )
    from src.services.retrieval.laya_policy import (
        AVAILABLE_ROUTES_SLICE_5_1,
        extract_domain_query_terms,
        get_deterministic_fallback_decision,
    )

logger = logging.getLogger(__name__)


class PayloadBudgetExceededError(ValueError):
    """Raised when mandatory payload content exceeds the preflight byte budget before dispatch."""
    pass


def _trim_utf8_to_bytes(text: str, max_bytes: int) -> str:
    """Trim a string so its UTF-8 encoding does not exceed max_bytes, without splitting multi-byte characters."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    if max_bytes <= 0:
        return ""
    return encoded[:max_bytes].decode("utf-8", errors="ignore")

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
        endpoint_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        if client is not None:
            self.client = client
            self.endpoint_url = "" if not endpoint_url or client is not None else endpoint_url
            self.api_key = api_key or ""
        elif endpoint_url or getattr(settings, "LAYA_ENDPOINT_URL", ""):
            self.endpoint_url = endpoint_url or getattr(settings, "LAYA_ENDPOINT_URL", "")
            self.api_key = api_key or getattr(settings, "LAYA_API_KEY", "")
            self.client = None
        else:
            self.endpoint_url = ""
            self.api_key = ""
            self.client = self._get_production_bedrock_client()
        from providers.bedrock_models import get_extraction_model
        self.model_id = model_id or get_extraction_model(getattr(settings, "LAYA_BEDROCK_MODEL_ID", None))
        self.concurrency_controller = concurrency_controller or get_laya_concurrency_controller()
        self.last_dispatched = False
        self.last_valid_response = False
        self.last_route_accepted = False
        self.last_fallback = False

    @staticmethod
    def _get_production_bedrock_client() -> Any | None:
        """Resolve production Bedrock runtime client through standard infrastructure factory."""
        env = getattr(settings, "ENVIRONMENT", "dev").lower()
        provider = getattr(settings, "MODEL_PROVIDER", "dev").lower()
        if env == "test" or provider == "test":
            return None
        try:
            from aws.infra import get_client
            from botocore.config import Config
            cfg = Config(connect_timeout=3.0, read_timeout=7.0, retries={"max_attempts": 0})
            return get_client("bedrock-runtime", config=cfg)
        except Exception as e:
            logger.info("laya.bedrock_client_init_failed: %s", e)
            return None

    def decide(
        self,
        request: LayaEscalationRequest,
        deadline_remaining_ms: float,
        ledger: LayaBudgetLedger,
    ) -> LayaEscalationDecision:
        """Evaluate missing evidence requirement and return validated escalation decision.

        Enforces:
        1. Single absolute escalation deadline covering pre-reservation, CountTokens,
           admission, inference, retry backoff, and decision validation.
        2. Atomic pre reservation in budget ledger.
        3. Authoritative token preflight with capacity retention and malformed response rejection.
        4. Bounded concurrency admission and capacity retention.
        5. 429 throttling charged as attempts with at most 1 retry within deadline.
        6. Strict provider usage validation without extra slack.
        7. Code validation of proposed route (rejecting unavailable routes in Slice 5.1).
        8. Deterministic fallback on any failure.
        """
        phase_deadline_ms = calculate_phase_deadline(deadline_remaining_ms)
        self.last_dispatched = False
        self.last_valid_response = False
        self.last_route_accepted = False
        self.last_fallback = False
        if phase_deadline_ms < 500.0:
            logger.warning(
                "laya.deadline_insufficient remaining_ms=%.1f min=500.0", phase_deadline_ms
            )
            self.last_fallback = True
            fallback = get_deterministic_fallback_decision(request, "Deadline expired before escalation")
            return fallback.model_copy(update={"execution_status": "timeout"})

        # Absolute escalation deadline covering all operations
        escalation_deadline_t = time.perf_counter() + (phase_deadline_ms / 1000.0)

        # Step 1: Pre-reservation check
        rem_ms = (escalation_deadline_t - time.perf_counter()) * 1000.0
        if rem_ms < 500.0:
            self.last_fallback = True
            fallback = get_deterministic_fallback_decision(request, "Deadline expired before pre-reservation")
            return fallback.model_copy(update={"execution_status": "timeout"})

        reserved, reason = ledger.pre_reserve(
            deadline_remaining_ms=rem_ms,
            estimated_input_tokens=1000,
            estimated_output_tokens=250,
        )
        if not reserved:
            logger.warning("laya.pre_reservation_failed reason=%s", reason)
            self.last_fallback = True
            fallback = get_deterministic_fallback_decision(request, f"Pre-reservation failed: {reason}")
            return fallback.model_copy(update={"execution_status": "budget_exhausted"})

        # Step 2: Token preflight with deterministic evidence truncation
        endpoint_mode = bool(self.endpoint_url)
        active_system_prompt = "" if endpoint_mode else SYSTEM_PROMPT
        truncated_req = truncate_evidence_summary_for_preflight(
            request, target_max_tokens=1000, system_prompt=active_system_prompt
        )
        if endpoint_mode:
            try:
                endpoint_payload = self._format_endpoint_payload(truncated_req)
                token_estimates = estimate_laya_tokens(endpoint_payload)
                if not token_estimates.get("within_estimated_limit", True):
                    raise PayloadBudgetExceededError(
                        f"Estimated sequence tokens ({token_estimates['estimated_max_sequence_tokens']}) exceeds limit (8192)"
                    )
            except PayloadBudgetExceededError as pbe:
                logger.warning("laya.payload_budget_exceeded: %s (failing closed before dispatch)", pbe)
                self.last_dispatched = False
                self.last_fallback = True
                fallback = get_deterministic_fallback_decision(request, f"Payload budget exceeded: {pbe}")
                return fallback.model_copy(update={"execution_status": "rejected_by_code"})
            user_content = json.dumps(endpoint_payload)
        else:
            user_content = truncated_req.model_dump_json()

        # CountTokens protected with remaining deadline and retained capacity
        rem_s = escalation_deadline_t - time.perf_counter()
        if rem_s < 0.5:
            self.last_fallback = True
            fallback = get_deterministic_fallback_decision(request, "Deadline expired before CountTokens preflight")
            return fallback.model_copy(update={"execution_status": "timeout"})

        try:
            est_prompt_tokens = preflight_token_count(
                client=self.client,
                model_id=self.model_id,
                system_prompt=active_system_prompt,
                user_content=user_content,
                concurrency_controller=self.concurrency_controller,
                timeout_s=rem_s,
            )
        except InvalidCountTokensError as cte:
            logger.warning("laya.count_tokens_rejected: %s (failing closed to Section 35 fallback)", cte)
            self.last_fallback = True
            fallback = get_deterministic_fallback_decision(request, f"CountTokens rejected: {cte}")
            return fallback.model_copy(update={"execution_status": "rejected_by_code"})

        if est_prompt_tokens > 1000:
            logger.warning(
                "laya.prompt_exceeds_token_cap est_tokens=%d cap=1000", est_prompt_tokens
            )
            self.last_fallback = True
            fallback = get_deterministic_fallback_decision(request, "Payload exceeds 1000 token cap")
            return fallback.model_copy(update={"execution_status": "rejected_by_code"})

        # Step 3: Bedrock Converse invocation with concurrency controller
        rem_s = escalation_deadline_t - time.perf_counter()
        if rem_s < 0.5:
            logger.warning("laya.deadline_expired_before_inference rem_s=%.2f", rem_s)
            self.last_fallback = True
            fallback = get_deterministic_fallback_decision(request, "Deadline expired before inference")
            return fallback.model_copy(update={"execution_status": "timeout"})

        def _spend_reconcile_cb(usage: dict[str, Any], latency_ms: float, status: str) -> None:
            in_tok = 0
            out_tok = 0
            if isinstance(usage, dict):
                raw_in = usage.get("inputTokens") if "inputTokens" in usage else usage.get("input_tokens")
                raw_out = usage.get("outputTokens") if "outputTokens" in usage else usage.get("output_tokens")
                if isinstance(raw_in, int) and not isinstance(raw_in, bool) and raw_in > 0:
                    in_tok = raw_in
                if isinstance(raw_out, int) and not isinstance(raw_out, bool) and raw_out >= 0:
                    out_tok = raw_out
            is_late = status != "completed"
            ledger.settle_spend(in_tok, out_tok, latency_ms, is_late=is_late, status=status)

        raw_response, status = self._invoke_with_retry(
            truncated_req=truncated_req,
            est_prompt_tokens=est_prompt_tokens,
            timeout_s=rem_s,
            on_spend_reconcile=_spend_reconcile_cb,
            ledger=ledger,
            escalation_deadline_t=escalation_deadline_t,
        )

        if status == "admission_rejected":
            logger.warning("laya.admission_rejected, falling back to deterministic policy")
            self.last_fallback = True
            fallback = get_deterministic_fallback_decision(request, "Admission queue saturated")
            return fallback.model_copy(update={"execution_status": "rejected_by_code"})

        if status in ("caller_timeout", "timeout") or isinstance(raw_response, TimeoutError):
            logger.warning("laya.invocation_timeout status=%s, falling back to deterministic policy", status)
            self.last_fallback = True
            fallback = get_deterministic_fallback_decision(request, "Provider timeout")
            return fallback.model_copy(update={"execution_status": "timeout"})

        if status == "call_failed" or isinstance(raw_response, Exception) or raw_response is None:
            err_str = str(raw_response).lower()
            self.last_fallback = True
            if self.endpoint_url or "unavailable" in err_str or "connection" in err_str or "permission" in err_str or "502" in err_str or "503" in err_str:
                logger.warning("laya.provider_unavailable err=%s, falling back to deterministic policy", raw_response)
                fallback = get_deterministic_fallback_decision(request, f"Provider unavailable: {raw_response}")
                return fallback.model_copy(update={"execution_status": "provider_unavailable"})
            logger.warning("laya.call_failed err=%s, falling back to deterministic policy", raw_response)
            fallback = get_deterministic_fallback_decision(request, "Provider call failed")
            return fallback.model_copy(update={"execution_status": "rejected_by_code"})

        # Step 4: Strict usage validation without extra slack
        usage = raw_response.get("usage")
        try:
            in_tok, out_tok = validate_provider_usage(
                usage=usage,
                estimated_prompt_tokens=est_prompt_tokens,
                reserved_input_tokens=1000,
            )
            raw_response["usage"] = {
                "inputTokens": in_tok,
                "outputTokens": out_tok,
            }
        except InvalidUsageError as e:
            logger.error("laya.invalid_usage error=%s usage=%s", e, usage)
            # Record invalid usage status without double counting already settled spend
            ledger.record_invalid_usage()
            self.last_fallback = True
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
        escalation_deadline_t: float,
    ) -> tuple[Any, str]:
        """Invoke provider with 429 rate limit retry charging bounded by escalation deadline."""
        t0 = time.perf_counter()

        def _single_call() -> dict[str, Any]:
            self.last_dispatched = True
            if self.endpoint_url:
                import requests

                payload = self._format_endpoint_payload(truncated_req)
                token_estimates = estimate_laya_tokens(payload)
                if not token_estimates.get("within_estimated_limit", True):
                    raise PayloadBudgetExceededError(
                        f"Estimated sequence tokens ({token_estimates['estimated_max_sequence_tokens']}) exceeds limit (8192)"
                    )
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
                call_timeout = max(0.1, timeout_s)
                try:
                    resp = requests.post(
                        self.endpoint_url,
                        json=payload,
                        headers=headers,
                        timeout=call_timeout,
                    )
                except requests.Timeout as te:
                    raise TimeoutError(f"LAYA endpoint timed out: {te}") from te
                except requests.RequestException as re:
                    raise ConnectionError(f"LAYA endpoint unavailable: {re}") from re

                if resp.status_code == 429:
                    raise RuntimeError("LAYA endpoint returned 429 rate limit")
                if resp.status_code in (401, 403):
                    raise PermissionError(f"LAYA endpoint authorization failed: {resp.status_code}")
                if resp.status_code != 200:
                    raise ConnectionError(f"LAYA endpoint returned error status {resp.status_code}: {resp.text[:200]}")

                try:
                    data = resp.json()
                except Exception as je:
                    raise ValueError(f"LAYA endpoint returned invalid JSON: {je}") from je

                # Do not clamp, truncate, or invent usage tokens. Preserve raw usage for validate_provider_usage.
                return data

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
            if hasattr(ledger, "can_attempt") and not ledger.can_attempt():
                logger.warning(
                    "laya.max_tool_attempts_reached attempts=%d",
                    getattr(ledger, "tool_attempts", 0),
                )
                return resp, status

            ledger.record_attempt()
            rem_s = escalation_deadline_t - time.perf_counter()
            if rem_s >= 0.5:
                logger.info("laya.429_throttled retrying within remaining deadline rem_s=%.2f", rem_s)
                time.sleep(0.05)  # brief backoff
                rem_s = escalation_deadline_t - time.perf_counter()
                if rem_s >= 0.5:
                    resp, status = self.concurrency_controller.run_provider_call(
                        _single_call,
                        timeout_s=rem_s,
                        on_spend_reconcile=on_spend_reconcile,
                    )

        return resp, status

    def _format_endpoint_payload(self, req: LayaEscalationRequest) -> dict[str, Any]:
        """Format LayaEscalationRequest into /v1/systemone JSON payload.

        Budgets fields separately:
        - Mandatory: query, detected gap, missing dimension.
        - Optional: context snippets from retrieved summary.
        - If mandatory content exceeds document budget, raises PayloadBudgetExceededError before dispatch.
        - Optional snippets are trimmed first to ensure total serialized payload <= 980 bytes.
        """
        all_criteria = {
            "lexical_expansion": "Keyword BM25 search for specific terms or exact phrases",
            "dense_expansion": "Vector similarity search for semantic concepts",
            "structural_expansion": "Navigate document structure or sections",
            "graph_expansion": "Traverse knowledge graph entity links",
            "structured_expansion": "Query structured table columns or database",
            "none": "Evidence is fundamentally absent or unrecoverable; do not expand",
        }
        allowed = tuple(r for r in (req.available_routes or ()) if r in all_criteria)
        filtered_criteria = {r: all_criteria[r] for r in allowed} if allowed else dict(all_criteria)

        base_skeleton: dict[str, Any] = {
            "state": {
                "document": "",
            },
            "questions": {
                "route": {
                    "type": "choice",
                    "instructions": "Targeted retrieval expansion route to execute",
                    "criteria": filtered_criteria,
                },
                "complexity": {
                    "type": "score",
                    "instructions": "How complex is this retrieval gap?",
                    "criteria": ["simple", "moderate", "complex"],
                },
            },
        }

        # Calculate exact wrapper overhead in bytes with safety margin for JSON escaping
        wrapper_bytes = len(json.dumps(base_skeleton).encode("utf-8"))
        max_total_bytes = 980
        # Reserve 16 bytes for JSON escaping of newlines/quotes
        doc_budget = max_total_bytes - wrapper_bytes - 16

        mandatory_text = (
            f"Query: {req.query}\n"
            f"Gap detected: {req.missing_requirement.detected_gap}\n"
            f"Missing dimension: {req.missing_requirement.missing_dimension}"
        )
        mandatory_bytes = len(mandatory_text.encode("utf-8"))

        if mandatory_bytes > doc_budget:
            raise PayloadBudgetExceededError(
                f"Mandatory payload content ({mandatory_bytes} bytes) exceeds document budget ({doc_budget} bytes)"
            )

        # Snippets are optional and trimmed first
        remaining_budget = doc_budget - mandatory_bytes
        snippets = " ".join(s.snippet for s in req.retrieved_summary[:3]).strip()
        context_prefix = "\nContext: "
        prefix_bytes = len(context_prefix.encode("utf-8"))

        if snippets and remaining_budget > prefix_bytes + 20:
            avail_snippet_bytes = remaining_budget - prefix_bytes
            trimmed_snippets = _trim_utf8_to_bytes(snippets, avail_snippet_bytes)
            doc_context = f"{mandatory_text}{context_prefix}{trimmed_snippets}"
        else:
            doc_context = mandatory_text

        base_skeleton["state"]["document"] = doc_context

        # Guarantee total serialized payload is strictly <= max_total_bytes
        total_payload_bytes = len(json.dumps(base_skeleton).encode("utf-8"))
        if total_payload_bytes > max_total_bytes and context_prefix in doc_context:
            excess = total_payload_bytes - max_total_bytes
            mandatory_part, snippet_part = doc_context.split(context_prefix, 1)
            trimmed_snippet = _trim_utf8_to_bytes(snippet_part, max(0, len(snippet_part.encode("utf-8")) - excess - 4))
            if trimmed_snippet:
                base_skeleton["state"]["document"] = f"{mandatory_part}{context_prefix}{trimmed_snippet}"
            else:
                base_skeleton["state"]["document"] = mandatory_part
            total_payload_bytes = len(json.dumps(base_skeleton).encode("utf-8"))

        if total_payload_bytes > 1000:
            raise PayloadBudgetExceededError(
                f"Total serialized payload ({total_payload_bytes} bytes) exceeds 1000 byte policy cap"
            )

        token_estimates = estimate_laya_tokens(base_skeleton)
        if not token_estimates.get("within_estimated_limit", True):
            raise PayloadBudgetExceededError(
                f"Estimated sequence tokens ({token_estimates['estimated_max_sequence_tokens']}) "
                f"exceeds calibrated limit (8192)"
            )

        return base_skeleton

    def _parse_endpoint_response(
        self,
        raw_response: dict[str, Any],
        request: LayaEscalationRequest,
    ) -> LayaEscalationDecision:
        """Parse and validate /v1/systemone answers into LayaEscalationDecision."""
        import uuid

        answers = raw_response.get("answers", {})
        route_data = answers.get("route", {})
        if not isinstance(route_data, dict):
            logger.warning("laya.malformed_answers_route: %s", route_data)
            self.last_fallback = True
            return get_deterministic_fallback_decision(request, "Malformed route answer from endpoint")

        chosen_route = route_data.get("choice")
        if not chosen_route or not isinstance(chosen_route, str):
            logger.warning("laya.missing_route_choice: %s", route_data)
            self.last_fallback = True
            return get_deterministic_fallback_decision(request, "Missing route choice in endpoint answer")

        if chosen_route not in (
            "lexical_expansion",
            "dense_expansion",
            "structural_expansion",
            "graph_expansion",
            "structured_expansion",
            "none",
        ):
            logger.warning("laya.unknown_route_choice: %s", chosen_route)
            self.last_fallback = True
            return get_deterministic_fallback_decision(request, f"Unknown route choice: {chosen_route}")

        conf = route_data.get("answer_confidence")
        if conf is None:
            conf = route_data.get("confidence")
        if conf is None or isinstance(conf, bool) or not isinstance(conf, (int, float)):
            logger.warning("laya.missing_or_invalid_endpoint_confidence: %s", conf)
            self.last_fallback = True
            return get_deterministic_fallback_decision(request, "Missing or invalid confidence score from endpoint")

        conf_float = float(conf)
        if conf_float < 0.0 or conf_float > 1.0:
            logger.warning("laya.out_of_bounds_endpoint_confidence: %f", conf_float)
            self.last_fallback = True
            return get_deterministic_fallback_decision(request, f"Confidence {conf_float} out of bounds")

        parameters: Any
        if chosen_route == "lexical_expansion":
            query_terms = extract_domain_query_terms(
                query=request.query,
                detected_gap=request.missing_requirement.detected_gap,
                max_terms=5,
            )
            if not query_terms:
                query_terms = ("term",)
            parameters = LexicalExpansionParams(
                route_type="lexical_expansion",
                query_terms=query_terms,
            )
        elif chosen_route == "none":
            parameters = NoneExpansionParams(
                route_type="none",
                reason=f"Endpoint selected route 'none' (confidence {conf_float:.3f})",
            )
        elif chosen_route == "dense_expansion":
            parameters = DenseExpansionParams(
                route_type="dense_expansion",
                concept_query=request.query[:200] or "expansion concept",
            )
        elif chosen_route == "structural_expansion":
            doc_id = request.retrieved_summary[0].document_id if request.retrieved_summary else "doc_unknown"
            parameters = StructuralExpansionParams(
                route_type="structural_expansion",
                document_id=doc_id,
            )
        elif chosen_route == "graph_expansion":
            parameters = GraphExpansionParams(
                route_type="graph_expansion",
                seed_entity_id="entity_unknown",
            )
        else:  # structured_expansion
            parameters = StructuredExpansionParams(
                route_type="structured_expansion",
                table_name="table_unknown",
                required_columns=("col1",),
            )

        decision = LayaEscalationDecision(
            decision_id=f"dec_{uuid.uuid4().hex[:8]}",
            route=chosen_route,
            parameters=parameters,
            confidence=conf_float,
            rationale=f"LAYA endpoint selected {chosen_route}",
            execution_status="proposed",
        )
        self.last_valid_response = True

        if decision.route not in AVAILABLE_ROUTES_SLICE_5_1:
            logger.info(
                "laya.route_unavailable_in_slice5_1 route=%s, falling back to deterministic policy",
                decision.route,
            )
            self.last_route_accepted = False
            self.last_fallback = True
            fallback = get_deterministic_fallback_decision(
                request,
                f"Route '{decision.route}' unavailable in Slice 5.1; falling back to lexical",
            )
            return fallback

        self.last_route_accepted = True
        self.last_fallback = False
        return decision.model_copy(update={"execution_status": "approved"})

    def _parse_and_validate_decision(
        self,
        raw_response: dict[str, Any],
        request: LayaEscalationRequest,
    ) -> LayaEscalationDecision:
        """Parse structured Converse or endpoint output into LayaEscalationDecision and validate against Slice 5.1 rules."""
        try:
            # Handle direct LAYA API endpoint response
            if "answers" in raw_response and isinstance(raw_response["answers"], dict):
                return self._parse_endpoint_response(raw_response, request)

            # Extract text from Converse response
            output_msg = raw_response.get("output", {}).get("message", {})
            content_list = output_msg.get("content", [])
            text_blocks = [c.get("text", "") for c in content_list if "text" in c]
            raw_text = "".join(text_blocks).strip()

            if not raw_text:
                logger.warning("laya.empty_text_returned, falling back to deterministic policy")
                self.last_fallback = True
                return get_deterministic_fallback_decision(request, "Empty model response text")

            # Extract JSON block if wrapped in markdown fences
            if "```json" in raw_text:
                raw_text = raw_text.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_text:
                raw_text = raw_text.split("```")[1].split("```")[0].strip()

            parsed = json.loads(raw_text)
            if not isinstance(parsed, dict):
                logger.warning("laya.non_dict_json_returned: %s", raw_text)
                self.last_fallback = True
                return get_deterministic_fallback_decision(request, "Non dictionary JSON returned")

            # Validate confidence is explicit float between 0.0 and 1.0
            conf = parsed.get("confidence")
            if conf is None or not isinstance(conf, (int, float)) or isinstance(conf, bool):
                logger.warning("laya.missing_or_invalid_confidence: %s", conf)
                self.last_fallback = True
                return get_deterministic_fallback_decision(request, "Missing or invalid confidence score")

            if conf < 0.0 or conf > 1.0:
                logger.warning("laya.out_of_bounds_confidence: %f", conf)
                self.last_fallback = True
                return get_deterministic_fallback_decision(request, f"Confidence {conf} out of bounds")

            # Validate Pydantic schema
            decision = LayaEscalationDecision.model_validate(parsed)
            self.last_valid_response = True

            # Code Validation: In Slice 5.1, non lexical routes are explicitly unavailable
            if decision.route not in AVAILABLE_ROUTES_SLICE_5_1:
                logger.info(
                    "laya.route_unavailable_in_slice5_1 route=%s, falling back to deterministic policy",
                    decision.route,
                )
                self.last_route_accepted = False
                self.last_fallback = True
                fallback = get_deterministic_fallback_decision(
                    request,
                    f"Route '{decision.route}' unavailable in Slice 5.1; falling back to lexical",
                )
                return fallback

            # Decision is approved
            self.last_route_accepted = True
            self.last_fallback = False
            return decision.model_copy(update={"execution_status": "approved"})

        except (json.JSONDecodeError, ValidationError, ValueError, KeyError) as e:
            logger.warning("laya.validation_failed error=%s, falling back to deterministic policy", e)
            self.last_fallback = True
            return get_deterministic_fallback_decision(request, f"Decision validation failed: {e}")
