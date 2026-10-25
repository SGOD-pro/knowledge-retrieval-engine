"""Offline ablation test suite and regression checks for Spec 0005 Slice 2:
PageIndex Candidate Fusion, Pipeline Wiring, and Offline Ablation.

Verifies:
1. Reciprocal rank fusion soft pooling with structural boost (k=60, structural_weight=1.1)
   without hard page exclusions or pruning baseline chunks (AC-3).
2. Counterexample discovery preservation: query mentioning "Table 1" on Page 3 preserves
   factual answer chunk on Page 14 in RRF and cites Page 14 correctly (AC-3).
3. Offline ablation comparison: candidate page recall@5, precision@3, and latency
   with PageIndex enabled versus disabled against baseline (AC-3, AC-9).
4. Ambiguity candidate pooling: duplicate definition targets pooled with divided weight (AC-6).
5. Cross-reference only candidate pooling: targets with only body cross-references (AC-6).
6. Candidate telemetry exposure in retrieval_candidates trace: structural_candidate_ids,
   structural_match_count, page_index_latency_ms, structural_target_state (AC-8).
7. Benchmark comparison precondition enforcement: blocking accuracy claims on non-canonical
   workspaces (AC-10).
8. Elimination of unscalable whole-workspace get_all_chunks scan in PageIndexStrategy (AC-3).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from config import settings
from db.database import CloudRepository
from db.table_store import get_shared_table_store
from ingestion.embed_service import _deterministic_vector, embed_fast_local
from modules.documents.documents_repository import _IN_MEMORY_CHUNKS, _IN_MEMORY_DOCS
from src.schemas.contracts.boundary import DocumentSnapshot
from src.schemas.contracts.page_index import (
    ElementLocation,
    PageIndexSummary,
    SectionSummary,
    StructuralLookupResult,
)
from schemas.models import Chunk, Document
from services.benchmark.benchmark_runner import (
    validate_benchmark_comparison_preconditions,
)
from services.langgraph_pipeline import (
    _rrf_merge,
    pipeline,
    run_page_index,
)
from services.retrieval.evidence_contract import EvidenceItem
from services.retrieval.page_index_retriever import PageIndexRetriever
from services.retrieval.strategies.page_index import PageIndexStrategy
from src.db.workspace_boundary_repo import get_workspace_boundary_repo


@pytest.fixture(autouse=True)
def clean_test_environment(monkeypatch):
    """Ensure in-memory repositories and test environment settings."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "test")
    monkeypatch.setattr(settings, "PAGEINDEX_ENABLED", True)
    monkeypatch.setattr(settings, "PAGEINDEX_STRUCTURAL_WEIGHT", 1.1)
    b_repo = get_workspace_boundary_repo(reset=True)
    store = get_shared_table_store(reset=True)
    _IN_MEMORY_DOCS.clear()
    _IN_MEMORY_CHUNKS.clear()
    yield b_repo, store
    get_workspace_boundary_repo(reset=True)
    get_shared_table_store(reset=True)
    _IN_MEMORY_DOCS.clear()
    _IN_MEMORY_CHUNKS.clear()


def _make_chunk(
    chunk_id: str,
    doc_id: str,
    workspace_id: str,
    text: str,
    page_number: int,
    element_type: str = "paragraph",
    structural_weight: float = 0.0,
) -> Chunk:
    return Chunk(
        id=chunk_id,
        document_id=doc_id,
        workspace_id=workspace_id,
        text=text,
        source_format="pdf",
        page_number=page_number,
        element_type=element_type,
        structural_weight=structural_weight,
        embedding_fast=embed_fast_local(text),
        embedding_full=_deterministic_vector(text, 1024),
    )


# ===========================================================================
# 1. Reciprocal Rank Fusion Soft Pooling (AC-3)
# ===========================================================================

def test_rrf_soft_pooling_with_structural_weight():
    """Verify RRF pools structural candidates with calibrated weight without pruning baseline chunks."""
    bm25_c1 = _make_chunk("c_b1", "doc1", "ws1", "Revenue in 2024", 1)
    bm25_c2 = _make_chunk("c_b2", "doc1", "ws1", "Expenses in 2024", 2)
    vec_c1 = _make_chunk("c_v1", "doc1", "ws1", "Capital investments", 3)
    vec_c2 = _make_chunk("c_v2", "doc1", "ws1", "Operational metrics", 4)
    struct_c1 = _make_chunk("c_s1", "doc1", "ws1", "Table 1: Financial Overview", 5, structural_weight=1.1)

    merged = _rrf_merge(
        bm25_chunks=[bm25_c1, bm25_c2],
        vector_chunks=[vec_c1, vec_c2],
        structural_chunks=[struct_c1],
        k=60,
        structural_weight=1.1,
    )

    merged_ids = [c.id for c in merged]
    # 1. Zero pruning: all 5 candidate chunks preserved in merged pool
    assert len(merged) == 5
    assert set(merged_ids) == {"c_b1", "c_b2", "c_v1", "c_v2", "c_s1"}

    # 2. Structural candidate receives soft boost score: 1.1 / (60 + 0 + 1) = 1.1 / 61 ≈ 0.01803
    # BM25 rank 0: 1.0 / 61 ≈ 0.01639. Therefore struct_c1 outranks single-source BM25 rank 0.
    assert merged[0].id == "c_s1"

    # 3. Divided ambiguity weight test: with structural_weight=0.55, score is 0.55 / 61 ≈ 0.00902
    struct_ambig = _make_chunk("c_s_ambig", "doc1", "ws1", "Table 1: Variant", 5, structural_weight=0.55)
    merged_ambig = _rrf_merge(
        bm25_chunks=[bm25_c1],
        vector_chunks=[vec_c1],
        structural_chunks=[struct_ambig],
        k=60,
    )
    # Both BM25 and Vector rank 0 score 1.0 / 61; ambig scores 0.55 / 61, so ambig is ranked after
    assert merged_ambig[-1].id == "c_s_ambig"

    # 4. Empty structural candidates leaves baseline unaffected
    baseline_only = _rrf_merge(
        bm25_chunks=[bm25_c1, bm25_c2],
        vector_chunks=[vec_c1, vec_c2],
        structural_chunks=None,
    )
    assert len(baseline_only) == 4
    assert set(c.id for c in baseline_only) == {"c_b1", "c_b2", "c_v1", "c_v2"}


