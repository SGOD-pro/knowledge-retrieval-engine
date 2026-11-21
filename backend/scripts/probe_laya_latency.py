#!/usr/bin/env python3
"""Bounded LAYA Latency Readiness Probe.

Inspects direct endpoint and compiled LangGraph pipeline performance under
controlled synthetic inputs, bounded to at most five LAYA requests.

Features:
- Completely synthetic in-memory input (no corpus or ground truth files).
- Distinguishes first request and subsequent request timings.
- Records per-attempt HTTP metrics (roundtrip, server latency, unaccounted time,
  status code, error, and late completion), preventing any measurement reuse.
- Separates direct endpoint results from compiled pipeline results.
- Preserves the strict 2500ms escalation phase deadline across all requests.
- Requires observed accepted routes and actual lexical expansion for lexical success.
- Requires an accepted none decision and clean refusal for route none.
- Rejects fallback delivery as proof of provider readiness.
- Enforces at most five actual HTTP transmissions (including retries) and rejects redirects.
- Uses offline doubles for synthesis, embeddings, and reranking (zero paid calls).
- Completely masks all credentials with zero key fragments printed.
- Offline HARNESS PASS strictly requires all explicit assertions and negative controls to pass.
- Defaults to offline dry-run preparation; live execution requires explicit --live opt-in.

Usage:
    # 1. Default offline preparation (zero live requests, validates all assertions and negative controls):
    cd backend && uv run python scripts/probe_laya_latency.py

    # 2. Explicit opt-in live probe against running LAYA server (at most 5 requests):
    cd backend && uv run python scripts/probe_laya_latency.py --live
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

# Ensure test environment mode so repository and models run offline in-memory
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("MODEL_PROVIDER", "test")

# Add src to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import requests
from config import settings
from db.database import CloudRepository
from schemas.contracts.laya import (
    BaselineEvidenceSummary,
    LayaEscalationDecision,
    LayaEscalationRequest,
    LexicalExpansionParams,
    MissingRequirementDescription,
    NoneExpansionParams,
)
from schemas.contracts.verification import VerificationRecord
from schemas.models import Chunk, Document
from services.langgraph_pipeline import pipeline
from services.retrieval.bm25_retriever import invalidate_chunk_cache
from services.retrieval.laya_gate import LayaBudgetLedger
from services.retrieval.laya_service import LayaDecisionService

logger = logging.getLogger("laya_probe")

MAX_ALLOWED_PROBE_REQUESTS = 5


class RedirectRejectedError(RuntimeError):
    """Raised when an HTTP redirect is received or requested in the latency probe."""


@dataclass
class HttpAttemptRecord:
    """Per-attempt HTTP transmission record preventing cross-request metric leakage."""

    attempt_id: int
    started_at: float
    probe_id: str | None = None
    ended_at: float | None = None
    roundtrip_ms: float | None = None
    server_latency_ms: float | None = None
    status_code: int | None = None
    error: str | None = None
    is_redirect: bool = False
    caller_timed_out: bool = False
    caller_timeout_at: float | None = None
    late_completed: bool = False
    response_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeMetricRecord:
    """Detailed observation record for a single probe query."""

    probe_id: str
    target: str  # "direct_endpoint" or "compiled_pipeline"
    request_sequence: str  # "first_request" or "subsequent_request"
    query_type: str  # "lexical_expansion" or "none"
    client_roundtrip_ms: float | None
    server_latency_ms: float | None
    unaccounted_elapsed_ms: float | None
    dispatched: bool
    valid_response: bool
    proposed_route: str
    route_accepted: bool
    expansion_executed: bool
    fallback: bool
    execution_status: str
    pipeline_total_ms: float | None = None
    delivery_outcome: str | None = None  # "DELIVERED", "REFUSAL", "APPROVED", "REJECTED", "TIMEOUT"
    phase_deadline_ms: float = 2500.0
    completed_within_phase_deadline: bool = True
    token_usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    is_redirect: bool = False
    attempts_count: int = 1


class MockOfflineVerifier:
    """Offline test double for the generative semantic verifier."""

    def __init__(self, support_status: str = "ASSERTED") -> None:
        self.support_status = support_status
        self.verify_call_count = 0

    def is_ready(self) -> bool:
        return True

    def verify(
        self,
        text: str = "",
        context: str = "",
        citations: list[Any] | None = None,
        **kwargs: Any,
    ) -> VerificationRecord:
        self.verify_call_count += 1
        return VerificationRecord(
            verification_id="vr_probe_synthetic_001",
            workspace_id="ws_latency_probe",
            query_id="q_probe_live",
            snapshot_id="snap_probe_1",
            requirement_id="req_probe_turnover",
            evidence_id="doc_probe_2026:chk_probe_turnover",
            verifier_version="v1.0",
            verifier_type="generative",
            attempt_number=1,
            execution_time_ms=15,
            support_status=self.support_status,
            confidence_score=0.95,
            verification_rationale="Synthetic verification asserted",
            verified_at="2026-11-20T00:00:00Z",
        )


def _mask_credential(key: str) -> str:
    """Mask sensitive API key for safe display without printing any key fragments."""
    if not key:
        return "[NOT CONFIGURED]"
    return "[CONFIGURED: REDACTED]"


def _build_synthetic_request(
    query: str,
    gap: str,
    missing_dimension: str = "exact_term",
    caller_deadline_ms: float = 2500.0,
) -> LayaEscalationRequest:
    """Build a synthetic LayaEscalationRequest without touching disk or corpus."""
    return LayaEscalationRequest(
        query_id="q_probe_synthetic",
        workspace_id="ws_latency_probe",
        query=query,
        missing_requirement=MissingRequirementDescription(
            requirement_id="req_probe_001",
            missing_dimension=missing_dimension,  # type: ignore[arg-type]
            description="Synthetic probe missing requirement",
            failed_stage="run_fidelity_coverage",
            detected_gap=gap,
        ),
        available_routes=("lexical_expansion", "none"),
        retrieved_summary=(
            BaselineEvidenceSummary(
                chunk_id="chk_probe_baseline",
                document_id="doc_probe_2026",
                document_version="ver_probe_v1",
                workspace_id="ws_latency_probe",
                snippet="General corporate overview with background metrics.",
                score=10.0,
                source_kind="text",
            ),
        ),
        caller_deadline_ms=caller_deadline_ms,
    )


def _setup_synthetic_repository() -> CloudRepository:
    """Seed synthetic documents in an in-memory repository without disk persistence."""
    repo = CloudRepository()
    ws_id = "ws_latency_probe"

    doc_chunk = Chunk(
        id="chk_probe_baseline",
        document_id="doc_probe_2026",
        source_format="pdf",
        text="General corporate overview with background metrics.",
        element_type="narrative_block",
        workspace_id=ws_id,
        metadata={"document_version": "ver_probe_v1"},
        embedding_fast=[0.1] * 384,
        embedding_full=[0.1] * 1024,
    )
    novel_chunk = Chunk(
        id="chk_probe_turnover",
        document_id="doc_probe_2026",
        source_format="pdf",
        text="The fiscal year 2026 turnover was officially confirmed as $450 million.",
        element_type="narrative_block",
        workspace_id=ws_id,
        metadata={"document_version": "ver_probe_v1"},
        embedding_fast=[0.1] * 384,
        embedding_full=[0.1] * 1024,
    )
    doc = Document(
        id="doc_probe_2026",
        filename="doc_probe_2026.pdf",
        source_format="pdf",
        chunks=(doc_chunk, novel_chunk),
        workspace_id=ws_id,
    )
    repo.save(doc)
    repo.add_document_to_workspace(ws_id, doc)
    invalidate_chunk_cache(ws_id)
    return repo


class BoundedNetworkSpy:
    """Monitors, measures, and strictly bounds network dispatches before execution.

    Maintains per-attempt records so exceptions, timeouts, and late worker completions
    never contaminate or reuse measurements across requests.
    """

    def __init__(
        self,
        dry_run: bool = False,
        max_requests: int = MAX_ALLOWED_PROBE_REQUESTS,
    ) -> None:
        self.dry_run = dry_run
        self.max_requests = max_requests
        self.request_count = 0
        self.attempts: list[HttpAttemptRecord] = []
        self._lock = threading.Lock()
        self._orig_post = requests.post
        self.current_probe_id: str | None = None

        # Simulation hooks for negative control testing
        self.simulated_exception: Exception | None = None
        self.simulated_status_code: int | None = None
        self.simulated_server_latency_ms: float | None = 32.5
        self.simulated_is_redirect: bool = False
        self.simulated_delay_s: float = 0.04

        # Event-controlled worker hooks for asynchronous timeout/late-completion testing
        self.on_worker_start: Any | None = None
        self.pause_event: threading.Event | None = None

    def set_probe_id(self, probe_id: str | None) -> None:
        """Associate subsequent attempts with an originating probe request."""
        with self._lock:
            self.current_probe_id = probe_id

    def get_attempts_for_probe(self, probe_id: str) -> list[HttpAttemptRecord]:
        """Return all attempts associated with an originating probe request."""
        with self._lock:
            return [att for att in self.attempts if att.probe_id == probe_id]

    def record_caller_timeout(self, probe_id: str | None = None) -> None:
        """Record caller timeout on matching active attempt without waiting for HTTP completion."""
        with self._lock:
            target_attempts = [
                att for att in self.attempts
                if (probe_id is None or att.probe_id == probe_id) and att.ended_at is None
            ]
            now = time.perf_counter()
            for att in target_attempts:
                att.caller_timed_out = True
                att.caller_timeout_at = now
                if not att.error:
                    att.error = "caller_timeout"

    def spy_post(self, *args: Any, **kwargs: Any) -> Any:
        # Pre-dispatch validation ceiling
        with self._lock:
            if self.request_count >= self.max_requests:
                raise RuntimeError(
                    f"Pre-dispatch validation failed: Request count ({self.request_count}) has reached maximum ceiling ({self.max_requests}). Dispatch blocked."
                )
            self.request_count += 1
            attempt_id = self.request_count
            attempt = HttpAttemptRecord(
                attempt_id=attempt_id,
                probe_id=self.current_probe_id,
                started_at=time.perf_counter(),
            )
            self.attempts.append(attempt)

        # Confirm credentials are kept private (never logged or exposed)
        headers = kwargs.get("headers", {})
        if "Authorization" in headers:
            auth_val = headers["Authorization"]
            assert auth_val.startswith("Bearer "), "Authorization header must use Bearer token format"

        # Explicitly disallow automatic redirects in the probe to count and inspect every transmission
        kwargs["allow_redirects"] = False

        # Fire worker start hook if configured
        if self.on_worker_start is not None:
            try:
                self.on_worker_start()
            except Exception:
                pass

        # Wait on pause event if configured (for event-controlled asynchronous testing)
        if self.pause_event is not None:
            self.pause_event.wait()

        if self.dry_run:
            if self.simulated_delay_s > 0:
                time.sleep(self.simulated_delay_s)

            t_end = time.perf_counter()
            with self._lock:
                attempt.ended_at = t_end
                attempt.roundtrip_ms = (t_end - attempt.started_at) * 1000.0
                if attempt.caller_timed_out:
                    attempt.late_completed = True

            # Handle simulated redirect
            if self.simulated_is_redirect or (self.simulated_status_code in (301, 302, 303, 307, 308)):
                with self._lock:
                    attempt.is_redirect = True
                    attempt.status_code = self.simulated_status_code or 302
                raise RedirectRejectedError(
                    f"HTTP redirect {attempt.status_code} rejected in latency probe to prevent untracked transmissions"
                )

            # Handle simulated exception
            if self.simulated_exception:
                with self._lock:
                    attempt.error = str(self.simulated_exception)
                raise self.simulated_exception

            # Normal dry-run mock response
            resp = MagicMock()
            resp.status_code = self.simulated_status_code or 200
            resp.is_redirect = False

            req_json = kwargs.get("json", {})
            doc_str = req_json.get("state", {}).get("document", "")
            if "none" in doc_str.lower() or "unexpandable" in doc_str.lower():
                choice = "none"
                conf = 0.80
            else:
                choice = "lexical_expansion"
                conf = 0.91

            resp_data = {
                "model": "laya-rl-agent",
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": choice,
                        "confidence": 0.45,
                        "answer_confidence": conf,
                    }
                },
                "usage": {"input_tokens": 85, "output_tokens": 0},
                "server_latency_ms": self.simulated_server_latency_ms,
            }
            resp.json.return_value = resp_data
            with self._lock:
                attempt.status_code = resp.status_code
                attempt.response_data = resp_data
                attempt.server_latency_ms = self.simulated_server_latency_ms
            return resp

        # Real network dispatch
        try:
            resp = self._orig_post(*args, **kwargs)
            t_end = time.perf_counter()
            with self._lock:
                attempt.ended_at = t_end
                attempt.roundtrip_ms = (t_end - attempt.started_at) * 1000.0
                attempt.status_code = resp.status_code
                if attempt.caller_timed_out:
                    attempt.late_completed = True

            # Check and reject HTTP redirects
            if (
                getattr(resp, "is_redirect", False)
                or resp.status_code in (301, 302, 303, 307, 308)
                or "Location" in getattr(resp, "headers", {})
            ):
                with self._lock:
                    attempt.is_redirect = True
                raise RedirectRejectedError(
                    f"HTTP redirect {resp.status_code} rejected in latency probe to prevent untracked transmissions"
                )

            try:
                resp_data = resp.json()
                with self._lock:
                    attempt.response_data = resp_data
                    srv_lat = resp_data.get("server_latency_ms") or resp_data.get("latency_ms")
                    if srv_lat is not None:
                        attempt.server_latency_ms = float(srv_lat)
            except Exception:
                with self._lock:
                    attempt.response_data = {}
                    attempt.server_latency_ms = None
            return resp
        except Exception as exc:
            t_end = time.perf_counter()
            with self._lock:
                if attempt.ended_at is None:
                    attempt.ended_at = t_end
                    attempt.roundtrip_ms = (t_end - attempt.started_at) * 1000.0
                if attempt.caller_timed_out:
                    attempt.late_completed = True
                attempt.error = str(exc)
            raise

    def get_attempts_since(self, marker: int) -> list[HttpAttemptRecord]:
        """Return all attempt records created since the specified index marker."""
        with self._lock:
            return list(self.attempts[marker:])

    def get_latest_attempt(self) -> HttpAttemptRecord | None:
        """Return the most recently created attempt record, if any."""
        with self._lock:
            return self.attempts[-1] if self.attempts else None


class FidelityMockSequence:
    """Controls fidelity check behavior across probe queries."""

    def __init__(self, mode: str = "lexical") -> None:
        self.calls = 0
        self.mode = mode

    def __call__(self, q: str, contexts: Any, **kwargs: Any) -> float:
        self.calls += 1
        if self.mode == "lexical":
            if self.calls == 1:
                from services.retrieval.fidelity_check import CoverageError

                raise CoverageError("Initial fidelity check failed coverage: Missing turnover coverage")
            return 1.0
        else:  # mode == "none"
            from services.retrieval.fidelity_check import CoverageError

            raise CoverageError("Unexpandable general concept")


def evaluate_probe_verdict(
    metrics: list[ProbeMetricRecord],
    max_phase_ms: float = 2500.0,
) -> tuple[int, str, list[str]]:
    """Strictly evaluate readiness criteria across all probe metrics.

    Enforces:
    1. Maximum five actual HTTP transmissions across all attempts.
    2. Zero redirects.
    3. Completion within the 2500ms shared escalation phase deadline.
    4. Observed accepted routes and actual lexical expansion for lexical queries.
    5. Observed accepted none decision and clean refusal for none queries.
    6. Rejection of fallback delivery: deterministic fallback delivery never proves provider readiness.

    Returns:
        tuple of (exit_code, verdict_label, list_of_reasons)
        exit_code 0: PASS
        exit_code 1: FAIL (LATENCY BLOCKER)
        exit_code 2: FAIL (FUNCTIONAL DEFECT)
    """
    failures: list[str] = []
    latency_failures: list[str] = []

    total_attempts = sum(m.attempts_count for m in metrics)
    if total_attempts > MAX_ALLOWED_PROBE_REQUESTS:
        failures.append(
            f"Transmission ceiling exceeded: {total_attempts} attempts > {MAX_ALLOWED_PROBE_REQUESTS} allowed"
        )

    for idx, m in enumerate(metrics, 1):
        if m.is_redirect:
            failures.append(f"Request {idx} received an HTTP redirect, which is disallowed in probe")

        # Check phase deadline
        elapsed = m.pipeline_total_ms or m.client_roundtrip_ms or 0.0
        if elapsed > max_phase_ms or not m.completed_within_phase_deadline:
            latency_failures.append(
                f"Request {idx} ({m.probe_id}) elapsed {elapsed:.1f}ms exceeding phase deadline {max_phase_ms:.1f}ms"
            )

        if m.fallback:
            failures.append(
                f"Request {idx} used fallback delivery; fallback delivery does not prove provider readiness"
            )

        if not m.dispatched:
            failures.append(f"Request {idx} was not dispatched to provider")
            continue

        if not m.valid_response:
            failures.append(f"Request {idx} did not receive a valid provider response")
            continue

        if not m.route_accepted:
            failures.append(f"Request {idx} proposed route {m.proposed_route} was not accepted")
            continue

        # Require actual observed provider route to match scenario and reject missing telemetry
        if not m.proposed_route or m.proposed_route in ("unknown", "None", ""):
            failures.append(f"Request {idx} missing provider route telemetry")
        elif m.query_type == "lexical_expansion" and m.proposed_route != "lexical_expansion":
            failures.append(
                f"Request {idx} observed provider route '{m.proposed_route}' does not match expected lexical_expansion"
            )
        elif m.query_type == "none" and m.proposed_route != "none":
            failures.append(
                f"Request {idx} observed provider route '{m.proposed_route}' does not match expected route none (rejected even if pipeline refused)"
            )

        # Target specific verification
        if m.query_type == "lexical_expansion":
            if m.target == "compiled_pipeline":
                if not m.expansion_executed:
                    failures.append(f"Request {idx} did not execute lexical expansion")
                if m.delivery_outcome != "DELIVERED":
                    failures.append(f"Request {idx} outcome {m.delivery_outcome} was not DELIVERED")
        elif m.query_type == "none":
            if m.target == "compiled_pipeline":
                if m.expansion_executed:
                    failures.append(f"Request {idx} unexpectedly executed expansion for route none")
                if m.delivery_outcome != "REFUSAL":
                    failures.append(f"Request {idx} outcome {m.delivery_outcome} was not REFUSAL")

    if latency_failures:
        return 1, "FAIL (LATENCY BLOCKER)", latency_failures + failures
    elif failures:
        return 2, "FAIL (FUNCTIONAL DEFECT)", failures
    else:
        return 0, "PASS", []


def run_offline_negative_controls(verbose: bool = True) -> list[str]:
    """Execute negative controls proving that probe assertions catch defects.

    Negative controls verified:
    1. Stale timeout metrics: Timed out attempts never inherit prior request roundtrip or server latency.
    2. Fallback-only output: Deterministic fallback delivery is rejected and cannot prove provider readiness.
    3. Phase expiry: Escalations exceeding the 2500ms phase ceiling fail with latency blocker.
    4. Redirects: HTTP redirects are rejected before untracked transmissions occur.
    5. Attempted sixth transmission: Pre-dispatch validation ceiling strictly blocks a sixth request.
    """
    passed_controls: list[str] = []

    # -------------------------------------------------------------------------
    # Negative Control 1: Stale Timeout Metrics
    # -------------------------------------------------------------------------
    spy1 = BoundedNetworkSpy(dry_run=True)
    with patch("requests.post", side_effect=spy1.spy_post):
        svc = LayaDecisionService(endpoint_url="https://mock.laya.local/v1/systemone", api_key="key1")
        req1 = _build_synthetic_request(query="Query 1", gap="Gap 1")

        # First request succeeds
        m0 = len(spy1.attempts)
        svc.decide(req1, deadline_remaining_ms=2500.0, ledger=LayaBudgetLedger())
        att1 = spy1.get_attempts_since(m0)[-1]
        assert att1.roundtrip_ms is not None, "Attempt 1 must record roundtrip"
        assert att1.server_latency_ms == 32.5, "Attempt 1 must record server latency"

        # Second request simulates timeout exception
        spy1.simulated_exception = requests.exceptions.Timeout("Endpoint timed out")
        spy1.simulated_delay_s = 0.05
        m1 = len(spy1.attempts)
        dec2 = svc.decide(req1, deadline_remaining_ms=2500.0, ledger=LayaBudgetLedger())
        att2 = spy1.get_attempts_since(m1)[-1]

        # Verify no stale metric inheritance
        assert att2.error is not None and "timed out" in att2.error.lower(), "Attempt 2 must record timeout error"
        assert att2.server_latency_ms is None, "Attempt 2 must NOT inherit server latency from Attempt 1"
        assert att2.roundtrip_ms != att1.roundtrip_ms, "Attempt 2 roundtrip must be independent of Attempt 1"
        assert dec2.execution_status in ("timeout", "provider_unavailable"), "Decision status must reflect timeout"
        passed_controls.append("Negative Control 1: Stale timeout metrics rejected (zero measurement leakage)")

    # -------------------------------------------------------------------------
    # Negative Control 2: Fallback-Only Output
    # -------------------------------------------------------------------------
    rec_fallback = ProbeMetricRecord(
        probe_id="ctrl_fallback_delivery",
        target="compiled_pipeline",
        request_sequence="subsequent_request",
        query_type="lexical_expansion",
        client_roundtrip_ms=45.0,
        server_latency_ms=None,
        unaccounted_elapsed_ms=None,
        dispatched=True,
        valid_response=False,
        proposed_route="lexical_expansion",
        route_accepted=False,
        expansion_executed=True,
        fallback=True,  # Provider failed, fallback delivered text
        execution_status="provider_unavailable",
        pipeline_total_ms=120.0,
        delivery_outcome="DELIVERED",
        completed_within_phase_deadline=True,
    )
    code_fb, _, reasons_fb = evaluate_probe_verdict([rec_fallback])
    assert code_fb == 2, f"Fallback-only delivery must evaluate to code 2 (got {code_fb})"
    assert any("fallback delivery does not prove provider readiness" in r for r in reasons_fb), (
        "Verdict evaluation must explicitly state that fallback delivery does not prove provider readiness"
    )
    passed_controls.append("Negative Control 2: Fallback-only delivery rejected (provider readiness not proven)")

    # -------------------------------------------------------------------------
    # Negative Control 3: Phase Expiry
    # -------------------------------------------------------------------------
    rec_slow = ProbeMetricRecord(
        probe_id="ctrl_phase_expiry",
        target="compiled_pipeline",
        request_sequence="subsequent_request",
        query_type="lexical_expansion",
        client_roundtrip_ms=2600.0,
        server_latency_ms=30.0,
        unaccounted_elapsed_ms=2570.0,
        dispatched=True,
        valid_response=True,
        proposed_route="lexical_expansion",
        route_accepted=True,
        expansion_executed=True,
        fallback=False,
        execution_status="completed",
        pipeline_total_ms=2650.0,  # Exceeds 2500ms phase ceiling
        delivery_outcome="DELIVERED",
        completed_within_phase_deadline=False,
    )
    code_slow, _, reasons_slow = evaluate_probe_verdict([rec_slow], max_phase_ms=2500.0)
    assert code_slow == 1, f"Phase expiry must evaluate to code 1 LATENCY BLOCKER (got {code_slow})"
    assert any("exceeding phase deadline" in r for r in reasons_slow), "Verdict must identify phase deadline overrun"
    passed_controls.append("Negative Control 3: Phase expiry caught (evaluated as latency blocker)")

    # -------------------------------------------------------------------------
    # Negative Control 4: Redirects
    # -------------------------------------------------------------------------
    spy_redir = BoundedNetworkSpy(dry_run=True)
    spy_redir.simulated_is_redirect = True
    spy_redir.simulated_status_code = 301
    try:
        spy_redir.spy_post("https://mock.laya.local/v1/systemone", json={})
        assert False, "Redirect must raise RedirectRejectedError"
    except RedirectRejectedError:
        pass
    assert spy_redir.attempts[-1].is_redirect is True, "Attempt record must flag is_redirect"

    rec_redir = ProbeMetricRecord(
        probe_id="ctrl_redirect",
        target="compiled_pipeline",
        request_sequence="first_request",
        query_type="lexical_expansion",
        client_roundtrip_ms=50.0,
        server_latency_ms=None,
        unaccounted_elapsed_ms=None,
        dispatched=True,
        valid_response=False,
        proposed_route="lexical_expansion",
        route_accepted=False,
        expansion_executed=False,
        fallback=True,
        execution_status="error",
        is_redirect=True,
    )
    code_red, _, reasons_red = evaluate_probe_verdict([rec_redir])
    assert code_red == 2, f"Redirect must evaluate to code 2 (got {code_red})"
    assert any("redirect" in r.lower() for r in reasons_red), "Verdict must reject redirects"
    passed_controls.append("Negative Control 4: Redirects rejected (untracked transmissions blocked)")

    # -------------------------------------------------------------------------
    # Negative Control 5: Attempted Sixth Transmission
    # -------------------------------------------------------------------------
    spy_ceiling = BoundedNetworkSpy(dry_run=True, max_requests=5)
    for _ in range(5):
        spy_ceiling.spy_post("https://mock.laya.local/v1/systemone", json={})
    assert spy_ceiling.request_count == 5, "Must reach ceiling of 5 requests"

    try:
        spy_ceiling.spy_post("https://mock.laya.local/v1/systemone", json={})
        assert False, "Sixth transmission must be blocked by pre-dispatch validation"
    except RuntimeError as re:
        assert "reached maximum ceiling" in str(re), "Exception must specify maximum ceiling reached"
    assert spy_ceiling.request_count == 5, "Request count must not increment past ceiling"
    passed_controls.append("Negative Control 5: Sixth transmission blocked before dispatch (strict ceiling enforced)")

    # -------------------------------------------------------------------------
    # Negative Control 6: Mismatched Route in None Scenario
    # -------------------------------------------------------------------------
    rec_mismatched = ProbeMetricRecord(
        probe_id="ctrl_mismatched_route",
        target="compiled_pipeline",
        request_sequence="subsequent_request",
        query_type="none",
        client_roundtrip_ms=45.0,
        server_latency_ms=30.0,
        unaccounted_elapsed_ms=15.0,
        dispatched=True,
        valid_response=True,
        proposed_route="lexical_expansion",  # Mismatched route for none scenario
        route_accepted=True,
        expansion_executed=False,
        fallback=False,
        execution_status="approved",
        pipeline_total_ms=90.0,
        delivery_outcome="REFUSAL",  # Pipeline refused, but provider route was lexical_expansion
        completed_within_phase_deadline=True,
    )
    code_mm, _, reasons_mm = evaluate_probe_verdict([rec_mismatched])
    assert code_mm == 2, f"Mismatched route on none scenario must evaluate to code 2 (got {code_mm})"
    assert any("does not match expected route none" in r for r in reasons_mm), (
        "Verdict must reject lexical_expansion returned during none scenario"
    )
    passed_controls.append("Negative Control 6: Mismatched route in none scenario rejected (even on pipeline refusal)")

    # -------------------------------------------------------------------------
    # Negative Control 7: Missing Route Telemetry
    # -------------------------------------------------------------------------
    rec_missing_route = ProbeMetricRecord(
        probe_id="ctrl_missing_route",
        target="compiled_pipeline",
        request_sequence="subsequent_request",
        query_type="lexical_expansion",
        client_roundtrip_ms=45.0,
        server_latency_ms=30.0,
        unaccounted_elapsed_ms=15.0,
        dispatched=True,
        valid_response=True,
        proposed_route="",  # Missing route telemetry
        route_accepted=True,
        expansion_executed=True,
        fallback=False,
        execution_status="approved",
        pipeline_total_ms=90.0,
        delivery_outcome="DELIVERED",
        completed_within_phase_deadline=True,
    )
    code_mr, _, reasons_mr = evaluate_probe_verdict([rec_missing_route])
    assert code_mr == 2, f"Missing route telemetry must evaluate to code 2 (got {code_mr})"
    assert any("missing provider route telemetry" in r for r in reasons_mr), "Verdict must reject missing route telemetry"
    passed_controls.append("Negative Control 7: Missing route telemetry rejected")

    # -------------------------------------------------------------------------
    # Negative Control 8: Event-Controlled Delayed Worker & Late Completion
    # -------------------------------------------------------------------------
    spy_async = BoundedNetworkSpy(dry_run=True)
    worker_started_ev = threading.Event()
    worker_release_ev = threading.Event()

    spy_async.on_worker_start = lambda: worker_started_ev.set()
    spy_async.pause_event = worker_release_ev

    with patch("requests.post", side_effect=spy_async.spy_post):
        svc_async = LayaDecisionService(endpoint_url="https://mock.laya.local/v1/systemone", api_key="key_async")
        req_async = _build_synthetic_request(query="Async Query", gap="Async Gap")

        spy_async.set_probe_id("probe_async_req1")
        dec_async1 = svc_async.decide(req_async, deadline_remaining_ms=520.0, ledger=LayaBudgetLedger())
        assert dec_async1.execution_status == "timeout", "Caller must time out"
        spy_async.record_caller_timeout("probe_async_req1")

        att_async1 = spy_async.get_attempts_for_probe("probe_async_req1")[0]
        assert att_async1.caller_timed_out is True, "Attempt 1 must record caller_timed_out"
        assert att_async1.ended_at is None, "Attempt 1 must not have ended while paused"
        assert att_async1.late_completed is False, "Attempt 1 must not be marked late_completed yet"

        # Subsequent request runs and completes without delay
        spy_async.pause_event = None
        spy_async.on_worker_start = None
        spy_async.set_probe_id("probe_async_req2")
        dec_async2 = svc_async.decide(req_async, deadline_remaining_ms=2500.0, ledger=LayaBudgetLedger())
        assert dec_async2.execution_status == "approved"
        att_async2 = spy_async.get_attempts_for_probe("probe_async_req2")[0]
        assert att_async2.attempt_id == 2
        assert att_async2.probe_id == "probe_async_req2"
        assert att_async2.caller_timed_out is False
        assert att_async2.late_completed is False
        assert att_async2.ended_at is not None

        # Eventual original worker completion
        worker_release_ev.set()
        time.sleep(0.05)
        assert att_async1.late_completed is True, "Attempt 1 must be marked late_completed upon eventual finish"
        assert att_async1.ended_at is not None, "Attempt 1 must record ended_at"
        assert dec_async1.execution_status == "timeout", "Caller outcome must not be overwritten"

    passed_controls.append("Negative Control 8: Event-controlled delayed worker and late completion verified")

    if verbose:
        print("\n" + "=" * 90)
        print("OFFLINE NEGATIVE CONTROLS VERIFIED")
        print("=" * 90)
        for ctrl_desc in passed_controls:
            print(f"  [PASS] {ctrl_desc}")

    return passed_controls


def run_latency_probe(endpoint_url: str, api_key: str, dry_run: bool = True) -> int:
    """Run the 5-request bounded latency probe with per-attempt measurement tracking."""
    print("=" * 80)
    print("BOUNDED LAYA LATENCY READINESS PROBE (SPEC 0008)")
    print("=" * 80)
    print(f"Target Endpoint URL:  {endpoint_url or 'https://mock.laya.local/v1/systemone (dry-run)'}")
    print(f"Authentication Key:   {_mask_credential(api_key)}")
    print(f"Execution Mode:       {'OFFLINE PREPARATION (Dry Run)' if dry_run else 'LIVE NETWORK (Opt-In)'}")
    print(f"Request Ceiling:      At most {MAX_ALLOWED_PROBE_REQUESTS} requests total (enforced before dispatch)")
    print(f"Escalation Phase SLA: 2500.0 ms hard ceiling")
    print("-" * 80)

    effective_url = endpoint_url or "https://mock.laya.local/v1/systemone"
    effective_key = api_key or "dry_run_key"

    spy = BoundedNetworkSpy(dry_run=dry_run)
    metrics: list[ProbeMetricRecord] = []
    repo = _setup_synthetic_repository()

    # Offline synthesis and reranking doubles
    mock_llm_response = {
        "answer": "The fiscal year 2026 turnover was $450 million.",
        "citations": ["chk_probe_turnover"],
        "usage": {"input_tokens": 120, "output_tokens": 18},
    }
    mock_verifier = MockOfflineVerifier(support_status="ASSERTED")

    def _mock_rerank(q: str, chunks: Any, top_k: int = 6, return_mode: bool = False) -> Any:
        c_list = list(chunks)
        return (c_list[:top_k], "mocked") if return_mode else c_list[:top_k]

    with patch("requests.post", side_effect=spy.spy_post):
        # ---------------------------------------------------------------------
        # SECTION 1: Direct Endpoint Probes (Requests 1 and 2)
        # ---------------------------------------------------------------------
        print("\n[SECTION 1] Direct Endpoint Decision Service Probes")
        service = LayaDecisionService(endpoint_url=effective_url, api_key=effective_key)

        # Request 1: Direct Endpoint (First Request)
        print("\n--> Request 1/5: Direct Endpoint (First Request, Lexical Intent)...")
        spy.set_probe_id("probe_direct_req1")
        ledger_1 = LayaBudgetLedger()
        req_1 = _build_synthetic_request(
            query="What was the turnover for fiscal year 2026?",
            gap="Initial fidelity check failed coverage: Missing turnover coverage",
        )
        marker_1 = len(spy.attempts)
        t_start = time.perf_counter()
        dec_1 = service.decide(req_1, deadline_remaining_ms=2500.0, ledger=ledger_1)
        total_time_1 = (time.perf_counter() - t_start) * 1000.0
        if dec_1.execution_status == "timeout":
            spy.record_caller_timeout("probe_direct_req1")

        attempts_1 = spy.get_attempts_for_probe("probe_direct_req1") or spy.get_attempts_since(marker_1)
        att_1 = attempts_1[-1] if attempts_1 else None
        roundtrip_1 = att_1.roundtrip_ms if att_1 else None
        srv_lat_1 = att_1.server_latency_ms if att_1 else None
        unacc_1 = (
            (roundtrip_1 - srv_lat_1)
            if (roundtrip_1 is not None and srv_lat_1 is not None)
            else None
        )

        rec_1 = ProbeMetricRecord(
            probe_id="probe_direct_req1",
            target="direct_endpoint",
            request_sequence="first_request",
            query_type="lexical_expansion",
            client_roundtrip_ms=roundtrip_1,
            server_latency_ms=srv_lat_1,
            unaccounted_elapsed_ms=unacc_1,
            dispatched=bool(service.last_dispatched),
            valid_response=bool(service.last_valid_response),
            proposed_route=str(dec_1.route) if getattr(dec_1, "route", None) is not None else "",
            route_accepted=bool(service.last_route_accepted and dec_1.execution_status == "approved"),
            expansion_executed=False,
            fallback=bool(service.last_fallback or dec_1.execution_status != "approved"),
            execution_status=dec_1.execution_status,
            pipeline_total_ms=total_time_1,
            delivery_outcome="APPROVED" if dec_1.execution_status == "approved" else dec_1.execution_status.upper(),
            phase_deadline_ms=2500.0,
            completed_within_phase_deadline=bool(total_time_1 <= 2500.0),
            token_usage={"inputTokens": ledger_1.total_input_tokens, "outputTokens": ledger_1.total_output_tokens},
            error=att_1.error if att_1 else None,
            is_redirect=att_1.is_redirect if att_1 else False,
            attempts_count=len(attempts_1),
        )
        metrics.append(rec_1)
        print(f"    Status: {dec_1.execution_status} | Route: {dec_1.route} | Conf: {dec_1.confidence:.4f}")
        print(f"    Client Roundtrip: {roundtrip_1:.2f} ms | Server Latency: {srv_lat_1} ms" if roundtrip_1 else "    Not dispatched")

        # Request 2: Direct Endpoint (Subsequent Request, default client behavior)
        print("\n--> Request 2/5: Direct Endpoint (Subsequent Request, default client behavior)...")
        spy.set_probe_id("probe_direct_req2")
        ledger_2 = LayaBudgetLedger()
        req_2 = _build_synthetic_request(
            query="What was the turnover for fiscal year 2026?",
            gap="Initial fidelity check failed coverage: Missing turnover coverage",
        )
        marker_2 = len(spy.attempts)
        t_start = time.perf_counter()
        dec_2 = service.decide(req_2, deadline_remaining_ms=2500.0, ledger=ledger_2)
        total_time_2 = (time.perf_counter() - t_start) * 1000.0
        if dec_2.execution_status == "timeout":
            spy.record_caller_timeout("probe_direct_req2")

        attempts_2 = spy.get_attempts_for_probe("probe_direct_req2") or spy.get_attempts_since(marker_2)
        att_2 = attempts_2[-1] if attempts_2 else None
        roundtrip_2 = att_2.roundtrip_ms if att_2 else None
        srv_lat_2 = att_2.server_latency_ms if att_2 else None
        unacc_2 = (
            (roundtrip_2 - srv_lat_2)
            if (roundtrip_2 is not None and srv_lat_2 is not None)
            else None
        )

        rec_2 = ProbeMetricRecord(
            probe_id="probe_direct_req2",
            target="direct_endpoint",
            request_sequence="subsequent_request",
            query_type="lexical_expansion",
            client_roundtrip_ms=roundtrip_2,
            server_latency_ms=srv_lat_2,
            unaccounted_elapsed_ms=unacc_2,
            dispatched=bool(service.last_dispatched),
            valid_response=bool(service.last_valid_response),
            proposed_route=str(dec_2.route) if getattr(dec_2, "route", None) is not None else "",
            route_accepted=bool(service.last_route_accepted and dec_2.execution_status == "approved"),
            expansion_executed=False,
            fallback=bool(service.last_fallback or dec_2.execution_status != "approved"),
            execution_status=dec_2.execution_status,
            pipeline_total_ms=total_time_2,
            delivery_outcome="APPROVED" if dec_2.execution_status == "approved" else dec_2.execution_status.upper(),
            phase_deadline_ms=2500.0,
            completed_within_phase_deadline=bool(total_time_2 <= 2500.0),
            token_usage={"inputTokens": ledger_2.total_input_tokens, "outputTokens": ledger_2.total_output_tokens},
            error=att_2.error if att_2 else None,
            is_redirect=att_2.is_redirect if att_2 else False,
            attempts_count=len(attempts_2),
        )
        metrics.append(rec_2)
        print(f"    Status: {dec_2.execution_status} | Route: {dec_2.route} | Conf: {dec_2.confidence:.4f}")
        print(f"    Client Roundtrip: {roundtrip_2:.2f} ms | Server Latency: {srv_lat_2} ms" if roundtrip_2 else "    Not dispatched")

        # ---------------------------------------------------------------------
        # SECTION 2: Compiled LangGraph Pipeline Probes (Requests 3, 4, and 5)
        # ---------------------------------------------------------------------
        print("\n[SECTION 2] Compiled LangGraph Pipeline End-to-End Probes")

        # Request 3: Compiled Pipeline (First Pipeline Request)
        print("\n--> Request 3/5: Compiled Pipeline (First Pipeline Request, Lexical Expansion)...")
        with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response), \
             patch("services.langgraph_pipeline._get_repo", return_value=repo), \
             patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=FidelityMockSequence(mode="lexical")):

            spy.set_probe_id("probe_pipeline_req3")
            marker_3 = len(spy.attempts)
            t_start = time.perf_counter()
            resp_3 = pipeline.run(
                query="What was the turnover for fiscal year 2026?",
                workspace_id="ws_latency_probe",
                laya_enabled=True,
                laya_endpoint_url=effective_url,
                laya_api_key=effective_key,
                verifier=mock_verifier,
                require_delivery_gate=True,
                caller_deadline_ms=2500.0,
            )
            total_time_3 = (time.perf_counter() - t_start) * 1000.0

            cands_3 = resp_3.retrieval_candidates or {}
            if str(cands_3.get("laya_status")) == "timeout":
                spy.record_caller_timeout("probe_pipeline_req3")
            attempts_3 = spy.get_attempts_for_probe("probe_pipeline_req3") or spy.get_attempts_since(marker_3)
            att_3 = attempts_3[-1] if attempts_3 else None
            roundtrip_3 = att_3.roundtrip_ms if att_3 else None
            srv_lat_3 = att_3.server_latency_ms if att_3 else None
            unacc_3 = (
                (roundtrip_3 - srv_lat_3)
                if (roundtrip_3 is not None and srv_lat_3 is not None)
                else None
            )

            route_acc_3 = bool(cands_3.get("laya_route_accepted"))
            exp_exec_3 = bool(cands_3.get("laya_expansion_executed"))
            fallback_3 = bool(cands_3.get("laya_fallback"))
            status_3 = str(cands_3.get("laya_status", "unknown"))

            if resp_3.answer == "NOT_FOUND":
                outcome_3 = "REFUSAL"
            elif "$450 million" in resp_3.answer:
                outcome_3 = "DELIVERED"
            else:
                outcome_3 = "UNKNOWN"

            observed_route_3 = cands_3.get("laya_route")
            proposed_route_3 = str(observed_route_3) if observed_route_3 is not None else ""

            rec_3 = ProbeMetricRecord(
                probe_id="probe_pipeline_req3",
                target="compiled_pipeline",
                request_sequence="first_request",
                query_type="lexical_expansion",
                client_roundtrip_ms=roundtrip_3,
                server_latency_ms=srv_lat_3,
                unaccounted_elapsed_ms=unacc_3,
                dispatched=bool(cands_3.get("laya_dispatched")),
                valid_response=bool(cands_3.get("laya_provider_success")),
                proposed_route=proposed_route_3,
                route_accepted=route_acc_3,
                expansion_executed=exp_exec_3,
                fallback=fallback_3,
                execution_status=status_3,
                pipeline_total_ms=total_time_3,
                delivery_outcome=outcome_3,
                phase_deadline_ms=2500.0,
                completed_within_phase_deadline=bool(total_time_3 <= 2500.0),
                error=att_3.error if att_3 else None,
                is_redirect=att_3.is_redirect if att_3 else False,
                attempts_count=len(attempts_3),
            )
            metrics.append(rec_3)
            print(f"    Outcome: {rec_3.delivery_outcome} | Synthesis Calls: {resp_3.synthesis_calls}")
            print(f"    Client Roundtrip: {roundtrip_3:.2f} ms | Pipeline Total: {total_time_3:.2f} ms" if roundtrip_3 else f"    Pipeline Total: {total_time_3:.2f} ms")

        # Request 4: Compiled Pipeline (Subsequent Pipeline Request, Lexical Expansion)
        print("\n--> Request 4/5: Compiled Pipeline (Subsequent Request, Lexical Expansion)...")
        with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response), \
             patch("services.langgraph_pipeline._get_repo", return_value=repo), \
             patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=FidelityMockSequence(mode="lexical")):

            spy.set_probe_id("probe_pipeline_req4")
            marker_4 = len(spy.attempts)
            t_start = time.perf_counter()
            resp_4 = pipeline.run(
                query="What was the turnover for fiscal year 2026?",
                workspace_id="ws_latency_probe",
                laya_enabled=True,
                laya_endpoint_url=effective_url,
                laya_api_key=effective_key,
                verifier=mock_verifier,
                require_delivery_gate=True,
                caller_deadline_ms=2500.0,
            )
            total_time_4 = (time.perf_counter() - t_start) * 1000.0

            cands_4 = resp_4.retrieval_candidates or {}
            if str(cands_4.get("laya_status")) == "timeout":
                spy.record_caller_timeout("probe_pipeline_req4")
            attempts_4 = spy.get_attempts_for_probe("probe_pipeline_req4") or spy.get_attempts_since(marker_4)
            att_4 = attempts_4[-1] if attempts_4 else None
            roundtrip_4 = att_4.roundtrip_ms if att_4 else None
            srv_lat_4 = att_4.server_latency_ms if att_4 else None
            unacc_4 = (
                (roundtrip_4 - srv_lat_4)
                if (roundtrip_4 is not None and srv_lat_4 is not None)
                else None
            )

            route_acc_4 = bool(cands_4.get("laya_route_accepted"))
            exp_exec_4 = bool(cands_4.get("laya_expansion_executed"))
            fallback_4 = bool(cands_4.get("laya_fallback"))
            status_4 = str(cands_4.get("laya_status", "unknown"))

            if resp_4.answer == "NOT_FOUND":
                outcome_4 = "REFUSAL"
            elif "$450 million" in resp_4.answer:
                outcome_4 = "DELIVERED"
            else:
                outcome_4 = "UNKNOWN"

            observed_route_4 = cands_4.get("laya_route")
            proposed_route_4 = str(observed_route_4) if observed_route_4 is not None else ""

            rec_4 = ProbeMetricRecord(
                probe_id="probe_pipeline_req4",
                target="compiled_pipeline",
                request_sequence="subsequent_request",
                query_type="lexical_expansion",
                client_roundtrip_ms=roundtrip_4,
                server_latency_ms=srv_lat_4,
                unaccounted_elapsed_ms=unacc_4,
                dispatched=bool(cands_4.get("laya_dispatched")),
                valid_response=bool(cands_4.get("laya_provider_success")),
                proposed_route=proposed_route_4,
                route_accepted=route_acc_4,
                expansion_executed=exp_exec_4,
                fallback=fallback_4,
                execution_status=status_4,
                pipeline_total_ms=total_time_4,
                delivery_outcome=outcome_4,
                phase_deadline_ms=2500.0,
                completed_within_phase_deadline=bool(total_time_4 <= 2500.0),
                error=att_4.error if att_4 else None,
                is_redirect=att_4.is_redirect if att_4 else False,
                attempts_count=len(attempts_4),
            )
            metrics.append(rec_4)
            print(f"    Outcome: {rec_4.delivery_outcome} | Synthesis Calls: {resp_4.synthesis_calls}")
            print(f"    Client Roundtrip: {roundtrip_4:.2f} ms | Pipeline Total: {total_time_4:.2f} ms" if roundtrip_4 else f"    Pipeline Total: {total_time_4:.2f} ms")

        # Request 5: Compiled Pipeline (Subsequent Pipeline Request, Route None Refusal)
        print("\n--> Request 5/5: Compiled Pipeline (Subsequent Request, Route None Refusal)...")
        with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response), \
             patch("services.langgraph_pipeline._get_repo", return_value=repo), \
             patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=FidelityMockSequence(mode="none")):

            spy.set_probe_id("probe_pipeline_req5")
            marker_5 = len(spy.attempts)
            t_start = time.perf_counter()
            resp_5 = pipeline.run(
                query="Unexpandable general concept with no evidence in existence",
                workspace_id="ws_latency_probe",
                laya_enabled=True,
                laya_endpoint_url=effective_url,
                laya_api_key=effective_key,
                caller_deadline_ms=2500.0,
            )
            total_time_5 = (time.perf_counter() - t_start) * 1000.0

            cands_5 = resp_5.retrieval_candidates or {}
            if str(cands_5.get("laya_status")) == "timeout":
                spy.record_caller_timeout("probe_pipeline_req5")
            attempts_5 = spy.get_attempts_for_probe("probe_pipeline_req5") or spy.get_attempts_since(marker_5)
            att_5 = attempts_5[-1] if attempts_5 else None
            roundtrip_5 = att_5.roundtrip_ms if att_5 else None
            srv_lat_5 = att_5.server_latency_ms if att_5 else None
            unacc_5 = (
                (roundtrip_5 - srv_lat_5)
                if (roundtrip_5 is not None and srv_lat_5 is not None)
                else None
            )

            route_acc_5 = bool(cands_5.get("laya_route_accepted"))
            exp_exec_5 = bool(cands_5.get("laya_expansion_executed"))
            fallback_5 = bool(cands_5.get("laya_fallback"))
            status_5 = str(cands_5.get("laya_status", "unknown"))

            outcome_5 = "REFUSAL" if resp_5.answer == "NOT_FOUND" else "DELIVERED"
            observed_route_5 = cands_5.get("laya_route")
            proposed_route_5 = str(observed_route_5) if observed_route_5 is not None else ""

            rec_5 = ProbeMetricRecord(
                probe_id="probe_pipeline_req5",
                target="compiled_pipeline",
                request_sequence="subsequent_request",
                query_type="none",
                client_roundtrip_ms=roundtrip_5,
                server_latency_ms=srv_lat_5,
                unaccounted_elapsed_ms=unacc_5,
                dispatched=bool(cands_5.get("laya_dispatched")),
                valid_response=bool(cands_5.get("laya_provider_success")),
                proposed_route=proposed_route_5,
                route_accepted=route_acc_5,
                expansion_executed=exp_exec_5,
                fallback=fallback_5,
                execution_status=status_5,
                pipeline_total_ms=total_time_5,
                delivery_outcome=outcome_5,
                phase_deadline_ms=2500.0,
                completed_within_phase_deadline=bool(total_time_5 <= 2500.0),
                error=att_5.error if att_5 else None,
                is_redirect=att_5.is_redirect if att_5 else False,
                attempts_count=len(attempts_5),
            )
            metrics.append(rec_5)
            print(f"    Outcome: {rec_5.delivery_outcome} | Synthesis Calls: {resp_5.synthesis_calls}")
            print(f"    Client Roundtrip: {roundtrip_5:.2f} ms | Pipeline Total: {total_time_5:.2f} ms" if roundtrip_5 else f"    Pipeline Total: {total_time_5:.2f} ms")

    # -------------------------------------------------------------------------
    # PRINT STRUCTURED READINESS REPORT
    # -------------------------------------------------------------------------
    direct_records = [m for m in metrics if m.target == "direct_endpoint"]
    pipeline_records = [m for m in metrics if m.target == "compiled_pipeline"]

    print("\n" + "=" * 90)
    print("PROBE RESULTS: DIRECT ENDPOINT (SECTION 1)")
    print("=" * 90)
    h_direct = f"{'Req':<4} {'Sequence':<20} {'Query Intent':<18} {'Client(ms)':<12} {'Server(ms)':<12} {'Unacc(ms)':<12} {'Status':<12}"
    print(h_direct)
    print("-" * 90)
    for idx, m in enumerate(direct_records, 1):
        srv_str = f"{m.server_latency_ms:.1f}" if m.server_latency_ms is not None else "N/A"
        unacc_str = f"{m.unaccounted_elapsed_ms:.1f}" if m.unaccounted_elapsed_ms is not None else "N/A"
        cli_str = f"{m.client_roundtrip_ms:.1f}" if m.client_roundtrip_ms is not None else "N/A"
        print(
            f"{idx:<4} {m.request_sequence:<20} {m.query_type:<18} "
            f"{cli_str:<12} {srv_str:<12} {unacc_str:<12} "
            f"{m.execution_status:<12}"
        )

    print("\n" + "=" * 90)
    print("PROBE RESULTS: COMPILED PIPELINE (SECTION 2)")
    print("=" * 90)
    h_pipe = f"{'Req':<4} {'Sequence':<20} {'Query Intent':<18} {'Pipeline(ms)':<14} {'LayaCall(ms)':<14} {'Status':<12} {'Outcome':<10}"
    print(h_pipe)
    print("-" * 90)
    for idx, m in enumerate(pipeline_records, 3):
        pipe_str = f"{m.pipeline_total_ms:.1f}" if m.pipeline_total_ms is not None else "N/A"
        cli_str = f"{m.client_roundtrip_ms:.1f}" if m.client_roundtrip_ms is not None else "N/A"
        print(
            f"{idx:<4} {m.request_sequence:<20} {m.query_type:<18} "
            f"{pipe_str:<14} {cli_str:<14} "
            f"{m.execution_status:<12} {m.delivery_outcome or 'N/A':<10}"
        )

    print("\n" + "-" * 90)
    print("[ANALYSIS: OBSERVABLE TIMING BREAKDOWN]")

    first_req_times = [m.client_roundtrip_ms for m in metrics if m.request_sequence == "first_request" and m.client_roundtrip_ms is not None]
    subsequent_req_times = [m.client_roundtrip_ms for m in metrics if m.request_sequence == "subsequent_request" and m.client_roundtrip_ms is not None]
    avg_first_ms = sum(first_req_times) / len(first_req_times) if first_req_times else 0.0
    avg_subsequent_ms = sum(subsequent_req_times) / len(subsequent_req_times) if subsequent_req_times else 0.0

    print(f"1. First Request Average Client Roundtrip:        {avg_first_ms:.1f} ms")
    print(f"2. Subsequent Requests Average Client Roundtrip:   {avg_subsequent_ms:.1f} ms")

    server_latencies = [m.server_latency_ms for m in metrics if m.server_latency_ms is not None]
    if server_latencies:
        avg_server_ms = sum(server_latencies) / len(server_latencies)
        print(f"3. Average Reported Server Model Latency:         {avg_server_ms:.1f} ms")
        avg_unacc_ms = avg_subsequent_ms - avg_server_ms
        print(f"4. Average Subsequent Unaccounted Elapsed Time:   {avg_unacc_ms:.1f} ms")

    print("\n[METHODOLOGY & LIMITATION NOTES]")
    print("* Connection Behavior: Production LayaDecisionService uses default requests.post per call.")
    print("  TCP connection reuse is not verified across separate invocations; production behavior is left unmodified.")
    print("* Timing Attribution: Unaccounted elapsed time reflects client roundtrip minus reported server execution.")
    print("  It represents the complete path outside server model processing and is not attributed specifically to")
    print("  Cloudflare, TLS, or DNS without supporting network-level instrumentation.")
    print("* Statistical Reliability: Five requests provide boundary validation and operational sanity checks.")
    print("  Five samples do NOT provide statistical reliability or SLA percentiles (p95/p99).")

    # Evaluate strict probe verdict
    code, label, reasons = evaluate_probe_verdict(metrics, max_phase_ms=2500.0)

    if dry_run:
        # In dry run mode, execute all negative controls to confirm assertion robustness
        if code != 0:
            print("\n" + "=" * 90)
            print(f">> VERDICT: {label}")
            print("=" * 90)
            for r in reasons:
                print(f"   [FAIL] {r}")
            return code

        # Run offline negative controls
        run_offline_negative_controls(verbose=True)

        print("\n" + "=" * 90)
        print(">> VERDICT: HARNESS PASS; LIVE LATENCY READINESS UNKNOWN")
        print("=" * 90)
        print("Harness execution, contract logic, and negative controls verified using offline test doubles.")
        print("Real network readiness cannot be derived from mocked timings.")
        print("To measure live network latency, run with explicit opt-in: --live")
        return 0

    # Live Execution Verdict Output
    print("\n" + "=" * 90)
    print(f">> VERDICT: {label}")
    print("=" * 90)
    if code == 0:
        print(f"   Subsequent roundtrips ({avg_subsequent_ms:.1f} ms) completed within the 2500ms phase SLA.")
        print("   Both lexical expansion delivery and route none refusal verified via accepted provider routes.")
    else:
        for r in reasons:
            print(f"   [DEFECT] {r}")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded LAYA Latency Readiness Probe")
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Explicit opt-in to execute live HTTP requests against configured LAYA server (defaults to offline preparation)",
    )
    parser.add_argument(
        "--url",
        type=str,
        default="",
        help="Optional override for LAYA_ENDPOINT_URL",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default="",
        help="Optional override for LAYA_API_KEY",
    )
    args = parser.parse_args()

    # Read from environment or explicit flags
    url = args.url or os.environ.get("LAYA_ENDPOINT_URL") or getattr(settings, "LAYA_ENDPOINT_URL", "")
    key = args.api_key or os.environ.get("LAYA_API_KEY") or getattr(settings, "LAYA_API_KEY", "")

    dry_run = not args.live

    if not dry_run:
        if not url:
            print("[ERROR] LAYA_ENDPOINT_URL is not configured in environment or CLI flag.", file=sys.stderr)
            return 1
        if not key:
            print("[ERROR] LAYA_API_KEY is not configured in environment or CLI flag.", file=sys.stderr)
            return 1

    return run_latency_probe(endpoint_url=url, api_key=key, dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main())
