"""Unit and regression tests for Bounded LAYA Latency Readiness Probe.

Verifies:
1. Per-attempt HTTP records prevent stale measurement leakage across requests.
2. Late worker completions remain associated with their original attempt.
3. Fallback-only delivery is strictly rejected (does not prove provider readiness).
4. Observed accepted routes and actual lexical expansion required for lexical success.
5. Observed accepted none decision and clean refusal required for route none.
6. Phase deadline overruns (>2500ms) evaluated as latency blockers.
7. HTTP redirects are strictly rejected to prevent untracked transmissions.
8. Pre-dispatch validation ceiling strictly blocks a sixth transmission.
9. Offline dry-run harness pass depends on explicit positive assertions and negative controls.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

# Add scripts directory to path to import probe components
scripts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

from probe_laya_latency import (
    BoundedNetworkSpy,
    HttpAttemptRecord,
    ProbeMetricRecord,
    RedirectRejectedError,
    _build_synthetic_request,
    evaluate_probe_verdict,
    run_latency_probe,
    run_offline_negative_controls,
)
from services.retrieval.laya_gate import LayaBudgetLedger
from services.retrieval.laya_service import LayaDecisionService


class TestProbePerAttemptRecords:
    """Verifies per-attempt tracking and prevention of stale measurement leakage."""

    def test_per_attempt_records_prevent_stale_measurement_leakage(self) -> None:
        spy = BoundedNetworkSpy(dry_run=True)
        with patch("requests.post", side_effect=spy.spy_post):
            svc = LayaDecisionService(endpoint_url="https://mock.laya.local/v1/systemone", api_key="key1")
            req = _build_synthetic_request(query="Query 1", gap="Gap 1")

            # Attempt 1: Happy path
            m0 = len(spy.attempts)
            dec1 = svc.decide(req, deadline_remaining_ms=2500.0, ledger=LayaBudgetLedger())
            attempts_1 = spy.get_attempts_since(m0)
            assert len(attempts_1) == 1
            att1 = attempts_1[0]
            assert att1.roundtrip_ms is not None
            assert att1.server_latency_ms == 32.5
            assert att1.error is None
            assert dec1.execution_status == "approved"

            # Attempt 2: Simulating timeout
            spy.simulated_exception = requests.exceptions.Timeout("Read timed out")
            spy.simulated_delay_s = 0.03
            m1 = len(spy.attempts)
            dec2 = svc.decide(req, deadline_remaining_ms=2500.0, ledger=LayaBudgetLedger())
            attempts_2 = spy.get_attempts_since(m1)
            assert len(attempts_2) == 1
            att2 = attempts_2[0]

            # Critical assertions: Attempt 2 has its own metrics and zero leakage from Attempt 1
            assert att2.attempt_id == 2
            assert att2.error is not None and "timed out" in att2.error.lower()
            assert att2.server_latency_ms is None, "Attempt 2 must NOT inherit server_latency from Attempt 1"
            assert att2.roundtrip_ms != att1.roundtrip_ms
            assert dec2.execution_status in ("timeout", "provider_unavailable")

    def test_late_worker_completion_associated_with_original_attempt(self) -> None:
        """Confirms that late completions in worker threads update their own attempt record without manual population."""
        spy = BoundedNetworkSpy(dry_run=True)
        worker_started = threading.Event()
        worker_release = threading.Event()

        spy.on_worker_start = lambda: worker_started.set()
        spy.pause_event = worker_release

        with patch("requests.post", side_effect=spy.spy_post):
            svc = LayaDecisionService(endpoint_url="https://mock.laya.local/v1/systemone", api_key="key_async")
            req = _build_synthetic_request(query="Query Async", gap="Gap Async")

            # 1. First request times out at caller level while worker remains paused
            spy.set_probe_id("probe_req1")
            dec1 = svc.decide(req, deadline_remaining_ms=520.0, ledger=LayaBudgetLedger())
            assert dec1.execution_status == "timeout", "Caller must time out"
            spy.record_caller_timeout("probe_req1")

            attempts_1 = spy.get_attempts_for_probe("probe_req1")
            assert len(attempts_1) == 1
            att1 = attempts_1[0]
            assert att1.caller_timed_out is True
            assert att1.ended_at is None
            assert att1.late_completed is False
            assert att1.roundtrip_ms is None

            # 2. Subsequent request runs and completes normally
            spy.pause_event = None
            spy.on_worker_start = None
            spy.set_probe_id("probe_req2")
            dec2 = svc.decide(req, deadline_remaining_ms=2500.0, ledger=LayaBudgetLedger())
            assert dec2.execution_status == "approved"

            attempts_2 = spy.get_attempts_for_probe("probe_req2")
            assert len(attempts_2) == 1
            att2 = attempts_2[0]
            assert att2.attempt_id == 2
            assert att2.caller_timed_out is False
            assert att2.late_completed is False
            assert att2.ended_at is not None
            assert att2.roundtrip_ms is not None

            # 3. Eventual original-worker completion updates att1 without overwriting caller outcome
            worker_release.set()
            time.sleep(0.05)

            assert att1.attempt_id == 1
            assert att1.caller_timed_out is True
            assert att1.late_completed is True, "Original attempt must be marked late_completed by worker"
            assert att1.ended_at is not None, "Original attempt must record ended_at upon completion"
            assert att1.roundtrip_ms is not None, "Original attempt must record roundtrip_ms"
            assert dec1.execution_status == "timeout", "Caller outcome must not be overwritten"


class TestProbeVerdictLogic:
    """Verifies strict readiness evaluation rules."""

    def test_verdict_rejects_fallback_only_delivery(self) -> None:
        """Deterministic fallback delivery must not prove provider readiness."""
        rec = ProbeMetricRecord(
            probe_id="test_fallback",
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
            expansion_executed=True,
            fallback=True,  # Provider failed, fallback delivered text
            execution_status="provider_unavailable",
            pipeline_total_ms=100.0,
            delivery_outcome="DELIVERED",
            completed_within_phase_deadline=True,
        )
        code, label, reasons = evaluate_probe_verdict([rec])
        assert code == 2
        assert label == "FAIL (FUNCTIONAL DEFECT)"
        assert any("fallback delivery does not prove provider readiness" in r for r in reasons)

    def test_verdict_requires_accepted_route_and_lexical_expansion(self) -> None:
        """Lexical queries must have accepted route and executed lexical expansion."""
        # Case A: Route not accepted
        rec_rejected = ProbeMetricRecord(
            probe_id="test_not_accepted",
            target="compiled_pipeline",
            request_sequence="subsequent_request",
            query_type="lexical_expansion",
            client_roundtrip_ms=40.0,
            server_latency_ms=30.0,
            unaccounted_elapsed_ms=10.0,
            dispatched=True,
            valid_response=True,
            proposed_route="lexical_expansion",
            route_accepted=False,
            expansion_executed=True,
            fallback=False,
            execution_status="rejected_by_code",
            pipeline_total_ms=80.0,
            delivery_outcome="DELIVERED",
        )
        code_rej, _, reasons_rej = evaluate_probe_verdict([rec_rejected])
        assert code_rej == 2
        assert any("was not accepted" in r for r in reasons_rej)

        # Case B: Route accepted, but expansion was not executed
        rec_no_exp = ProbeMetricRecord(
            probe_id="test_no_expansion",
            target="compiled_pipeline",
            request_sequence="subsequent_request",
            query_type="lexical_expansion",
            client_roundtrip_ms=40.0,
            server_latency_ms=30.0,
            unaccounted_elapsed_ms=10.0,
            dispatched=True,
            valid_response=True,
            proposed_route="lexical_expansion",
            route_accepted=True,
            expansion_executed=False,  # Expansion failed or skipped
            fallback=False,
            execution_status="approved",
            pipeline_total_ms=80.0,
            delivery_outcome="DELIVERED",
        )
        code_no_exp, _, reasons_no_exp = evaluate_probe_verdict([rec_no_exp])
        assert code_no_exp == 2
        assert any("did not execute lexical expansion" in r for r in reasons_no_exp)

    def test_verdict_requires_accepted_route_none_and_refusal(self) -> None:
        """Route none queries must have accepted none route and clean refusal."""
        # Case A: Route none, but unexpectedly executed expansion
        rec_unexpected_exp = ProbeMetricRecord(
            probe_id="test_none_exp",
            target="compiled_pipeline",
            request_sequence="subsequent_request",
            query_type="none",
            client_roundtrip_ms=40.0,
            server_latency_ms=30.0,
            unaccounted_elapsed_ms=10.0,
            dispatched=True,
            valid_response=True,
            proposed_route="none",
            route_accepted=True,
            expansion_executed=True,  # Bug: executed expansion on route none
            fallback=False,
            execution_status="approved",
            pipeline_total_ms=80.0,
            delivery_outcome="REFUSAL",
        )
        code_exp, _, reasons_exp = evaluate_probe_verdict([rec_unexpected_exp])
        assert code_exp == 2
        assert any("unexpectedly executed expansion for route none" in r for r in reasons_exp)

        # Case B: Route none, but outcome was not REFUSAL
        rec_not_refusal = ProbeMetricRecord(
            probe_id="test_none_not_refused",
            target="compiled_pipeline",
            request_sequence="subsequent_request",
            query_type="none",
            client_roundtrip_ms=40.0,
            server_latency_ms=30.0,
            unaccounted_elapsed_ms=10.0,
            dispatched=True,
            valid_response=True,
            proposed_route="none",
            route_accepted=True,
            expansion_executed=False,
            fallback=False,
            execution_status="approved",
            pipeline_total_ms=80.0,
            delivery_outcome="DELIVERED",  # Bug: failed to refuse
        )
        code_deliv, _, reasons_deliv = evaluate_probe_verdict([rec_not_refusal])
        assert code_deliv == 2
        assert any("was not REFUSAL" in r for r in reasons_deliv)

    def test_verdict_rejects_missing_route_telemetry(self) -> None:
        """Route telemetry must not be empty or missing."""
        rec = ProbeMetricRecord(
            probe_id="test_missing_route",
            target="compiled_pipeline",
            request_sequence="subsequent_request",
            query_type="lexical_expansion",
            client_roundtrip_ms=40.0,
            server_latency_ms=30.0,
            unaccounted_elapsed_ms=10.0,
            dispatched=True,
            valid_response=True,
            proposed_route="",  # Missing telemetry
            route_accepted=True,
            expansion_executed=True,
            fallback=False,
            execution_status="approved",
            pipeline_total_ms=80.0,
            delivery_outcome="DELIVERED",
        )
        code, label, reasons = evaluate_probe_verdict([rec])
        assert code == 2
        assert label == "FAIL (FUNCTIONAL DEFECT)"
        assert any("missing provider route telemetry" in r for r in reasons)

    def test_verdict_rejects_mismatched_route_in_none_scenario_even_if_pipeline_refuses(self) -> None:
        """Observed route lexical_expansion in none scenario must be rejected even if pipeline refused."""
        rec = ProbeMetricRecord(
            probe_id="test_mismatched_none",
            target="compiled_pipeline",
            request_sequence="subsequent_request",
            query_type="none",
            client_roundtrip_ms=40.0,
            server_latency_ms=30.0,
            unaccounted_elapsed_ms=10.0,
            dispatched=True,
            valid_response=True,
            proposed_route="lexical_expansion",  # Mismatched route
            route_accepted=True,
            expansion_executed=False,
            fallback=False,
            execution_status="approved",
            pipeline_total_ms=80.0,
            delivery_outcome="REFUSAL",  # Pipeline refused, but route didn't match scenario
        )
        code, label, reasons = evaluate_probe_verdict([rec])
        assert code == 2
        assert label == "FAIL (FUNCTIONAL DEFECT)"
        assert any("does not match expected route none" in r for r in reasons)

    def test_verdict_rejects_phase_deadline_overrun(self) -> None:
        """Any request exceeding 2500ms phase ceiling is evaluated as latency blocker."""
        rec = ProbeMetricRecord(
            probe_id="test_overrun",
            target="compiled_pipeline",
            request_sequence="first_request",
            query_type="lexical_expansion",
            client_roundtrip_ms=2550.0,
            server_latency_ms=30.0,
            unaccounted_elapsed_ms=2520.0,
            dispatched=True,
            valid_response=True,
            proposed_route="lexical_expansion",
            route_accepted=True,
            expansion_executed=True,
            fallback=False,
            execution_status="completed",
            pipeline_total_ms=2600.0,
            delivery_outcome="DELIVERED",
            completed_within_phase_deadline=False,
        )
        code, label, reasons = evaluate_probe_verdict([rec], max_phase_ms=2500.0)
        assert code == 1
        assert label == "FAIL (LATENCY BLOCKER)"
        assert any("exceeding phase deadline" in r for r in reasons)


class TestProbeTransmissionsAndRedirects:
    """Verifies transmission ceilings and redirect rejection."""

    def test_redirect_rejected_before_untracked_transmissions(self) -> None:
        spy = BoundedNetworkSpy(dry_run=True)
        spy.simulated_is_redirect = True
        spy.simulated_status_code = 302

        with pytest.raises(RedirectRejectedError) as exc_info:
            spy.spy_post("https://mock.laya.local/v1/systemone", json={})
        assert "HTTP redirect 302 rejected" in str(exc_info.value)
        assert spy.attempts[-1].is_redirect is True

        rec = ProbeMetricRecord(
            probe_id="test_redir_record",
            target="compiled_pipeline",
            request_sequence="first_request",
            query_type="lexical_expansion",
            client_roundtrip_ms=30.0,
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
        code, _, reasons = evaluate_probe_verdict([rec])
        assert code == 2
        assert any("redirect" in r.lower() for r in reasons)

    def test_pre_dispatch_ceiling_blocks_sixth_transmission(self) -> None:
        spy = BoundedNetworkSpy(dry_run=True, max_requests=5)
        for _ in range(5):
            spy.spy_post("https://mock.laya.local/v1/systemone", json={})
        assert spy.request_count == 5

        with pytest.raises(RuntimeError) as exc_info:
            spy.spy_post("https://mock.laya.local/v1/systemone", json={})
        assert "reached maximum ceiling" in str(exc_info.value)
        assert spy.request_count == 5, "Request count must not increment past ceiling"


class TestOfflineHarnessSuite:
    """Verifies end-to-end dry-run harness execution and negative controls."""

    def test_negative_controls_pass_explicitly(self) -> None:
        passed = run_offline_negative_controls(verbose=False)
        assert len(passed) == 8

    def test_offline_harness_pass_assertions_end_to_end(self) -> None:
        exit_code = run_latency_probe(endpoint_url="", api_key="", dry_run=True)
        assert exit_code == 0