# ===========================================================================
# 2. Counterexample Discovery Preservation (AC-3)
# ===========================================================================

def test_counterexample_discovery_preservation_ac3():
    """Query mentions 'Table 1' (on Page 3) while answer fact resides on Page 14.
    Proves that wrong structural candidate does not prevent retrieval of correct answer.
    """
    repo = CloudRepository()
    b_repo = get_workspace_boundary_repo()

    ws_id = "ws_counterexample_test"
    doc_id = "doc_annual_report"
    doc_ver = "v1"

    # 1. Register DocumentSnapshot in boundary repo
    snapshot = DocumentSnapshot(
        workspace_id=ws_id,
        document_id=doc_id,
        source=f"{doc_id}.pdf",
        version=doc_ver,
        format="pdf",
        created_at="2026-10-24T00:00:00Z",
        persisted_rows=2,
        total_rows=2,
        page_index_status="ready",
        page_index_elements_count=1,
    )
    b_repo.save_document_snapshot(snapshot)

    # 2. Persist PageIndexSummary: Table 1 defined on Page 3
    summary = PageIndexSummary(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=doc_ver,
        total_pages=20,
        layout_status="ready",
        root_sections=(
            SectionSummary(title="Governance", level=1, page_start=1, page_end=5),
            SectionSummary(title="Financials", level=1, page_start=6, page_end=20),
        ),
        inline_definitions={
            "table 1": (
                ElementLocation(
                    element_id=f"{doc_id}:c_p3",
                    page_number=3,
                    element_type="caption",
                    label="table 1",
                    title="Executive Committee Attendance",
                ),
            )
        },
        created_at="2026-10-24T00:00:00Z",
        structural_elements_count=1,
    )
    b_repo.save_page_index_summary(summary)

    # 3. Create Chunks:
    # Page 3: Table 1 (distractor structural match)
    # Page 14: Factual evidence answering query
    chunk_p3 = _make_chunk(
        chunk_id=f"{doc_id}:c_p3",
        doc_id=doc_id,
        workspace_id=ws_id,
        text="Table 1: Executive Committee Attendance. All committee members were present during the annual meeting.",
        page_number=3,
        element_type="table",
    )
    chunk_p14 = _make_chunk(
        chunk_id=f"{doc_id}:c_p14",
        doc_id=doc_id,
        workspace_id=ws_id,
        text="In fiscal year 2024, the net statutory capital reserve was recorded at 42.5 million dollars.",
        page_number=14,
        element_type="paragraph",
    )

    doc = Document(
        id=doc_id,
        filename="annual_report.pdf",
        source_format="pdf",
        chunks=(chunk_p3, chunk_p14),
        workspace_id=ws_id,
    )
    repo.save(doc)

    # 4. Query mentioning Table 1 but asking for capital reserve on page 14
    query = "According to Table 1, what was the net statutory capital reserve in fiscal year 2024?"
    response = pipeline.run(query=query, workspace_id=ws_id)

    # Assertions for AC-3:
    # A. Structural candidate from Page 3 was retrieved
    assert f"{doc_id}:c_p3" in response.structural_candidate_ids
    assert response.structural_target_state == "unique"

    # B. Vector retrieval searched unindexed pages and found Page 14
    vector_ids = response.retrieval_candidates.get("vector", [])
    assert f"{doc_id}:c_p14" in vector_ids

    # C. Zero hard exclusion: RRF retained both Page 3 and Page 14 candidates
    rrf_ids = response.retrieval_candidates.get("rrf", [])
    assert f"{doc_id}:c_p3" in rrf_ids
    assert f"{doc_id}:c_p14" in rrf_ids

    # D. Final answer extracted the grounded fact and cited Page 14, proving Page 14 was not excluded
    assert "42.5 million" in response.answer.lower() or "capital reserve" in response.answer.lower()
    assert len(response.citations) > 0
    cit_pages = [
        c.get("location", {}).get("page") or c.get("page_number")
        for c in response.citations
    ]
    assert 14 in cit_pages, f"Expected citation on Page 14, got citations on pages {cit_pages}"


# ===========================================================================
# 3. Offline Ablation Comparison Suite (AC-3, AC-9)
# ===========================================================================

