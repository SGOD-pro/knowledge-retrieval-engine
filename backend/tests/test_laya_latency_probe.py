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
    FidelityMockSequence,
    HttpAttemptRecord,
    MockOfflineVerifier,
    ProbeMetricRecord,
    RedirectRejectedError,
    _build_synthetic_request,
    _setup_synthetic_repository,
    evaluate_probe_verdict,
    run_latency_probe,
    run_offline_negative_controls,
)
from services.langgraph_pipeline import pipeline
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
        assert len(passed) == 9

    def test_offline_harness_pass_assertions_end_to_end(self) -> None:
        exit_code = run_latency_probe(endpoint_url="", api_key="", dry_run=True)
        assert exit_code == 0


class TestLexicalScenarioIsolation:
    """Verifies that baseline discovery exposes only background evidence and expansion retrieves novel chunk."""

    def test_stage_bounded_repository_isolates_baseline_and_reveals_novel_chunk(self) -> None:
        """Confirms StageBoundedRepository exposes only baseline chunk until explicit expansion transition."""
        repo = _setup_synthetic_repository()
        assert repo.stage == "baseline_discovery"

        # Baseline discovery stage
        chunks_stage1 = repo.get_all_chunks(workspace_id="ws_latency_probe")
        assert len(chunks_stage1) == 1
        assert chunks_stage1[0].id == "chk_probe_baseline"
        assert repo.baseline_candidate_ids == ["chk_probe_baseline"]

        vec_stage1 = repo.search_vector([0.1] * 1024, workspace_id="ws_latency_probe")
        assert len(vec_stage1) == 1
        assert vec_stage1[0][0].id == "chk_probe_baseline"

        # Transition to expansion stage
        repo.transition_to_expansion()
        assert repo.stage == "expansion"
        chunks_stage2 = repo.get_all_chunks(workspace_id="ws_latency_probe")
        assert len(chunks_stage2) == 2
        chunk_ids_stage2 = [c.id for c in chunks_stage2]
        assert "chk_probe_baseline" in chunk_ids_stage2
        assert "chk_probe_turnover" in chunk_ids_stage2

        vec_stage2 = repo.search_vector([0.1] * 1024, workspace_id="ws_latency_probe")
        assert len(vec_stage2) == 2
        vec_ids_stage2 = [v[0].id for v in vec_stage2]
        assert "chk_probe_turnover" in vec_ids_stage2
        assert "chk_probe_baseline" in vec_ids_stage2

        # Reset baseline stage
        repo.reset_baseline()
        assert repo.stage == "baseline_discovery"
        assert repo.baseline_candidate_ids == []
        chunks_reset = repo.get_all_chunks(workspace_id="ws_latency_probe")
        assert len(chunks_reset) == 1
        assert chunks_reset[0].id == "chk_probe_baseline"
        assert repo.baseline_candidate_ids == ["chk_probe_baseline"]

    def test_dispatch_spy_pre_dispatch_assertion_catches_leaked_answer(self) -> None:
        """Confirms spy_post rejects requests that leak answer text or chunk ID in baseline context."""
        spy = BoundedNetworkSpy(dry_run=True)

        # Leaked answer text
        leaked_text_payload = {
            "state": {
                "document": "Query: Turnover query\nGap: Missing turnover\nContext: Confirmed as $450 million."
            },
            "questions": {"route": {"type": "choice"}},
        }
        with pytest.raises(AssertionError) as exc_info_text:
            spy.spy_post("https://mock.laya.local/v1/systemone", json=leaked_text_payload)
        assert "Answer text $450 million found in baseline context" in str(exc_info_text.value)

        # Leaked answer chunk ID
        leaked_chunk_payload = {
            "state": {
                "document": "Query: Turnover query\nGap: Missing turnover\nContext: [chk_probe_turnover] overview"
            },
            "questions": {"route": {"type": "choice"}},
        }
        with pytest.raises(AssertionError) as exc_info_chunk:
            spy.spy_post("https://mock.laya.local/v1/systemone", json=leaked_chunk_payload)
        assert "Answer chunk chk_probe_turnover found in baseline context" in str(exc_info_chunk.value)

    def test_captured_actual_serialized_pipeline_payload_offline(self) -> None:
        """Verifies that BoundedNetworkSpy captures actual compiled pipeline payload offline with restored turnover question."""
        spy = BoundedNetworkSpy(dry_run=True)
        repo = _setup_synthetic_repository()
        spy.stage_boundary = repo
        repo.reset_baseline()

        mock_llm_response = {
            "answer": "The fiscal year 2026 turnover was $450 million.",
            "citations": ["chk_probe_turnover"],
            "usage": {"input_tokens": 120, "output_tokens": 18},
        }
        mock_verifier = MockOfflineVerifier(support_status="ASSERTED")

        def _mock_rerank(q: str, chunks: Any, top_k: int = 6, return_mode: bool = False) -> Any:
            c_list = list(chunks)
            c_list.sort(key=lambda c: 1.0 if "turnover" in c.text.lower() else 0.5, reverse=True)
            return (c_list[:top_k], "mocked") if return_mode else c_list[:top_k]

        with patch("requests.post", side_effect=spy.spy_post), \
             patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response), \
             patch("services.langgraph_pipeline._get_repo", return_value=repo), \
             patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=FidelityMockSequence(mode="lexical")):

            spy.set_probe_id("probe_pipeline_test")
            resp = pipeline.run(
                query="What was the turnover for fiscal year 2026?",
                workspace_id="ws_latency_probe",
                laya_enabled=True,
                laya_endpoint_url="https://mock.laya.local/v1/systemone",
                laya_api_key="mock_key",
                verifier=mock_verifier,
                require_delivery_gate=True,
                caller_deadline_ms=2500.0,
            )

        attempts = spy.get_attempts_for_probe("probe_pipeline_test")
        assert len(attempts) == 1
        att = attempts[0]
        assert att.baseline_candidate_ids == ["chk_probe_baseline"]

        payload = att.request_payload
        assert isinstance(payload, dict) and "state" in payload and "questions" in payload
        assert "route" in payload["questions"]

        doc_str = payload["state"]["document"]
        assert "Query: What was the turnover for fiscal year 2026?" in doc_str
        assert "Gap detected: Initial fidelity check failed coverage: Missing turnover coverage" in doc_str

        # Assert answer chunk and text are absent from baseline serialized context
        context_part = doc_str.split("\nContext: ")[-1] if "\nContext: " in doc_str else ""
        assert "chk_probe_turnover" not in context_part
        assert "$450 million" not in context_part
        assert "450 million" not in context_part

        # Assert answer chunk is absent from baseline candidates captured at baseline stage
        assert "chk_probe_turnover" not in att.baseline_candidate_ids
        assert att.baseline_candidate_ids == ["chk_probe_baseline"]

        # Assert after mocked lexical approval that executor retrieves novel chunk, survives post-expansion rerank, and delivery succeeds
        cands = resp.retrieval_candidates or {}
        assert "chk_probe_turnover" in cands.get("laya_novel_chunks", [])
        assert "chk_probe_turnover" in [str(c) for c in cands.get("reranked", [])]
        assert cands.get("laya_expansion_executed") is True
        assert "$450 million" in resp.answer

    def test_novel_answer_survives_full_post_expansion_evidence_path(self) -> None:
        """Verifies novel answer survives post-expansion rerank/compression, reaches synthesis and verification, and is cited."""
        spy = BoundedNetworkSpy(dry_run=True)
        repo = _setup_synthetic_repository()
        spy.stage_boundary = repo
        repo.reset_baseline()

        synth_inputs: list[tuple[Any, ...]] = []
        def spy_call_llm(*args: Any, **kwargs: Any) -> dict[str, Any]:
            synth_inputs.append((args, kwargs))
            return {
                "answer": "The fiscal year 2026 turnover was $450 million.",
                "citations": ["chk_probe_turnover"],
                "usage": {"input_tokens": 120, "output_tokens": 18},
            }

        mock_verifier = MockOfflineVerifier(support_status="ASSERTED")
        verifier_inputs: list[tuple[Any, ...]] = []
        orig_verify = mock_verifier.verify
        def spy_verify(*args: Any, **kwargs: Any) -> Any:
            verifier_inputs.append((args, kwargs))
            return orig_verify(*args, **kwargs)
        mock_verifier.verify = spy_verify

        def _mock_rerank(q: str, chunks: Any, top_k: int = 6, return_mode: bool = False) -> Any:
            c_list = list(chunks)
            c_list.sort(key=lambda c: 1.0 if "turnover" in c.text.lower() else 0.5, reverse=True)
            return (c_list[:top_k], "mocked") if return_mode else c_list[:top_k]

        with patch("requests.post", side_effect=spy.spy_post), \
             patch("services.langgraph_pipeline.call_llm", side_effect=spy_call_llm), \
             patch("services.langgraph_pipeline._get_repo", return_value=repo), \
             patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=FidelityMockSequence(mode="lexical")):

            spy.set_probe_id("probe_full_evidence_test")
            resp = pipeline.run(
                query="What was the turnover for fiscal year 2026?",
                workspace_id="ws_latency_probe",
                laya_enabled=True,
                laya_endpoint_url="https://mock.laya.local/v1/systemone",
                laya_api_key="mock_key",
                verifier=mock_verifier,
                require_delivery_gate=True,
                caller_deadline_ms=2500.0,
            )

        # 1. Rerank and compression survival
        cands = resp.retrieval_candidates or {}
        assert "chk_probe_turnover" in cands.get("laya_novel_chunks", [])
        assert cands.get("laya_expansion_executed") is True

        # 2. Synthesis inputs
        assert len(synth_inputs) == 1
        s_args, _ = synth_inputs[0]
        assert s_args[0] == "What was the turnover for fiscal year 2026?"
        assert "chk_probe_turnover" in s_args[1]
        assert "$450 million" in s_args[1]

        # 3. Verification inputs
        assert len(verifier_inputs) == 1
        _, v_kwargs = verifier_inputs[0]
        assert "$450 million" in v_kwargs.get("text", "")
        assert "$450 million" in v_kwargs.get("context", "")
        v_cits = [str(c.get("chunk_id") if isinstance(c, dict) else getattr(c, "chunk_id", c)) for c in v_kwargs.get("citations", [])]
        assert "chk_probe_turnover" in v_cits

        # 4. Final cited evidence
        resp_cits = [str(c.get("chunk_id") if isinstance(c, dict) else getattr(c, "chunk_id", c)) for c in resp.citations]
        assert "chk_probe_turnover" in resp_cits
        assert "$450 million" in resp.answer

    def test_live_failure_responses_record_into_verdict_without_crashing(self) -> None:
        """Confirms live none, timeout, or unavailable responses record scenario failures into verdict without crashing."""
        # Case A: Live none response for lexical scenario
        rec_none = ProbeMetricRecord(
            probe_id="live_lexical_returned_none",
            target="compiled_pipeline",
            request_sequence="first_request",
            query_type="lexical_expansion",
            client_roundtrip_ms=120.0,
            server_latency_ms=85.0,
            unaccounted_elapsed_ms=35.0,
            dispatched=True,
            valid_response=True,
            proposed_route="none",  # Live model returned none
            route_accepted=True,
            expansion_executed=False,  # No expansion executed
            fallback=False,
            execution_status="approved",
            pipeline_total_ms=150.0,
            delivery_outcome="REFUSAL",  # Pipeline refused
            completed_within_phase_deadline=True,
        )
        code_none, label_none, reasons_none = evaluate_probe_verdict([rec_none])
        assert code_none == 2
        assert label_none == "FAIL (FUNCTIONAL DEFECT)"
        assert any("does not match expected lexical_expansion" in r for r in reasons_none)
        assert any("did not execute lexical expansion" in r for r in reasons_none)
        assert any("outcome REFUSAL was not DELIVERED" in r for r in reasons_none)

        # Case B: Live timeout response
        rec_timeout = ProbeMetricRecord(
            probe_id="live_lexical_timeout",
            target="compiled_pipeline",
            request_sequence="first_request",
            query_type="lexical_expansion",
            client_roundtrip_ms=2600.0,
            server_latency_ms=None,
            unaccounted_elapsed_ms=None,
            dispatched=True,
            valid_response=False,
            proposed_route="",
            route_accepted=False,
            expansion_executed=False,
            fallback=True,
            execution_status="timeout",
            pipeline_total_ms=2600.0,
            delivery_outcome="TIMEOUT",
            completed_within_phase_deadline=False,
            error="caller_timeout",
        )
        code_to, label_to, reasons_to = evaluate_probe_verdict([rec_timeout])
        assert code_to == 1
        assert label_to == "FAIL (LATENCY BLOCKER)"
        assert any("exceeding phase deadline" in r for r in reasons_to)

        # Case C: Live provider unavailable response
        rec_unavail = ProbeMetricRecord(
            probe_id="live_lexical_unavail",
            target="compiled_pipeline",
            request_sequence="first_request",
            query_type="lexical_expansion",
            client_roundtrip_ms=50.0,
            server_latency_ms=None,
            unaccounted_elapsed_ms=None,
            dispatched=True,
            valid_response=False,
            proposed_route="",
            route_accepted=False,
            expansion_executed=False,
            fallback=True,
            execution_status="provider_unavailable",
            pipeline_total_ms=90.0,
            delivery_outcome="REFUSAL",
            completed_within_phase_deadline=True,
            error="Connection refused",
        )
        code_un, label_un, reasons_un = evaluate_probe_verdict([rec_unavail])
        assert code_un == 2
        assert label_un == "FAIL (FUNCTIONAL DEFECT)"
        assert any("did not receive a valid provider response" in r for r in reasons_un)
