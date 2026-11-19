"""Comprehensive offline regression suite for 0008 Slice 5.1:
Bounded LAYA Escalation, Lexical Vertical Path, Safety Delivery Gate, and Concurrency Admission.

Verifies:
- Frozen typed contracts, immutability, and discriminator agreement (AC-2, AC-5).
- Mandatory routing confidence without certifying evidence sufficiency (AC-2).
- Section 35 deterministic missing requirement policy and unavailable route fallback (AC-3).
- Atomic request local budget pre reservation, deadline bounds, and settlement (AC-4).
- Authoritative token preflight and deterministic evidence summary truncation (AC-4).
- Strict provider usage validation and invalid usage fail closed handling (AC-4).
- Concurrency queue saturation, immediate load shedding, and caller timeout capacity retention (AC-4).
- Late completion discarding with spend reconciliation (AC-4).
- 429 throttling retries charged as attempts within the same bounded operation (AC-4).
- Bounded lexical expansion via BM25 preserving baseline chunks and version isolation (AC-1, AC-5).
- Dynamic workspace and source tombstones before retrieval and before delivery (AC-5).
- Empty context entry path and stale refusal clearing (AC-1, AC-7).
- No progress suppression resulting in structured refusal (AC-6, AC-7).
- Single synthesis call invariant under ADR-004 (AC-4, AC-6).
- Final delivery gate blocking free form prose when verifier is unavailable (AC-6).
- Offline execution with zero paid model calls or benchmarks (AC-8).
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch
import pytest
from pydantic import ValidationError

from config import settings
from schemas.models import Chunk, Document
from services.retrieval.bm25_retriever import invalidate_chunk_cache
from src.schemas.contracts.laya import (
    BaselineEvidenceSummary,
    DenseExpansionParams,
    GraphExpansionParams,
    LayaEscalationDecision,
    LayaEscalationRequest,
    LexicalExpansionParams,
    MissingRequirementDescription,
    NoneExpansionParams,
    StructuralExpansionParams,
    StructuredExpansionParams,
)
from src.schemas.contracts.verification import VerificationRecord
from src.schemas.contracts.knowledge_graph import PinnedDocumentVersion, PinnedSnapshotContext
from src.services.retrieval.laya_policy import (
    AVAILABLE_ROUTES_SLICE_5_1,
    extract_fallback_query_terms,
    get_deterministic_fallback_decision,
)
from src.services.retrieval.laya_gate import (
    InvalidUsageError,
    LayaBudgetLedger,
    calculate_phase_deadline,
    estimate_tokens_defensible_bound,
    preflight_token_count,
    truncate_evidence_summary_for_preflight,
    validate_provider_usage,
)
from src.services.retrieval.laya_concurrency import (
    LayaProviderConcurrencyController,
)
from src.services.retrieval.laya_service import (
    LayaDecisionService,
)
from src.services.retrieval.laya_executor import (
    LayaExpansionExecutor,
)
from services.langgraph_pipeline import pipeline, run_llm, run_laya_escalation, delivery_gate


# ---------------------------------------------------------------------------
# Test Helpers and Doubles
# ---------------------------------------------------------------------------

def _make_sample_request(
    query: str = "What are the latest revenue numbers?",
    ws_id: str = "ws_test_laya",
    dim: str = "exact_term",
    gap: str = "Missing quarterly revenue identifier",
    summaries: tuple[BaselineEvidenceSummary, ...] = (),
) -> LayaEscalationRequest:
    return LayaEscalationRequest(
        query_id="q_sample_123",
        workspace_id=ws_id,
        query=query,
        missing_requirement=MissingRequirementDescription(
            requirement_id="req_1",
            missing_dimension=dim,  # type: ignore[arg-type]
            description=f"Gap for {dim}",
            failed_stage="run_fidelity_coverage",
            detected_gap=gap,
        ),
        available_routes=("lexical_expansion", "none"),
        retrieved_summary=summaries,
        caller_deadline_ms=2500.0,
    )


class MockBedrockConverseClient:
    """Mock Bedrock client returning structured Converse responses."""

    def __init__(self, response_payload: dict[str, Any] | None = None, usage: dict[str, Any] | None = None) -> None:
        self.response_payload = response_payload or {
            "decision_id": "dec_test_001",
            "route": "lexical_expansion",
            "parameters": {
                "route_type": "lexical_expansion",
                "query_terms": ["revenue", "quarterly"],
                "document_ids": ["doc_financials"],
            },
            "confidence": 0.85,
            "rationale": "Lexical keywords match financial reports",
            "execution_status": "proposed",
        }
        self.usage = usage if usage is not None else {"inputTokens": 180, "outputTokens": 45}
        self.call_count = 0
        self.count_tokens_call_count = 0

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        import json
        text_content = json.dumps(self.response_payload)
        return {
            "output": {"message": {"content": [{"text": text_content}]}},
            "usage": self.usage,
        }

    def count_tokens(self, **kwargs: Any) -> dict[str, Any]:
        self.count_tokens_call_count += 1
        return {"inputTokens": 180}


class MockBoundaryRepo:
    """Mock boundary repository for dynamic tombstone testing."""

    def __init__(self, tombstoned_resources: set[tuple[str, str, str]] | None = None) -> None:
        self.tombstoned = tombstoned_resources or set()

    def has_tombstone(self, workspace_id: str, resource_type: str, resource_id: str) -> bool:
        return (workspace_id, resource_type, resource_id) in self.tombstoned


class MockVerifier:
    """Mock semantic verifier for delivery gate testing."""

    def __init__(self, support_status: str = "ASSERTED", ready: bool = True) -> None:
        self.support_status = support_status
        self.ready = ready
        self.verify_call_count = 0

    def is_ready(self) -> bool:
        return self.ready

    def verify(self, text: str, context: str, citations: list[Any]) -> VerificationRecord:
        self.verify_call_count += 1
        return VerificationRecord(
            verification_id="vr_test_001",
            workspace_id="ws_test_laya",
            query_id="q_test_123",
            snapshot_id="snap_1",
            requirement_id="req_1",
            evidence_id="doc_1:c1",
            verifier_version="v1.0",
            verifier_type="generative",
            attempt_number=1,
            execution_time_ms=25,
            support_status=self.support_status,  # type: ignore[arg-type]
            confidence_score=0.92,
            verification_rationale="Grounded in retrieved financial table",
            verified_at="2026-11-19T00:00:00Z",
        )


# ===========================================================================
# 1. Contract and Discriminator Agreement Tests (AC-2, AC-5)
# ===========================================================================

def test_laya_contracts_strict_discriminator_and_bounds():
    """Verify frozen contracts enforce discriminator agreement and strict length bounds."""
    # Valid lexical expansion decision
    valid_lexical = LayaEscalationDecision(
        decision_id="dec_1",
        route="lexical_expansion",
        parameters=LexicalExpansionParams(
            route_type="lexical_expansion",
            query_terms=("revenue", "q3"),
            document_ids=("doc_1",),
        ),
        confidence=0.8,
        rationale="Keyword expansion",
        execution_status="proposed",
    )
    assert valid_lexical.route == "lexical_expansion"
    assert valid_lexical.confidence == 0.8

    # Discriminator mismatch: route is lexical_expansion, but parameters is DenseExpansionParams
    with pytest.raises(ValidationError) as exc_info:
        LayaEscalationDecision(
            decision_id="dec_2",
            route="lexical_expansion",
            parameters=DenseExpansionParams(
                route_type="dense_expansion",
                concept_query="quarterly revenue growth",
            ),
            confidence=0.8,
        )
    assert "Route discriminator mismatch" in str(exc_info.value)

    # Missing confidence is rejected (mandatory, no default)
    with pytest.raises(ValidationError):
        LayaEscalationDecision.model_validate({
            "decision_id": "dec_3",
            "route": "none",
            "parameters": {"route_type": "none"},
        })

    # Out of bounds confidence rejected
    with pytest.raises(ValidationError):
        LayaEscalationDecision(
            decision_id="dec_4",
            route="none",
            parameters=NoneExpansionParams(route_type="none"),
            confidence=1.5,
        )

    # Boolean confidence rejected
    with pytest.raises(ValidationError):
        LayaEscalationDecision(
            decision_id="dec_5",
            route="none",
            parameters=NoneExpansionParams(route_type="none"),
            confidence=True,  # type: ignore[arg-type]
        )


def test_baseline_evidence_summary_normalized_bbox():
    """Verify BaselineEvidenceSummary enforces normalized coordinates and opaque versions."""
    # Valid summary
    valid_summary = BaselineEvidenceSummary(
        chunk_id="chk_1",
        document_id="doc_financials_2026",
        document_version="ver_alpha_99",  # opaque string version
        workspace_id="ws_fin_1",
        snippet="Q3 total revenue was $1.5M",
        score=88.5,
        source_kind="text",
        page_number=3,
        bbox=(0.1, 0.1, 0.9, 0.9),
    )
    assert valid_summary.document_version == "ver_alpha_99"

    # Inverted bounding box coordinates rejected
    with pytest.raises(ValidationError) as exc_info:
        BaselineEvidenceSummary(
            chunk_id="chk_2",
            document_id="doc_1",
            document_version="ver_1",
            workspace_id="ws_fin_1",
            snippet="Snippet",
            score=50.0,
            source_kind="text",
            bbox=(0.8, 0.8, 0.2, 0.2),  # x0 >= x1
        )
    assert "bbox coordinates must satisfy x0 < x1" in str(exc_info.value)

    # Coordinates exceeding 1.0 rejected
    with pytest.raises(ValidationError):
        BaselineEvidenceSummary(
            chunk_id="chk_3",
            document_id="doc_1",
            document_version="ver_1",
            workspace_id="ws_fin_1",
            snippet="Snippet",
            score=50.0,
            source_kind="text",
            bbox=(0.0, 0.0, 1.5, 0.8),
        )


def test_payload_length_caps():
    """Verify request and parameter length bounds reject oversized payloads."""
    # Query terms max 5 items
    with pytest.raises(ValidationError):
        LexicalExpansionParams(
            query_terms=("a", "b", "c", "d", "e", "f"),  # 6 items
        )

    # Query term item length > 64 chars
    with pytest.raises(ValidationError):
        LexicalExpansionParams(
            query_terms=("a" * 65,),
        )

    # Page number 0 rejected (must be >= 1)
    with pytest.raises(ValidationError):
        StructuralExpansionParams(
            document_id="doc_1",
            target_pages=(0,),
        )


# ===========================================================================
# 2. Section 35 Deterministic Fallback Policy Tests (AC-3)
# ===========================================================================

def test_deterministic_fallback_policy_section_35():
    """Verify Section 35 policy falls back to lexical expansion in Slice 5.1."""
    req_term = _make_sample_request(dim="exact_term", gap="missing term EBITDA")
    fb_term = get_deterministic_fallback_decision(req_term)
    assert fb_term.route == "lexical_expansion"
    assert "ebitda" in fb_term.parameters.query_terms  # type: ignore[attr-defined]
    assert fb_term.execution_status == "approved"

    # In Slice 5.1, dense and structural dimensions also fall back to lexical
    req_concept = _make_sample_request(dim="semantic_concept", gap="operating leverage definition")
    fb_concept = get_deterministic_fallback_decision(req_concept)
    assert fb_concept.route == "lexical_expansion"

    req_location = _make_sample_request(dim="location", gap="appendix B table 4")
    fb_location = get_deterministic_fallback_decision(req_location)
    assert fb_location.route == "lexical_expansion"


# ===========================================================================
# 3. Budget Pre-Reservation and Phase Deadlines (AC-4)
# ===========================================================================

def test_budget_ledger_pre_reservation_and_phase_deadline():
    """Verify atomic pre reservation and deadline derivation under Slice 5.1 caps."""
    ledger = LayaBudgetLedger()

    # 1. Successful pre reservation with adequate deadline
    ok, reason = ledger.pre_reserve(deadline_remaining_ms=10000.0)
    assert ok is True
    assert ledger.laya_calls == 1
    assert ledger.expansion_steps == 1

    # 2. Subsequent reservation fails closed (MAX_QUERY_LAYA_CALLS = 1)
    ok_second, reason_second = ledger.pre_reserve(deadline_remaining_ms=8000.0)
    assert ok_second is False
    assert "exhausted" in reason_second

    # 3. Deadline below 500ms fails closed immediately
    fresh_ledger = LayaBudgetLedger()
    ok_deadline, reason_deadline = fresh_ledger.pre_reserve(deadline_remaining_ms=450.0)
    assert ok_deadline is False
    assert "below minimum" in reason_deadline

    # 4. Phase deadline caps at 2500ms
    assert calculate_phase_deadline(10000.0) == 2500.0
    assert calculate_phase_deadline(1500.0) == 1500.0


# ===========================================================================
# 4. Token Preflight and Evidence Truncation (AC-4)
# ===========================================================================

def test_token_preflight_and_evidence_truncation():
    """Verify preflight token estimation and deterministic truncation of oversized summaries."""
    # Create request with long snippets
    long_summaries = tuple(
        BaselineEvidenceSummary(
            chunk_id=f"chk_{i}",
            document_id=f"doc_{i}",
            document_version="v1",
            workspace_id="ws_test_laya",
            snippet="Comprehensive financial statement analysis detailing operations " * 4,
            score=80.0,
            source_kind="text",
        )
        for i in range(8)
    )
    req = _make_sample_request(summaries=long_summaries)

    # Truncation reduces snippet length
    truncated = truncate_evidence_summary_for_preflight(req, target_max_tokens=600, system_prompt="System instructions")
    for s in truncated.retrieved_summary:
        assert len(s.snippet) <= 80

    # Authoritative counting with mock client
    mock_client = MockBedrockConverseClient()
    count = preflight_token_count(
        client=mock_client,
        model_id="anthropic.claude-3-haiku-20240307-v1:0",
        system_prompt="sys",
        user_content="user message",
    )
    assert count == 180
    assert mock_client.count_tokens_call_count == 1


# ===========================================================================
# 5. Strict Usage Validation (AC-4)
# ===========================================================================

def test_strict_provider_usage_validation():
    """Verify provider usage dictionary validation and rejection of malformed usage."""
    # Valid usage
    in_tok, out_tok = validate_provider_usage({"inputTokens": 200, "outputTokens": 50}, estimated_prompt_tokens=250)
    assert in_tok == 200
    assert out_tok == 50

    # Missing usage dict
    with pytest.raises(InvalidUsageError):
        validate_provider_usage(None, 250)

    # Non integer token counts
    with pytest.raises(InvalidUsageError):
        validate_provider_usage({"inputTokens": "200", "outputTokens": 50}, 250)

    # Boolean token count rejected (bool is subclass of int)
    with pytest.raises(InvalidUsageError):
        validate_provider_usage({"inputTokens": True, "outputTokens": 50}, 250)

    # Negative tokens rejected
    with pytest.raises(InvalidUsageError):
        validate_provider_usage({"inputTokens": -10, "outputTokens": 50}, 250)

    # Output tokens exceeding cap of 250 rejected
    with pytest.raises(InvalidUsageError):
        validate_provider_usage({"inputTokens": 200, "outputTokens": 251}, 250)

    # Input tokens exceeding estimated prompt tokens by excessive variance
    with pytest.raises(InvalidUsageError):
        validate_provider_usage({"inputTokens": 500, "outputTokens": 50}, estimated_prompt_tokens=200)


# ===========================================================================
# 6. Concurrency Queue Saturation and Load Shedding (AC-4)
# ===========================================================================

def test_concurrency_queue_saturation_load_shedding():
    """Verify 4 workers, 8 wait queue callers, and immediate load shedding on 9th caller."""
    controller = LayaProviderConcurrencyController(max_concurrency=2, max_queue=3)

    gate_event = threading.Event()

    def _slow_task():
        gate_event.wait(timeout=2.0)
        return {"status": "ok"}

    threads = []
    statuses = []

    # Fill 2 active workers + 3 queued callers = 5 total callers
    for i in range(5):
        def _runner():
            _, st = controller.run_provider_call(_slow_task, timeout_s=1.0)
            statuses.append(st)
        t = threading.Thread(target=_runner)
        threads.append(t)
        t.start()

    time.sleep(0.05)  # brief wait for queue to saturate

    # 6th caller attempts entry when wait queue is at max 3 -> immediate load shedding
    res, status_rejected = controller.run_provider_call(_slow_task, timeout_s=1.0)
    assert status_rejected == "admission_rejected"
    assert res is None

    # Unblock workers and wait for threads
    gate_event.set()
    for t in threads:
        t.join(timeout=2.0)


# ===========================================================================
# 7. Caller Timeout with Capacity Retention and Late Settlement (AC-4)
# ===========================================================================

def test_caller_timeout_capacity_retention_and_late_settlement():
    """Verify worker slot is retained until provider finishes and late spend is settled."""
    controller = LayaProviderConcurrencyController(max_concurrency=1, max_queue=2)
    ledger = LayaBudgetLedger()

    task_completed = threading.Event()
    reconciled = threading.Event()

    def _slow_provider_call():
        time.sleep(0.15)
        task_completed.set()
        return {
            "output": {"message": {"content": [{"text": "late result"}]}},
            "usage": {"inputTokens": 100, "outputTokens": 20},
        }

    def _spend_cb(usage, lat_ms, status):
        ledger.settle_spend(usage.get("inputTokens", 0), usage.get("outputTokens", 0), lat_ms, is_late=True, status=status)
        reconciled.set()

    # Caller times out at 0.05s while call takes 0.15s
    res, status = controller.run_provider_call(
        _slow_provider_call,
        timeout_s=0.05,
        on_spend_reconcile=_spend_cb,
    )
    assert status == "caller_timeout"
    assert res is None

    # Verify worker slot remains retained during the extra 0.10s
    assert controller.active_count == 1

    # Wait for background task to finish and reconcile spend
    finished = task_completed.wait(timeout=1.0)
    assert finished is True
    reconciled.wait(timeout=1.0)

    # Spend is reconciled into ledger
    assert ledger.late_settled_calls == 1
    assert ledger.total_input_tokens == 100
    assert ledger.total_output_tokens == 20

    # Capacity released afterwards
    time.sleep(0.02)
    assert controller.active_count == 0


# ===========================================================================
# 8. 429 Throttling Retries Charged as Attempts (AC-4)
# ===========================================================================

def test_429_throttling_retries_charged_as_attempts():
    """Verify 429 rate limit is charged as an attempt and retries within deadline."""
    ledger = LayaBudgetLedger()
    mock_client = MagicMock()

    # First call raises 429 ClientError, second call succeeds
    from botocore.exceptions import ClientError
    err_429 = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded 429"}},
        "Converse",
    )
    import json
    valid_resp = {
        "output": {
            "message": {
                "content": [{
                    "text": json.dumps({
                        "decision_id": "dec_retry_1",
                        "route": "lexical_expansion",
                        "parameters": {
                            "route_type": "lexical_expansion",
                            "query_terms": ["revenue"],
                            "document_ids": [],
                        },
                        "confidence": 0.8,
                        "rationale": "Retried successfully",
                    })
                }]
            }
        },
        "usage": {"inputTokens": 150, "outputTokens": 30},
    }
    mock_client.converse.side_effect = [err_429, valid_resp]
    mock_client.count_tokens.return_value = {"inputTokens": 150}

    service = LayaDecisionService(client=mock_client)
    req = _make_sample_request()

    decision = service.decide(req, deadline_remaining_ms=2500.0, ledger=ledger)
    assert decision.route == "lexical_expansion"
    assert decision.execution_status == "approved"
    # Attempt counter reflects retry
    assert ledger.tool_attempts == 2


# ===========================================================================
# 9. Bounded Lexical Expansion Success (AC-1, AC-5, AC-7)
# ===========================================================================

def test_happy_path_lexical_expansion_success():
    """Verify vocabulary mismatch triggers lexical expansion and delivers verified answer."""
    ws_id = "ws_test_lexical"
    from db.database import CloudRepository
    repo = CloudRepository()

    # Document contains 'turnover' (synonym of 'revenue')
    doc_chunk = Chunk(
        id="chunk_turnover_1",
        document_id="doc_annual_report",
        source_format="pdf",
        text="The annual turnover for fiscal year 2026 was $450 million.",
        element_type="narrative_block",
        workspace_id=ws_id,
        metadata={"document_version": "v1"},
        embedding_fast=[0.1] * 384,
        embedding_full=[0.1] * 1024,
    )
    doc = Document(
        id="doc_annual_report",
        filename="doc_annual_report.pdf",
        source_format="pdf",
        chunks=(doc_chunk,),
        workspace_id=ws_id,
    )
    repo.save(doc)
    repo.add_document_to_workspace(ws_id, doc)
    invalidate_chunk_cache(ws_id)

    # Initial query uses 'revenue' (fails initial keyword / fidelity)
    query = "What was the revenue for fiscal year 2026?"

    # Mock LAYA decision returning lexical expansion on 'turnover'
    mock_client = MockBedrockConverseClient(
        response_payload={
            "decision_id": "dec_synonym_1",
            "route": "lexical_expansion",
            "parameters": {
                "route_type": "lexical_expansion",
                "query_terms": ["turnover", "fiscal", "2026"],
                "document_ids": ["doc_annual_report"],
            },
            "confidence": 0.9,
            "rationale": "Synonym match turnover for revenue",
        }
    )

    # Mock semantic verifier approving delivery
    mock_verifier = MockVerifier(support_status="ASSERTED")

    mock_llm_response = {
        "answer": "The annual turnover for fiscal year 2026 was $450 million.",
        "citations": ["chunk_turnover_1"],
        "usage": {"input_tokens": 120, "output_tokens": 25},
    }

    # Patch call_llm so run_llm produces the answer
    with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response):
        with patch("services.langgraph_pipeline._get_repo", return_value=repo):
            # Patch initial fidelity to fail with CoverageError on initial baseline pass
            original_check_fidelity = None
            from services.retrieval import fidelity_check
            original_check_fidelity = fidelity_check.check_fidelity

            fidelity_calls = 0

            def _mock_check_fidelity(q, contexts, **kw):
                nonlocal fidelity_calls
                fidelity_calls += 1
                if fidelity_calls == 1:
                    # Initial baseline pass fails coverage
                    from services.retrieval.fidelity_check import CoverageError
                    raise CoverageError("No coverage for 'turnover'")
                return 1.0

            with patch("services.langgraph_pipeline.check_fidelity", side_effect=_mock_check_fidelity):
                response = pipeline.run(
                    query=query,
                    workspace_id=ws_id,
                    laya_enabled=True,
                    laya_client=mock_client,
                    verifier=mock_verifier,
                    require_delivery_gate=True,
                )

                assert response.laya_calls == 1
                assert response.expansion_steps == 1
                assert response.synthesis_calls == 1
                assert "$450 million" in response.answer
                assert len(response.citations) > 0
                assert mock_verifier.verify_call_count == 1


# ===========================================================================
# 10. Empty Context Entry Path (AC-1, AC-7)
# ===========================================================================

def test_empty_context_triggers_escalation():
    """Verify empty initial context triggers LAYA escalation rather than immediate termination."""
    ws_id = "ws_test_empty"
    from db.database import CloudRepository
    repo = CloudRepository()

    doc_chunk = Chunk(
        id="chunk_empty_recov_1",
        document_id="doc_ops",
        source_format="pdf",
        text="Target acquired in sector 7.",
        element_type="narrative_block",
        workspace_id=ws_id,
        metadata={"document_version": "v1"},
        embedding_fast=[0.1] * 384,
        embedding_full=[0.1] * 1024,
    )
    doc = Document(
        id="doc_ops",
        filename="doc_ops.pdf",
        source_format="pdf",
        chunks=(doc_chunk,),
        workspace_id=ws_id,
    )
    repo.save(doc)
    repo.add_document_to_workspace(ws_id, doc)
    invalidate_chunk_cache(ws_id)

    mock_client = MockBedrockConverseClient(
        response_payload={
            "decision_id": "dec_empty_recov",
            "route": "lexical_expansion",
            "parameters": {
                "route_type": "lexical_expansion",
                "query_terms": ["sector", "7"],
                "document_ids": ["doc_ops"],
            },
            "confidence": 0.85,
            "rationale": "Retrieve sector 7 details",
        }
    )

    mock_llm_resp = {
        "answer": "Target was acquired in sector 7.",
        "citations": ["chunk_empty_recov_1"],
        "usage": {"input_tokens": 80, "output_tokens": 15},
    }

    mock_verifier = MockVerifier(support_status="ASSERTED")

    with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_resp):
        with patch("services.langgraph_pipeline._get_repo", return_value=repo):
            # Simulate initial compressor returning empty context, secondary returning text
            comp_calls = 0

            def _mock_compress(q, chunks):
                nonlocal comp_calls
                comp_calls += 1
                if comp_calls == 1:
                    return ""
                return f"[{doc_chunk.id}] Target acquired in sector 7."

            with patch("services.langgraph_pipeline.compress_chunks", side_effect=_mock_compress):
                with patch("services.langgraph_pipeline.check_fidelity", return_value=1.0):
                    response = pipeline.run(
                        query="Where was target acquired?",
                        workspace_id=ws_id,
                        laya_enabled=True,
                        laya_client=mock_client,
                        verifier=mock_verifier,
                    )

                    assert response.laya_calls == 1
                    assert response.expansion_steps == 1
                    assert "sector 7" in response.answer


# ===========================================================================
# 11. Code Rejection of Unavailable Routes in Slice 5.1 (AC-3)
# ===========================================================================

def test_code_rejection_of_unavailable_routes_in_slice5_1():
    """Verify proposal of graph_expansion or dense_expansion is rejected and falls back."""
    mock_client = MockBedrockConverseClient(
        response_payload={
            "decision_id": "dec_unavail_graph",
            "route": "graph_expansion",  # unavailable in Slice 5.1
            "parameters": {
                "route_type": "graph_expansion",
                "seed_entity_id": "ent_123",
                "relation_types": ["subsidiary_of"],
                "max_hops": 1,
            },
            "confidence": 0.9,
            "rationale": "Graph traversal proposal",
        }
    )

    service = LayaDecisionService(client=mock_client)
    ledger = LayaBudgetLedger()
    req = _make_sample_request(dim="relationship", gap="unlinked subsidiary")

    decision = service.decide(req, deadline_remaining_ms=2500.0, ledger=ledger)

    # Rejection of unavailable route transitions to fallback lexical_expansion
    assert decision.route == "lexical_expansion"
    assert decision.execution_status == "approved"
    assert "unavailable in Slice 5.1" in decision.rationale


# ===========================================================================
# 12. Version Isolation and Opaque String Binding (AC-5)
# ===========================================================================

def test_version_isolation_and_opaque_string_binding():
    """Verify expansion lookup enforces exact opaque string version matching."""
    ws_id = "ws_test_version"
    chunk_v1 = Chunk(
        id="c_v1",
        document_id="doc_formula",
        source_format="pdf",
        text="Alpha formula 2025 version.",
        element_type="narrative_block",
        workspace_id=ws_id,
        metadata={"document_version": "ver_2025_alpha"},
    )
    chunk_v2 = Chunk(
        id="c_v2",
        document_id="doc_formula",
        source_format="pdf",
        text="Alpha formula 2026 updated version.",
        element_type="narrative_block",
        workspace_id=ws_id,
        metadata={"document_version": "ver_2026_beta"},
    )

    mock_repo = MagicMock()
    mock_repo.get_all_chunks.return_value = [chunk_v1, chunk_v2]

    # Pin context strictly to ver_2026_beta
    snap_context = PinnedSnapshotContext(
        workspace_id=ws_id,
        captured_at_utc="2026-11-19T00:00:00Z",
        active_documents=(
            PinnedDocumentVersion(
                document_id="doc_formula",
                document_version="ver_2026_beta",
                publication_id="pub_1",
                is_tombstoned=False,
            ),
        ),
    )

    executor = LayaExpansionExecutor()
    decision = LayaEscalationDecision(
        decision_id="dec_ver_test",
        route="lexical_expansion",
        parameters=LexicalExpansionParams(
            route_type="lexical_expansion",
            query_terms=("alpha", "formula"),
            document_ids=("doc_formula",),
        ),
        confidence=0.9,
    )

    novel, no_prog, st = executor.execute(
        decision=decision,
        workspace_id=ws_id,
        baseline_chunks=[],
        chunk_repo=mock_repo,
        snapshot_context=snap_context,
    )

    # Only chunk_v2 is retrieved, chunk_v1 is excluded due to version mismatch
    assert len(novel) == 1
    assert novel[0].id == "c_v2"
    assert (novel[0].metadata or {}).get("document_version") == "ver_2026_beta"


# ===========================================================================
# 13. Dynamic Tombstone Halts Retrieval Fail-Closed (AC-5)
# ===========================================================================

def test_dynamic_tombstone_halts_retrieval_fail_closed():
    """Verify active workspace or document tombstone halts expansion immediately."""
    ws_id = "ws_test_tombstone"
    boundary_repo = MockBoundaryRepo(
        tombstoned_resources={(ws_id, "workspace", ws_id)}
    )

    executor = LayaExpansionExecutor()
    decision = LayaEscalationDecision(
        decision_id="dec_tomb_test",
        route="lexical_expansion",
        parameters=LexicalExpansionParams(
            route_type="lexical_expansion",
            query_terms=("secret",),
        ),
        confidence=0.8,
    )

    novel, no_prog, status = executor.execute(
        decision=decision,
        workspace_id=ws_id,
        baseline_chunks=[],
        boundary_repo=boundary_repo,
    )

    assert status == "tombstoned"
    assert len(novel) == 0
    assert no_prog is True


# ===========================================================================
# 14. Dynamic Tombstone Blocks Delivery Gate (AC-5)
# ===========================================================================

def test_dynamic_tombstone_blocks_delivery_gate():
    """Verify document tombstone recorded before delivery suppresses citations."""
    ws_id = "ws_test_deliv_tomb"
    boundary_repo = MockBoundaryRepo(
        tombstoned_resources={(ws_id, "source", "doc_sensitive")}
    )

    state = {
        "workspace_id": ws_id,
        "boundary_repo": boundary_repo,
        "final_answer": "Sensitively retrieved answer text.",
        "citations": [{"document_id": "doc_sensitive", "chunk_id": "c1"}],
        "top_chunks": [],
        "retrieval_incomplete": False,
    }

    result = delivery_gate(state)
    assert result["final_answer"] == "NOT_FOUND"
    assert result["citations"] == []
    assert result["retrieval_incomplete"] is True


# ===========================================================================
# 15. Final Delivery Gate Blocks Unverified Free-Form Prose (AC-6)
# ===========================================================================

def test_delivery_gate_blocks_unverified_free_form_delivery():
    """Verify free form synthesized prose is blocked when verifier is unconfigured."""
    state = {
        "query": "What is the product release date?",
        "workspace_id": "ws_test_deliv_gate",
        "final_answer": "The product will release on July 15 2027.",  # unverified free form prose
        "citations": [{"document_id": "doc_1", "chunk_id": "c1"}],
        "top_chunks": [],
        "escalation_executed": True,
        "require_delivery_gate": True,
        "verifier": None,  # no compatible verifier in environment
    }

    result = delivery_gate(state)

    # Free form prose blocked in favor of refusal
    assert result["final_answer"] == "NOT_FOUND"
    assert result["citations"] == []
    assert result["retrieval_incomplete"] is True


def test_delivery_gate_rejects_contradicted_verification():
    """Verify delivery gate blocks output when verifier returns CONTRADICTED."""
    mock_verifier = MockVerifier(support_status="CONTRADICTED")

    state = {
        "query": "What is the product release date?",
        "workspace_id": "ws_test_deliv_gate",
        "final_answer": "Fabricated unsupported release date.",
        "citations": [{"document_id": "doc_1", "chunk_id": "c1"}],
        "top_chunks": [],
        "escalation_executed": True,
        "require_delivery_gate": True,
        "verifier": mock_verifier,
    }

    result = delivery_gate(state)
    assert result["final_answer"] == "NOT_FOUND"
    assert result["citations"] == []
    assert result["retrieval_incomplete"] is True


# ===========================================================================
# 16. Single Synthesis Call Invariant (AC-4, AC-6)
# ===========================================================================

def test_single_synthesis_call_invariant():
    """Verify run_llm is capped at exactly one call per query."""
    state = {
        "query": "What is the capital of Mars?",
        "compressed_text": "[c1] Mars has no capital.",
        "top_chunks": [
            Chunk(
                id="c1",
                document_id="doc_mars",
                source_format="pdf",
                text="Mars has no capital.",
                element_type="narrative_block",
            )
        ],
        "synthesis_calls": 1,  # already called once
        "stage_timings": {},
    }

    # Second synthesis call returns empty dict and does not invoke LLM
    with patch("services.langgraph_pipeline.call_llm") as mock_call:
        res = run_llm(state)
        assert res == {}
        mock_call.assert_not_called()


# ===========================================================================
# 17. No Progress Suppression (AC-6, AC-7)
# ===========================================================================

def test_stale_refusal_clearing_explicit():
    """Verify that stale refusal fields are cleared before secondary fidelity check."""
    ws_id = "ws_test_stale_refusal"
    novel_chunk = Chunk(
        id="c_novel_1",
        document_id="doc_novel",
        source_format="pdf",
        text="Novel grounding evidence for secondary check.",
        element_type="narrative_block",
        workspace_id=ws_id,
        metadata={"document_version": "v1"},
    )

    mock_executor = MagicMock()
    mock_executor.execute.return_value = ([novel_chunk], False, "completed")

    mock_client = MockBedrockConverseClient(
        response_payload={
            "decision_id": "dec_stale_test",
            "route": "lexical_expansion",
            "parameters": {
                "route_type": "lexical_expansion",
                "query_terms": ["novel", "evidence"],
            },
            "confidence": 0.88,
            "rationale": "Lexical expansion for novel terms",
        }
    )

    state = {
        "query": "Where is the novel evidence?",
        "workspace_id": ws_id,
        "error": "Initial coverage failure",
        "final_answer": "NOT_FOUND",
        "citations": [{"old": "citation"}],
        "candidate_chunks": [],
        "top_chunks": [],
        "compressed_text": "",
        "laya_decision_service": LayaDecisionService(client=mock_client),
        "laya_expansion_executor": mock_executor,
        "stage_timings": {},
        "verifier": MockVerifier(support_status="ASSERTED"),
        "synthesis_calls": 0,
        "_pipeline_start_time": time.perf_counter(),
    }

    with patch("services.langgraph_pipeline.run_reranker", return_value={"top_chunks": [novel_chunk]}):
        with patch("services.langgraph_pipeline.run_compressor", return_value={"compressed_text": "[c_novel_1] Novel grounding evidence."}):
            with patch("services.langgraph_pipeline.run_fidelity", return_value={"error": None, "stage_timings": {}}):
                with patch("services.langgraph_pipeline.run_llm", return_value={
                    "final_answer": "Verified novel answer",
                    "citations": [{"chunk_id": "c_novel_1"}],
                    "synthesis_calls": 1,
                }):
                    out = run_laya_escalation(state)

                    # Assert stale error was cleared and verified answer delivered
                    assert out.get("error") is None
                    assert out.get("final_answer") == "Verified novel answer"
                    assert len(out.get("citations", [])) == 1
                    assert out.get("synthesis_calls") == 1


def test_no_progress_clean_refusal():
    """Verify expansion retrieving existing chunks terminates with clean refusal."""
    chunk_existing = Chunk(
        id="c_already_seen",
        document_id="doc_known",
        source_format="pdf",
        text="Known data chunk.",
        element_type="narrative_block",
        workspace_id="ws_test_no_prog",
    )

    mock_repo = MagicMock()
    mock_repo.get_all_chunks.return_value = [chunk_existing]

    executor = LayaExpansionExecutor()
    decision = LayaEscalationDecision(
        decision_id="dec_no_prog",
        route="lexical_expansion",
        parameters=LexicalExpansionParams(
            route_type="lexical_expansion",
            query_terms=("known", "data"),
        ),
        confidence=0.8,
    )

    # Baseline already contains chunk_existing
    novel, no_prog, st = executor.execute(
        decision=decision,
        workspace_id="ws_test_no_prog",
        baseline_chunks=[chunk_existing],
        chunk_repo=mock_repo,
    )

    assert no_prog is True
    assert len(novel) == 0
    assert st == "no_progress"


def test_invalid_usage_fallback_through_decision_service():
    """Verify InvalidUsageError in decide() falls back to Section 35 policy without TypeError or double settlement."""
    req = _make_sample_request()
    ledger = LayaBudgetLedger()

    # Provider response returns out of bound input tokens (2000 > est_prompt_tokens + 100)
    mock_client = MagicMock()
    mock_client.converse.return_value = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "name": "propose_escalation_route",
                            "input": {
                                "route": "lexical_expansion",
                                "parameters": {"route_type": "lexical_expansion", "query_terms": ["revenue"]},
                                "confidence": 0.9,
                                "rationale": "Targeted lexical query",
                            },
                        }
                    }
                ],
            }
        },
        "usage": {"inputTokens": 2000, "outputTokens": 50},
    }

    ctrl = LayaProviderConcurrencyController()
    service = LayaDecisionService(client=mock_client, concurrency_controller=ctrl)
    try:
        decision = service.decide(request=req, deadline_remaining_ms=2500.0, ledger=ledger)

        # Must fall back gracefully to deterministic policy with rejected_by_code
        assert decision.route == "lexical_expansion"
        assert decision.execution_status == "rejected_by_code"
        assert "Invalid provider usage" in decision.rationale

        # Must not double settle spend: concurrency callback settled 2000/50 once
        assert ledger.total_input_tokens == 2000
        assert ledger.total_output_tokens == 50
        assert len(ledger.settled_history) == 1
        assert ledger.settled_history[-1]["status"] == "invalid_usage"
    finally:
        ctrl.shutdown(wait=False)


def test_missing_usage_dict_fallback_through_decision_service():
    """Verify missing usage dictionary in provider response falls back to Section 35 policy cleanly."""
    req = _make_sample_request()
    ledger = LayaBudgetLedger()

    mock_client = MagicMock()
    mock_client.converse.return_value = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "Unexpected free-form text without usage"}],
            }
        }
    }

    ctrl = LayaProviderConcurrencyController()
    service = LayaDecisionService(client=mock_client, concurrency_controller=ctrl)
    try:
        decision = service.decide(request=req, deadline_remaining_ms=2500.0, ledger=ledger)

        assert decision.route == "lexical_expansion"
        assert decision.execution_status == "rejected_by_code"
        assert "Invalid provider usage" in decision.rationale
        assert ledger.total_input_tokens == 0
        assert ledger.total_output_tokens == 0
        assert len(ledger.settled_history) == 1
        assert ledger.settled_history[-1]["status"] == "invalid_usage"
    finally:
        ctrl.shutdown(wait=False)


def test_dynamic_missing_dimension_derivation_in_pipeline():
    """Verify run_laya_escalation maps various failure modes to proper missing dimensions."""
    mock_client = MagicMock()
    captured_requests: list[LayaEscalationRequest] = []

    class MockService(LayaDecisionService):
        def decide(self, request: LayaEscalationRequest, deadline_remaining_ms: float, ledger: LayaBudgetLedger) -> LayaEscalationDecision:
            captured_requests.append(request)
            return get_deterministic_fallback_decision(request)

    scenarios = [
        # (compressed_text, err, recorded_stage, expected_stage, expected_dim)
        ("", "Empty context", None, "run_fidelity_empty_context", "semantic_concept"),
        ("Some text", "Math operands missing for EBITDA calculation", None, "run_deterministic_math_operands", "tabular_operand"),
        ("Some text", "Missing location page 4 breakdown", None, "run_fidelity_coverage", "location"),
        ("Some text", "Missing relationship between entity A and entity B", None, "run_fidelity_coverage", "relationship"),
        ("Some text", "Missing exact term Q3_guidance_target", None, "run_fidelity_coverage", "exact_term"),
    ]

    for comp, err_msg, rec_stage, exp_stage, exp_dim in scenarios:
        captured_requests.clear()
        state = {
            "query": "Where is the specific metric?",
            "workspace_id": "ws_test_dims",
            "error": err_msg,
            "failed_stage": rec_stage,
            "compressed_text": comp,
            "candidate_chunks": [],
            "top_chunks": [],
            "laya_decision_service": MockService(client=mock_client),
            "laya_expansion_executor": MagicMock(),
            "stage_timings": {},
            "_pipeline_start_time": time.perf_counter(),
        }

        # Mock expansion executor to report no novel chunks so it completes quickly
        state["laya_expansion_executor"].execute.return_value = ([], True, "no_progress")

        run_laya_escalation(state)

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req.missing_requirement.failed_stage == exp_stage
        assert req.missing_requirement.missing_dimension == exp_dim


def test_laya_policy_empty_terms_routes_to_none():
    """Verify that when no alphanumeric terms can be extracted, deterministic fallback routes to none."""
    req = LayaEscalationRequest(
        query_id="q_no_terms",
        workspace_id="ws_test",
        query="??? !!! @@@",
        missing_requirement=MissingRequirementDescription(
            requirement_id="req_empty_terms",
            missing_dimension="exact_term",
            description="Empty evidence",
            failed_stage="run_fidelity_coverage",
            detected_gap="??? !!!",
        ),
        available_routes=("lexical_expansion", "none"),
        retrieved_summary=(),
    )

    decision = get_deterministic_fallback_decision(req)
    assert decision.route == "none"
    assert decision.confidence == 0.0
    assert "No viable terms" in decision.parameters.reason