def test_offline_ablation_suite_recall_precision_latency_ac9(monkeypatch):
    """Run offline ablation harness comparing candidate recall@5, precision@3,
    and latency with PageIndex enabled vs disabled against repaired baseline.
    """
    repo = CloudRepository()
    b_repo = get_workspace_boundary_repo()

    ws_id = "ws_ablation_test"
    doc_id = "doc_ablation_handbook"
    doc_ver = "v1"

    # Setup boundary snapshot
    snapshot = DocumentSnapshot(
        workspace_id=ws_id,
        document_id=doc_id,
        source=f"{doc_id}.pdf",
        version=doc_ver,
        format="pdf",
        created_at="2026-10-24T00:00:00Z",
        persisted_rows=5,
        total_rows=5,
        page_index_status="ready",
        page_index_elements_count=2,
    )
    b_repo.save_document_snapshot(snapshot)

    # Persist PageIndex definitions for Table 2 (Page 5) and Figure 3 (Page 8)
    summary = PageIndexSummary(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=doc_ver,
        total_pages=20,
        layout_status="ready",
        root_sections=(
            SectionSummary(title="Financial Tables", level=1, page_start=1, page_end=10),
            SectionSummary(title="Governance", level=1, page_start=11, page_end=20),
        ),
        inline_definitions={
            "table 2": (
                ElementLocation(
                    element_id=f"{doc_id}:c_t2",
                    page_number=5,
                    element_type="table",
                    label="table 2",
                    title="Operating Liabilities Summary",
                ),
            ),
            "figure 3": (
                ElementLocation(
                    element_id=f"{doc_id}:c_f3",
                    page_number=8,
                    element_type="figure",
                    label="figure 3",
                    title="Cash Flow Trajectory Diagram",
                ),
            ),
            "table 1": (
                ElementLocation(
                    element_id=f"{doc_id}:c_t1",
                    page_number=3,
                    element_type="table",
                    label="table 1",
                    title="Board Meeting Schedule",
                ),
            ),
        },
        created_at="2026-10-24T00:00:00Z",
        structural_elements_count=3,
    )
    b_repo.save_page_index_summary(summary)

    # Ingest representative corpus chunks across multiple pages
    chunks = (
        _make_chunk(f"{doc_id}:c_t1", doc_id, ws_id, "Table 1: Board Meeting Schedule and quarterly dates.", 3, "table"),
        _make_chunk(f"{doc_id}:c_t2", doc_id, ws_id, "Table 2: Operating Liabilities Summary. Total obligations reached 120 million.", 5, "table"),
        _make_chunk(f"{doc_id}:c_f3", doc_id, ws_id, "Figure 3: Cash Flow Trajectory Diagram illustrating liquidity reserves.", 8, "figure"),
        _make_chunk(f"{doc_id}:c_narrative", doc_id, ws_id, "The primary corporate compliance policy mandates biannual independent auditing.", 11, "paragraph"),
        _make_chunk(f"{doc_id}:c_counter", doc_id, ws_id, "Foreign exchange hedging exposure was limited to 8 percent of total capital in 2023.", 14, "paragraph"),
    )
    doc = Document(id=doc_id, filename="handbook.pdf", source_format="pdf", chunks=chunks, workspace_id=ws_id)
    repo.save(doc)

    # Corpus queries with defined target pages
    test_queries = [
        # (query, target_page, query_type)
        ("What are the total obligations in Table 2?", 5, "structural_table"),
        ("What is illustrated in Figure 3 regarding liquidity?", 8, "structural_figure"),
        ("What does the corporate compliance policy mandate?", 11, "narrative_unindexed"),
        ("Referencing Table 1, what was the foreign exchange hedging exposure in 2023?", 14, "counterexample"),
    ]

    results_enabled = []
    results_disabled = []

    # 1. Evaluate with PageIndex ENABLED
    monkeypatch.setattr(settings, "PAGEINDEX_ENABLED", True)
    for q, tgt_page, q_type in test_queries:
        resp = pipeline.run(query=q, workspace_id=ws_id)
        cands = resp.top_chunks if resp.top_chunks else []
        cand_pages = [c.page_number for c in cands]
        top5_pages = cand_pages[:5]
        top3_pages = cand_pages[:3]
        recall_at_5 = 1.0 if tgt_page in top5_pages else 0.0
        prec_at_3 = sum(1 for p in top3_pages if p == tgt_page) / max(1, len(top3_pages))
        latency = resp.page_index_latency_ms
        results_enabled.append({
            "query": q,
            "type": q_type,
            "recall@5": recall_at_5,
            "prec@3": prec_at_3,
            "latency_ms": latency,
        })

    # 2. Evaluate with PageIndex DISABLED
    monkeypatch.setattr(settings, "PAGEINDEX_ENABLED", False)
    for q, tgt_page, q_type in test_queries:
        resp = pipeline.run(query=q, workspace_id=ws_id)
        cands = resp.top_chunks if resp.top_chunks else []
        cand_pages = [c.page_number for c in cands]
        top5_pages = cand_pages[:5]
        top3_pages = cand_pages[:3]
        recall_at_5 = 1.0 if tgt_page in top5_pages else 0.0
        prec_at_3 = sum(1 for p in top3_pages if p == tgt_page) / max(1, len(top3_pages))
        results_disabled.append({
            "query": q,
            "type": q_type,
            "recall@5": recall_at_5,
            "prec@3": prec_at_3,
        })

    # Assertions for AC-9 and AC-3:
    mean_recall_enabled = sum(r["recall@5"] for r in results_enabled) / len(results_enabled)
    mean_recall_disabled = sum(r["recall@5"] for r in results_disabled) / len(results_disabled)

    # A. PageIndex enabled recall@5 is >= baseline recall (no degradation)
    assert mean_recall_enabled >= mean_recall_disabled
    assert mean_recall_enabled >= 0.75

    # B. On structural queries (Table 2, Figure 3), PageIndex candidate recall is 1.0
    structural_recalls = [r["recall@5"] for r in results_enabled if r["type"].startswith("structural")]
    assert all(r == 1.0 for r in structural_recalls)

    # C. On counterexample query, PageIndex enabled preserves ground truth Page 14 in candidate set
    counter_res = next(r for r in results_enabled if r["type"] == "counterexample")
    assert counter_res["recall@5"] == 1.0

    # D. Latency of PageIndex lookup strictly honors 50ms deadline
    for r in results_enabled:
        assert r["latency_ms"] < 50.0, f"Latency {r['latency_ms']}ms exceeded 50ms deadline"


# ===========================================================================
# 4. Ambiguity Candidate Pooling (AC-6)
# ===========================================================================

def test_ambiguity_candidate_pooling_ac6():
    """Verify that duplicate definition targets are marked ambiguous and pooled with divided weight."""
    repo = CloudRepository()
    b_repo = get_workspace_boundary_repo()

    ws_id = "ws_ambiguity_test"
    doc_id = "doc_dual_tables"
    doc_ver = "v1"

    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source=f"{doc_id}.pdf",
            version=doc_ver,
            format="pdf",
            created_at="2026-10-24T00:00:00Z",
            persisted_rows=2,
            total_rows=2,
            page_index_status="ready",
            page_index_elements_count=2,
        )
    )

    # Document has two distinct definitions labeled "Table 1" on Page 5 and Page 12
    summary = PageIndexSummary(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=doc_ver,
        total_pages=15,
        layout_status="ready",
        root_sections=(),
        inline_definitions={
            "table 1": (
                ElementLocation(element_id=f"{doc_id}:c5", page_number=5, element_type="table", label="table 1", title="Regional Sales"),
                ElementLocation(element_id=f"{doc_id}:c12", page_number=12, element_type="table", label="table 1", title="Appendix Risk Factors"),
            )
        },
        created_at="2026-10-24T00:00:00Z",
        structural_elements_count=2,
    )
    b_repo.save_page_index_summary(summary)

    c5 = _make_chunk(f"{doc_id}:c5", doc_id, ws_id, "Table 1: Regional Sales breakdown", 5, "table")
    c12 = _make_chunk(f"{doc_id}:c12", doc_id, ws_id, "Table 1: Appendix Risk Factors catalog", 12, "table")
    repo.save(Document(id=doc_id, filename="dual.pdf", source_format="pdf", chunks=(c5, c12), workspace_id=ws_id))

    # Run page_index node directly
    state = {
        "workspace_id": ws_id,
        "query": "What data is in Table 1?",
        "stage_timings": {},
    }
    out = run_page_index(state)

    # Assertions for AC-6:
    assert out["structural_target_state"] == "ambiguous"
    assert out["structural_match_count"] == 2
    cand_ids = [c.id for c in out["structural_candidates"]]
    assert f"{doc_id}:c5" in cand_ids
    assert f"{doc_id}:c12" in cand_ids

    # Divided weight verification: 1.1 * 0.5 = 0.55
    for c in out["structural_candidates"]:
        assert pytest.approx(c.structural_weight, 0.01) == 0.55


# ===========================================================================
# 5. Cross-Reference Only Candidate Pooling (AC-6)
# ===========================================================================

def test_cross_reference_only_candidate_pooling_ac6():
    """Verify that targets appearing only as body cross-references are pooled appropriately."""
    repo = CloudRepository()
    b_repo = get_workspace_boundary_repo()

    ws_id = "ws_xref_test"
    doc_id = "doc_xref"
    doc_ver = "v1"

    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source=f"{doc_id}.pdf",
            version=doc_ver,
            format="pdf",
            created_at="2026-10-24T00:00:00Z",
            persisted_rows=1,
            total_rows=1,
            page_index_status="ready",
            page_index_elements_count=0,
        )
    )

    summary = PageIndexSummary(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=doc_ver,
        total_pages=10,
        layout_status="ready",
        root_sections=(),
        inline_definitions={},
        inline_cross_references={"table 9": (7,)},
        created_at="2026-10-24T00:00:00Z",
        structural_elements_count=0,
    )
    b_repo.save_page_index_summary(summary)

    c7 = _make_chunk(f"{doc_id}:c7", doc_id, ws_id, "Paragraph on page 7 mentioning as detailed in Table 9.", 7)
    repo.save(Document(id=doc_id, filename="xref.pdf", source_format="pdf", chunks=(c7,), workspace_id=ws_id))

    state = {
        "workspace_id": ws_id,
        "query": "Where is Table 9 referenced?",
        "stage_timings": {},
    }
    out = run_page_index(state)

    assert out["structural_match_count"] == 1
    assert out["structural_candidates"][0].id == f"{doc_id}:c7"


# ===========================================================================
# 6. Candidate Telemetry in Retrieval Candidates Trace (AC-8)
# ===========================================================================

def test_telemetry_exposure_in_retrieval_candidates_ac8():
    """Verify that retrieval_candidates trace records all AC-8 structural fields."""
    repo = CloudRepository()
    b_repo = get_workspace_boundary_repo()

    ws_id = "ws_telemetry_test"
    doc_id = "doc_telemetry"
    doc_ver = "v1"

    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source=f"{doc_id}.pdf",
            version=doc_ver,
            format="pdf",
            created_at="2026-10-24T00:00:00Z",
            persisted_rows=1,
            total_rows=1,
            page_index_status="ready",
            page_index_elements_count=1,
        )
    )

    summary = PageIndexSummary(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=doc_ver,
        total_pages=5,
        layout_status="ready",
        root_sections=(),
        inline_definitions={
            "figure 1": (
                ElementLocation(element_id=f"{doc_id}:c2", page_number=2, element_type="figure", label="figure 1", title="System Topology"),
            )
        },
        created_at="2026-10-24T00:00:00Z",
        structural_elements_count=1,
    )
    b_repo.save_page_index_summary(summary)

    c2 = _make_chunk(f"{doc_id}:c2", doc_id, ws_id, "Figure 1: System Topology diagram showing node connectivity.", 2, "figure")
    repo.save(Document(id=doc_id, filename="telemetry.pdf", source_format="pdf", chunks=(c2,), workspace_id=ws_id))

    response = pipeline.run(query="What is depicted in Figure 1?", workspace_id=ws_id)

    # Check ResponseObject properties directly
    assert response.structural_candidate_ids == [f"{doc_id}:c2"]
    assert response.structural_match_count == 1
    assert isinstance(response.page_index_latency_ms, float)
    assert response.page_index_latency_ms >= 0.0
    assert response.structural_target_state == "unique"

    # Check retrieval_candidates dictionary exposed in debug trace
    rc = response.retrieval_candidates
    assert rc.get("structural_candidate_ids") == [f"{doc_id}:c2"]
    assert rc.get("structural_match_count") == 1
    assert rc.get("structural_target_state") == "unique"
    assert isinstance(rc.get("page_index_latency_ms"), (int, float))


# ===========================================================================
# 7. Benchmark Comparison Precondition Gate (AC-10)
# ===========================================================================

def test_benchmark_comparison_preconditions_ac10():
    """Verify that benchmark accuracy comparisons remain blocked until controlled run on canonical workspace."""
    # 1. Non-canonical workspace raises ValueError blocking claims
    with pytest.raises(ValueError, match="Benchmark comparison claims are blocked on workspace"):
        validate_benchmark_comparison_preconditions(
            baseline_run_id="run_base_1",
            candidate_run_id="run_cand_1",
            comparison_workspace_id="ws_sandbox_dev",
        )

    # 2. Missing run IDs raise ValueError
    with pytest.raises(ValueError, match="Both baseline_run_id and candidate_run_id are required"):
        validate_benchmark_comparison_preconditions(
            baseline_run_id="",
            candidate_run_id="run_cand_1",
            comparison_workspace_id="ws_canonical_comparison_0004",
        )

    # 3. Canonical comparison workspace with valid IDs passes
    is_valid = validate_benchmark_comparison_preconditions(
        baseline_run_id="run_base_1",
        candidate_run_id="run_cand_1",
        comparison_workspace_id="ws_canonical_comparison_0004",
    )
    assert is_valid is True


# ===========================================================================
# 8. PageIndexStrategy Eliminates Unscalable Scan (AC-3)
# ===========================================================================

@pytest.mark.anyio
async def test_page_index_strategy_eliminates_unscalable_scan():
    """Verify that PageIndexStrategy does not call get_all_chunks(workspace_id) across all workspace chunks."""
    mock_repo = MagicMock()
    mock_retriever = MagicMock()

    # Configure target label extractor to return empty for generic query
    mock_retriever.lookup_service.extract_target_labels.return_value = []

    strategy = PageIndexStrategy(repository=mock_repo, retriever=mock_retriever)
    res = await strategy.retrieve(query="What is the mission overview?", workspace_id="ws_test")

    # With no structural labels, get_all_chunks should NEVER be called
    mock_repo.get_all_chunks.assert_not_called()
    assert res.items == []

    # Now configure structural label match
    mock_retriever.lookup_service.extract_target_labels.return_value = ["table 4"]

    b_repo = get_workspace_boundary_repo()
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id="ws_targeted",
            document_id="doc_t4",
            source="doc_t4.pdf",
            version="v1",
            format="pdf",
            created_at="2026-10-24T00:00:00Z",
            persisted_rows=1,
            total_rows=1,
            page_index_status="ready",
            page_index_elements_count=1,
        )
    )

    mock_retriever.lookup = AsyncMock(
        return_value=(
            StructuralLookupResult(
                target_label="table 4",
                candidate_pages=(6,),
                state="unique",
            ),
            "ready",
        )
    )
    mock_repo.get_all_chunks.return_value = [
        _make_chunk("doc_t4:c6", "doc_t4", "ws_targeted", "Table 4 content", 6, "table")
    ]

    strategy_targeted = PageIndexStrategy(repository=mock_repo, retriever=mock_retriever)
    res_targeted = await strategy_targeted.retrieve(query="Show me Table 4", workspace_id="ws_targeted")

    # Verify get_all_chunks was called with specific document_ids and targeted pages, NOT whole-workspace scan
    mock_repo.get_all_chunks.assert_called_once_with(
        document_ids=["doc_t4"],
        workspace_id="ws_targeted",
        page_numbers=(6,),
    )
    assert len(res_targeted.items) == 1
    assert res_targeted.items[0].locator["candidate_pages"] == [6]


# ===========================================================================
# 9. Actual Pipeline Path Regressions: Slow Storage, Saturation, Stage Bounding
# ===========================================================================

def test_pipeline_actual_path_slow_storage_fallback_to_baseline(monkeypatch):
    """Verify actual pipeline path enforces caller deadlines on slow storage and preserves baseline."""
    monkeypatch.setattr(settings, "PAGEINDEX_TIMEOUT_MS", 25)
    monkeypatch.setattr(settings, "PAGEINDEX_STAGE_TIMEOUT_MS", 50)

    ws_id = "ws_pipeline_slow"
    b_repo = get_workspace_boundary_repo()
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id="doc_slow",
            source="doc_slow.pdf",
            version="v1",
            format="pdf",
            created_at="2026-10-24T00:00:00Z",
            persisted_rows=1,
            total_rows=1,
            page_index_status="ready",
            page_index_elements_count=5,
        )
    )

    # Inject slow storage read (70ms) into PageIndexRepository.get_summary
    from src.db.page_index_repo import PageIndexRepository
    def slow_get_summary(*args, **kwargs):
        time.sleep(0.070)
        return None

    monkeypatch.setattr(PageIndexRepository, "get_summary", slow_get_summary)

    # Initial pipeline state with baseline candidate chunks
    baseline_chunk = _make_chunk("c_base_1", "doc_slow", ws_id, "Baseline factual answer", 14, "paragraph")
    state = {
        "workspace_id": ws_id,
        "query": "According to Table 1, what is the score?",
        "bm25_candidates": [baseline_chunk],
        "candidate_chunks": [baseline_chunk],
        "stage_timings": {},
        "retrieval_candidates": {},
    }

    # Warm up shared repository singleton to isolate storage work from cold start instantiation
    from services.langgraph_pipeline import _get_repo
    _get_repo()

    t0 = time.perf_counter()
    res_state = run_page_index(state)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # Must return promptly without exceeding total deadline budget
    assert elapsed_ms < 60.0

    # PageIndex timed out: structural candidates are empty
    assert res_state["structural_candidate_ids"] == []
    assert res_state["structural_match_count"] == 0
    assert res_state["structural_target_state"] == "none"

    # Baseline candidate chunks remain completely preserved and unpruned
    assert "candidate_chunks" not in res_state or res_state["candidate_chunks"] is None


def test_pipeline_actual_path_concurrency_saturation_fallback_to_baseline(monkeypatch):
    """Verify actual pipeline path handles admission queue saturation gracefully, falling back to baseline."""
    from src.services.retrieval.page_index_lookup import PageIndexConcurrencyController, get_page_index_controller
    import src.services.retrieval.page_index_lookup as p_lookup

    # Controller with max_concurrency 1, max_queue 0 (any contending call rejected immediately)
    saturated_controller = PageIndexConcurrencyController(max_concurrency=1, max_queue=0)
    # Occupy the single worker slot
    import threading
    worker_running = threading.Event()
    worker_release = threading.Event()

    def slot_occupier(*args):
        worker_running.set()
        worker_release.wait(timeout=1.0)
        return "occupied"

    import concurrent.futures
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    pool.submit(saturated_controller.run_storage_work_sync, slot_occupier, deadline_s=1.0)
    worker_running.wait(timeout=0.5)

    monkeypatch.setattr(p_lookup, "_PAGE_INDEX_CONTROLLER", saturated_controller)

    ws_id = "ws_pipeline_sat"
    b_repo = get_workspace_boundary_repo()
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id="doc_sat",
            source="doc_sat.pdf",
            version="v1",
            format="pdf",
            created_at="2026-10-24T00:00:00Z",
            persisted_rows=1,
            total_rows=1,
            page_index_status="ready",
            page_index_elements_count=5,
        )
    )

    baseline_chunk = _make_chunk("c_base_sat", "doc_sat", ws_id, "Baseline factual answer", 10, "paragraph")
    state = {
        "workspace_id": ws_id,
        "query": "Look at Table 2 for metrics",
        "bm25_candidates": [baseline_chunk],
        "candidate_chunks": [baseline_chunk],
        "stage_timings": {},
        "retrieval_candidates": {},
    }

    res_state = run_page_index(state)

    # Queue saturation rejected lookup immediately; baseline discovery is preserved
    assert res_state["structural_candidate_ids"] == []
    assert res_state["structural_match_count"] == 0

    # Cleanup background thread
    worker_release.set()
    pool.shutdown(wait=True)


def test_pipeline_total_stage_deadline_bounded_across_multiple_documents_and_labels(monkeypatch):
    """Verify total PageIndex stage work across multiple documents and labels is strictly bounded."""
    monkeypatch.setattr(settings, "PAGEINDEX_TIMEOUT_MS", 20)
    monkeypatch.setattr(settings, "PAGEINDEX_STAGE_TIMEOUT_MS", 45)

    ws_id = "ws_stage_budget"
    b_repo = get_workspace_boundary_repo()

    # Create 6 PDF document snapshots
    for i in range(1, 7):
        b_repo.save_document_snapshot(
            DocumentSnapshot(
                workspace_id=ws_id,
                document_id=f"doc_budget_{i}",
                source=f"doc_{i}.pdf",
                version="v1",
                format="pdf",
                created_at="2026-10-24T00:00:00Z",
                persisted_rows=1,
                total_rows=1,
                page_index_status="ready",
                page_index_elements_count=5,
            )
        )

    # Inject a 15ms delay per lookup
    # 6 docs * 2 labels = 12 lookups. Unbounded would take 12 * 15ms = 180ms!
    from src.db.page_index_repo import PageIndexRepository
    def delay_get_summary(*args, **kwargs):
        time.sleep(0.015)
        return None

    monkeypatch.setattr(PageIndexRepository, "get_summary", delay_get_summary)

    state = {
        "workspace_id": ws_id,
        "query": "Examine Table 1 and Figure 2 for results",  # 2 target labels
        "stage_timings": {},
        "retrieval_candidates": {},
    }

    # Warm up shared repository singleton to isolate storage work from cold start instantiation
    from services.langgraph_pipeline import _get_repo
    _get_repo()

    t0 = time.perf_counter()
    res_state = run_page_index(state)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # Total stage work must NOT run all 180ms. It must break when stage budget (45ms) is reached!
    assert elapsed_ms < 75.0  # bounded strictly within budget plus minimal thread dispatch
    assert res_state["structural_match_count"] == 0


def test_pipeline_slow_snapshot_discovery_bounded_and_baseline_fallback(monkeypatch):
    """Verify snapshot discovery under controller enforces deadline and falls back to baseline."""
    monkeypatch.setattr(settings, "PAGEINDEX_TIMEOUT_MS", 25)
    monkeypatch.setattr(settings, "PAGEINDEX_STAGE_TIMEOUT_MS", 50)

    ws_id = "ws_pipeline_slow_snaps"
    b_repo = get_workspace_boundary_repo()
    orig_list_snaps = type(b_repo).list_document_snapshots

    def slow_list_snaps(self, workspace_id, *args, **kwargs):
        time.sleep(0.070)
        return orig_list_snaps(self, workspace_id, *args, **kwargs)

    monkeypatch.setattr(type(b_repo), "list_document_snapshots", slow_list_snaps)

    baseline_chunk = _make_chunk("c_base_snap", "doc_snap", ws_id, "Baseline factual answer", 2, "paragraph")
    state = {
        "workspace_id": ws_id,
        "query": "Look at Table 1 for details",
        "bm25_candidates": [baseline_chunk],
        "candidate_chunks": [baseline_chunk],
        "stage_timings": {},
        "retrieval_candidates": {},
    }

    # Warm up shared repository singleton to isolate storage work from cold start instantiation
    from services.langgraph_pipeline import _get_repo
    _get_repo()

    t0 = time.perf_counter()
    res_state = run_page_index(state)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # Bounded caller latency (<60ms despite 70ms snapshot discovery delay)
    assert elapsed_ms < 60.0

    # Snapshot discovery timed out: structural candidates empty, baseline preserved
    assert res_state["structural_candidate_ids"] == []
    assert res_state["structural_match_count"] == 0
    assert res_state["structural_target_state"] == "none"


def test_pipeline_slow_tombstone_check_bounded_and_fail_closed(monkeypatch):
    """Verify tombstone check inside worker enforces caller deadline, retains capacity, and fails closed."""
    from src.ingestion.page_index_extractor import extract_layout_from_elements

    monkeypatch.setattr(settings, "PAGEINDEX_TIMEOUT_MS", 25)
    monkeypatch.setattr(settings, "PAGEINDEX_STAGE_TIMEOUT_MS", 50)

    ws_id = "ws_pipeline_slow_tomb"
    b_repo = get_workspace_boundary_repo()
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id="doc_tomb_slow",
            source="doc_tomb_slow.pdf",
            version="v1",
            format="pdf",
            created_at="2026-10-24T00:00:00Z",
            persisted_rows=1,
            total_rows=1,
            page_index_status="ready",
            page_index_elements_count=5,
        )
    )

    # Persist summary with Table 1 defined on page 5
    from src.db.page_index_repo import PageIndexRepository
    pi_repo = PageIndexRepository()
    elements = [
        {"type": "table", "content": "Table 1: Metrics", "page_number": 5, "label": "Table 1"},
    ]
    layout = extract_layout_from_elements(elements, total_pages=10, document_id="doc_tomb_slow")
    pi_repo.persist_manifest(ws_id, "doc_tomb_slow", "v1", layout)

    def slow_has_tombstone(self, workspace_id, resource_type, resource_id):
        time.sleep(0.070)
        return False

    monkeypatch.setattr(type(b_repo), "has_tombstone", slow_has_tombstone)

    baseline_chunk = _make_chunk("c_base_tomb", "doc_tomb_slow", ws_id, "Baseline answer", 5, "paragraph")
    state = {
        "workspace_id": ws_id,
        "query": "According to Table 1, what are the metrics?",
        "bm25_candidates": [baseline_chunk],
        "candidate_chunks": [baseline_chunk],
        "stage_timings": {},
        "retrieval_candidates": {},
    }

    # Warm up shared repository singleton to isolate storage work from cold start instantiation
    from services.langgraph_pipeline import _get_repo
    _get_repo()

    t0 = time.perf_counter()
    res_state = run_page_index(state)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # Bounded caller latency (<60ms despite 70ms tombstone delay)
    assert elapsed_ms < 60.0

    # Fail closed: no candidates published when tombstone check cannot finish within deadline
    assert res_state["structural_candidate_ids"] == []
    assert res_state["structural_match_count"] == 0
    assert res_state["structural_target_state"] == "none"


def test_pipeline_slow_candidate_chunk_fetching_bounded_and_no_late_publication(monkeypatch):
    """Verify candidate chunk fetching inside worker enforces deadline and prevents late publication."""
    import threading
    from src.ingestion.page_index_extractor import extract_layout_from_elements

    monkeypatch.setattr(settings, "PAGEINDEX_TIMEOUT_MS", 25)
    monkeypatch.setattr(settings, "PAGEINDEX_STAGE_TIMEOUT_MS", 50)

    ws_id = "ws_pipeline_slow_chunks"
    b_repo = get_workspace_boundary_repo()
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id="doc_chunk_slow",
            source="doc_chunk_slow.pdf",
            version="v1",
            format="pdf",
            created_at="2026-10-24T00:00:00Z",
            persisted_rows=1,
            total_rows=1,
            page_index_status="ready",
            page_index_elements_count=5,
        )
    )

    from src.db.page_index_repo import PageIndexRepository
    pi_repo = PageIndexRepository()
    elements = [
        {"type": "table", "content": "Table 1: Target Content", "page_number": 4, "label": "Table 1"},
    ]
    layout = extract_layout_from_elements(elements, total_pages=10, document_id="doc_chunk_slow")
    pi_repo.persist_manifest(ws_id, "doc_chunk_slow", "v1", layout)

    from db.database import CloudRepository

    worker_finished = threading.Event()

    def slow_get_all_chunks(self, *args, **kwargs):
        time.sleep(0.070)
        worker_finished.set()
        return [_make_chunk("c_late_table", "doc_chunk_slow", ws_id, "Late Table 1 content", 4, "table")]

    monkeypatch.setattr(CloudRepository, "get_all_chunks", slow_get_all_chunks)

    baseline_chunk = _make_chunk("c_base_chunk", "doc_chunk_slow", ws_id, "Baseline answer", 1, "paragraph")
    state = {
        "workspace_id": ws_id,
        "query": "Show me Table 1",
        "bm25_candidates": [baseline_chunk],
        "candidate_chunks": [baseline_chunk],
        "stage_timings": {},
        "retrieval_candidates": {},
    }

    # Warm up shared repository singleton to isolate storage work from cold start instantiation
    from services.langgraph_pipeline import _get_repo
    _get_repo()

    t0 = time.perf_counter()
    res_state = run_page_index(state)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # Bounded caller latency (<60ms despite 70ms chunk read delay)
    assert elapsed_ms < 60.0

    # Caller timed out on chunk read: structural candidates are empty, baseline preserved
    assert res_state["structural_candidate_ids"] == []
    assert res_state["structural_match_count"] == 0
    assert res_state["structural_target_state"] == "none"

    # Wait for background worker to finish
    worker_finished.wait(timeout=1.0)

    # Prove no late publication: the returned state candidate list never mutated or published late chunks
    assert res_state["structural_candidate_ids"] == []
    assert res_state["structural_match_count"] == 0


def test_pipeline_slow_s3_detail_read_bounded_and_baseline_fallback(monkeypatch):
    """Verify S3 overflow detail read inside worker enforces deadline and falls back to baseline."""
    from src.ingestion.page_index_extractor import extract_layout_from_elements

    monkeypatch.setattr(settings, "PAGEINDEX_TIMEOUT_MS", 25)
    monkeypatch.setattr(settings, "PAGEINDEX_STAGE_TIMEOUT_MS", 50)

    ws_id = "ws_pipeline_slow_s3"
    b_repo = get_workspace_boundary_repo()
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id="doc_s3_slow",
            source="doc_s3_slow.pdf",
            version="v1",
            format="pdf",
            created_at="2026-10-24T00:00:00Z",
            persisted_rows=1,
            total_rows=1,
            page_index_status="ready",
            page_index_elements_count=50,
        )
    )

    from src.db.page_index_repo import PageIndexRepository
    pi_repo = PageIndexRepository()
    elements = []
    for i in range(1, 40):
        elements.append({"type": "table", "content": f"Table {i} detailed content with excessive metadata to overflow payload size limit", "page_number": i, "label": f"Table {i}"})
    layout = extract_layout_from_elements(elements, total_pages=50, document_id="doc_s3_slow")
    pi_repo.persist_manifest(ws_id, "doc_s3_slow", "v1", layout)

    def slow_get_detail(*args, **kwargs):
        time.sleep(0.070)
        return None

    monkeypatch.setattr(PageIndexRepository, "get_detail", slow_get_detail)

    baseline_chunk = _make_chunk("c_base_s3", "doc_s3_slow", ws_id, "Baseline answer", 1, "paragraph")
    state = {
        "workspace_id": ws_id,
        "query": "Show me Table 15",
        "bm25_candidates": [baseline_chunk],
        "candidate_chunks": [baseline_chunk],
        "stage_timings": {},
        "retrieval_candidates": {},
    }

    # Warm up shared repository singleton to isolate storage work from cold start instantiation
    from services.langgraph_pipeline import _get_repo
    _get_repo()

    t0 = time.perf_counter()
    res_state = run_page_index(state)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # Bounded caller latency (<60ms)
    assert elapsed_ms < 60.0
    assert res_state["structural_candidate_ids"] == []
    assert res_state["structural_match_count"] == 0


