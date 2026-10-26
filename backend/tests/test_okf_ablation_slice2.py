"""Offline ablation test suite and regression checks for Spec 0006 Slice 2:
Open Knowledge Framework (OKF) Candidate Fusion, Pipeline Wiring, and Offline Ablation.

Verifies:
1. Reciprocal rank fusion soft pooling with OKF boost (k=60, okf_weight=1.15)
   without hard page exclusions or pruning baseline chunks (AC-9).
2. Counterexample discovery preservation: query matching OKF entity on Page 2 preserves
   factual answer chunk on Page 18 in RRF and cites Page 18 correctly (AC-9).
3. Ambiguity & conflict candidate pooling: ambiguous entity names and conflicting properties
   pooled with divided weight (0.575) (AC-4, AC-5).
4. Fast-path query bypass: queries executing fast path skip OKF lookups completely (AC-9).
5. Removal of legacy pre-BM25 OKF_BOOST in run_bm25 (AC-9).
6. Candidate telemetry exposure in retrieval_candidates trace: okf, okf_candidate_ids,
   okf_match_count, okf_latency_ms, okf_entity_state, okf_property_state (AC-11).
7. Offline ablation comparison: candidate page recall@5, precision@3, and latency
   with OKF enabled versus disabled against baseline (AC-12).
8. Benchmark comparison precondition enforcement: blocking accuracy claims on non-canonical
   workspaces (AC-12).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch
import pytest

from config import settings
from db.database import CloudRepository
from db.table_store import get_shared_table_store
from ingestion.embed_service import _deterministic_vector, embed_fast_local
from modules.documents.documents_repository import _IN_MEMORY_CHUNKS, _IN_MEMORY_DOCS
from schemas.models import Chunk, Document
from services.benchmark.benchmark_runner import (
    validate_benchmark_comparison_preconditions,
)
from services.retrieval.planner import Plan
from services.langgraph_pipeline import (
    _rrf_merge,
    pipeline,
    run_bm25,
    run_okf_router,
)
from src.db.okf_repo import get_okf_repo
from src.db.workspace_boundary_repo import get_workspace_boundary_repo
from src.schemas.contracts.boundary import DocumentSnapshot
from src.schemas.contracts.okf import (
    OkfEntityRecord,
    OkfLookupResult,
    OkfManifest,
    OkfPropertyAssertion,
)
from src.services.retrieval.okf_lookup import extract_entity_names_from_query


@pytest.fixture(autouse=True)
def clean_test_environment(monkeypatch):
    """Ensure in-memory repositories and test environment settings."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "test")
    monkeypatch.setattr(settings, "OKF_ENABLED", True)
    monkeypatch.setattr(settings, "OKF_FUSION_WEIGHT", 1.15)
    monkeypatch.setattr(settings, "OKF_TIMEOUT_MS", 50)
    monkeypatch.setattr(settings, "VECTOR_THRESHOLD", 0.0)
    b_repo = get_workspace_boundary_repo(reset=True)
    okf_repo = get_okf_repo(reset=True)
    store = get_shared_table_store(reset=True)
    _IN_MEMORY_DOCS.clear()
    _IN_MEMORY_CHUNKS.clear()
    yield b_repo, okf_repo, store
    get_workspace_boundary_repo(reset=True)
    get_okf_repo(reset=True)
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
    okf_weight: float = 0.0,
) -> Chunk:
    return Chunk(
        id=chunk_id,
        document_id=doc_id,
        workspace_id=workspace_id,
        text=text,
        source_format="pdf",
        page_number=page_number,
        element_type=element_type,
        okf_weight=okf_weight,
        embedding_fast=embed_fast_local(text),
        embedding_full=_deterministic_vector(text, 1024),
    )


# ===========================================================================
# 1. Reciprocal Rank Fusion Soft Pooling (AC-9)
# ===========================================================================

def test_rrf_soft_pooling_with_okf_weight():
    """Verify RRF pools OKF candidates with calibrated weight without pruning baseline chunks."""
    bm25_c1 = _make_chunk("c_b1", "doc1", "ws1", "Operating revenue in 2024", 1)
    bm25_c2 = _make_chunk("c_b2", "doc1", "ws1", "Operating expenses in 2024", 2)
    vec_c1 = _make_chunk("c_v1", "doc1", "ws1", "Long term debt obligations", 3)
    vec_c2 = _make_chunk("c_v2", "doc1", "ws1", "Cash and cash equivalents", 4)
    okf_c1 = _make_chunk("c_o1", "doc1", "ws1", "CEO of Acme Corp is Jane Doe", 5, okf_weight=1.15)

    merged = _rrf_merge(
        bm25_chunks=[bm25_c1, bm25_c2],
        vector_chunks=[vec_c1, vec_c2],
        okf_chunks=[okf_c1],
        k=60,
        okf_weight=1.15,
    )

    merged_ids = [c.id for c in merged]
    # 1. Zero pruning: all 5 candidate chunks preserved in merged pool
    assert len(merged) == 5
    assert set(merged_ids) == {"c_b1", "c_b2", "c_v1", "c_v2", "c_o1"}

    # 2. OKF candidate receives soft boost score: 1.15 / (60 + 0 + 1) = 1.15 / 61 ≈ 0.01885
    # BM25 rank 0: 1.0 / 61 ≈ 0.01639. Therefore okf_c1 outranks single-source BM25 rank 0.
    assert merged[0].id == "c_o1"

    # 3. Divided ambiguity weight test: with okf_weight=0.575, score is 0.575 / 61 ≈ 0.00942
    okf_ambig = _make_chunk("c_o_ambig", "doc1", "ws1", "Ambiguous CEO info", 5, okf_weight=0.575)
    merged_ambig = _rrf_merge(
        bm25_chunks=[bm25_c1],
        vector_chunks=[vec_c1],
        okf_chunks=[okf_ambig],
        k=60,
    )
    # Both BM25 and Vector rank 0 score 1.0 / 61; ambig scores 0.575 / 61, so ambig is ranked after
    assert merged_ambig[-1].id == "c_o_ambig"

    # 4. Empty OKF candidates leaves baseline unaffected
    baseline_only = _rrf_merge(
        bm25_chunks=[bm25_c1, bm25_c2],
        vector_chunks=[vec_c1, vec_c2],
        okf_chunks=None,
    )
    assert len(baseline_only) == 4
    assert set(c.id for c in baseline_only) == {"c_b1", "c_b2", "c_v1", "c_v2"}


# ===========================================================================
# 2. Counterexample Discovery Preservation (AC-9)
# ===========================================================================

def test_counterexample_discovery_preservation_ac9():
    """Query mentions officer (on Page 2) while answer fact resides on Page 18.
    Proves that matching an OKF entity does not exclude or prune the factual answer chunk."""
    repo = CloudRepository()
    b_repo = get_workspace_boundary_repo()
    okf_repo = get_okf_repo()

    ws_id = "ws_counterexample_okf"
    doc_id = "doc_exec_report"
    doc_ver = "v1"

    # 1. Boundary snapshot
    snapshot = DocumentSnapshot(
        workspace_id=ws_id,
        document_id=doc_id,
        source=f"{doc_id}.pdf",
        version=doc_ver,
        format="pdf",
        created_at="2026-10-26T00:00:00Z",
        persisted_rows=2,
        total_rows=2,
        okf_status="ready",
        okf_entities_count=1,
        okf_properties_count=1,
    )
    b_repo.create_workspace(ws_id, owner_principal_id="test_user", workspace_id=ws_id)
    b_repo.create_source(ws_id, f"{doc_id}.pdf", source_id=doc_id)
    b_repo.save_document_snapshot(snapshot)

    # 2. OKF Manifest with officer on Page 2
    ent = OkfEntityRecord(
        workspace_id=ws_id,
        entity_id=f"e_{doc_id}_officer",
        entity_name="Alice Smith",
        normalized_name="alice smith",
        entity_type="person",
        document_id=doc_id,
        document_version=doc_ver,
        created_at="2026-10-26T00:00:00Z",
    )
    ass = OkfPropertyAssertion(
        assertion_id=f"ass_{doc_id}_role",
        workspace_id=ws_id,
        entity_id=ent.entity_id,
        entity_name="Alice Smith",
        property_key="title",
        property_value="Chief Operating Officer",
        value_type="string",
        source_chunk_id=f"{doc_id}:c_p2",
        document_id=doc_id,
        document_version=doc_ver,
        page_number=2,
        created_at="2026-10-26T00:00:00Z",
    )
    okf_repo.save_manifest(
        ws_id,
        OkfManifest(
            workspace_id=ws_id,
            document_id=doc_id,
            document_version=doc_ver,
            entities=(ent,),
            assertions=(ass,),
            status="ready",
            idempotency_key="k_counter",
            created_at="2026-10-26T00:00:00Z",
        ),
    )

    # 3. Chunks: Page 2 (officer) and Page 18 (factual answer about server migration)
    chunk_p2 = _make_chunk(
        chunk_id=f"{doc_id}:c_p2",
        doc_id=doc_id,
        workspace_id=ws_id,
        text="Alice Smith serves as Chief Operating Officer of the enterprise.",
        page_number=2,
        element_type="paragraph",
    )
    chunk_p18 = _make_chunk(
        chunk_id=f"{doc_id}:c_p18",
        doc_id=doc_id,
        workspace_id=ws_id,
        text="Under the enterprise cloud migration project, 450 bare metal servers were migrated to Kubernetes.",
        page_number=18,
        element_type="paragraph",
    )

    doc = Document(
        id=doc_id,
        filename=f"{doc_id}.pdf",
        source_format="pdf",
        chunks=(chunk_p2, chunk_p18),
        workspace_id=ws_id,
    )
    repo.save(doc)

    # 4. Query mentioning Alice Smith but asking about server migration on page 18
    query = "Under Chief Operating Officer Alice Smith, how many servers were migrated to Kubernetes?"
    response = pipeline.run(query=query, workspace_id=ws_id, force_full_path=True)

    # Assertions for AC-9:
    # A. OKF candidate from Page 2 was retrieved
    assert f"{doc_id}:c_p2" in response.okf_candidate_ids
    assert response.okf_match_count >= 1

    # B. Vector retrieval searched unindexed text and found Page 18
    vector_ids = response.retrieval_candidates.get("vector", [])
    assert f"{doc_id}:c_p18" in vector_ids

    # C. Zero hard exclusion: RRF retained both Page 2 and Page 18 candidates
    rrf_ids = response.retrieval_candidates.get("rrf", [])
    assert f"{doc_id}:c_p2" in rrf_ids
    assert f"{doc_id}:c_p18" in rrf_ids

    # D. Final answer cited Page 18 correctly
    cit_pages = [
        c.get("location", {}).get("page") or c.get("page_number")
        for c in response.citations
    ]
    assert 18 in cit_pages, f"Expected citation on Page 18, got {cit_pages}"


# ===========================================================================
# 3. Ambiguity & Conflict Weight Division (AC-4, AC-5)
# ===========================================================================

def test_ambiguity_and_conflict_weight_division_ac4_ac5():
    """Verify that ambiguous entity names or conflicting properties receive divided weight (0.575)."""
    okf_repo = get_okf_repo()
    b_repo = get_workspace_boundary_repo()

    ws_id = "ws_ambig_test"
    now = "2026-10-26T00:00:00Z"
    b_repo.create_workspace(ws_id, owner_principal_id="test", workspace_id=ws_id)

    # 1. Ambiguous entity: John Smith in Doc A and Doc B
    e_a = OkfEntityRecord(
        workspace_id=ws_id, entity_id="e_a", entity_name="John Smith",
        normalized_name="john smith", entity_type="person", document_id="doc_a",
        document_version="v1", created_at=now,
    )
    e_b = OkfEntityRecord(
        workspace_id=ws_id, entity_id="e_b", entity_name="John Smith",
        normalized_name="john smith", entity_type="person", document_id="doc_b",
        document_version="v1", created_at=now,
    )
    ass_a = OkfPropertyAssertion(
        assertion_id="ass_a", workspace_id=ws_id, entity_id="e_a",
        entity_name="John Smith", property_key="role", property_value="Developer",
        value_type="string", source_chunk_id="c_a", document_id="doc_a",
        document_version="v1", created_at=now,
    )
    ass_b = OkfPropertyAssertion(
        assertion_id="ass_b", workspace_id=ws_id, entity_id="e_b",
        entity_name="John Smith", property_key="role", property_value="Manager",
        value_type="string", source_chunk_id="c_b", document_id="doc_b",
        document_version="v1", created_at=now,
    )

    okf_repo.save_manifest(ws_id, OkfManifest(
        workspace_id=ws_id, document_id="doc_a", document_version="v1",
        entities=(e_a,), assertions=(ass_a,), idempotency_key="k_a", created_at=now,
    ))
    okf_repo.save_manifest(ws_id, OkfManifest(
        workspace_id=ws_id, document_id="doc_b", document_version="v1",
        entities=(e_b,), assertions=(ass_b,), idempotency_key="k_b", created_at=now,
    ))

    # Mock chunk repo returning hydrated chunks
    c_a = _make_chunk("c_a", "doc_a", ws_id, "John Smith is Developer", 1)
    c_b = _make_chunk("c_b", "doc_b", ws_id, "John Smith is Manager", 2)
    mock_chunk_repo = MagicMock()
    mock_chunk_repo.get_all_chunks.return_value = [c_a, c_b]

    with patch("services.langgraph_pipeline._get_repo", return_value=mock_chunk_repo):
        state = {
            "query": "What is John Smith's role?",
            "workspace_id": ws_id,
            "stage_timings": {},
        }
        res = run_okf_router(state)

        # Ambiguous entity state & conflicting property state
        assert res["okf_entity_state"] == "ambiguous"
        assert res["okf_property_state"] == "conflicting"
        assert len(res["okf_candidates"]) == 2

        # Check divided weight applied to chunks: 1.15 / 2 = 0.575
        for chunk in res["okf_candidates"]:
            assert getattr(chunk, "okf_weight") == pytest.approx(0.575, rel=1e-3)


# ===========================================================================
# 4. Fast-Path Query Bypass (AC-9)
# ===========================================================================

def test_fast_path_bypass_okf_ac9():
    """Verify queries executing on fast path bypass OKF lookups completely (0 tokens, 0ms)."""
    from services.retrieval.planner import Plan

    fast_plan = Plan(fast_path=True, use_graph=False, stages=["vector", "extraction"])
    state = {
        "query": "What is the capital of France?",
        "workspace_id": "ws_fast",
        "plan": fast_plan,
        "stage_timings": {},
    }

    res = run_okf_router(state)
    assert res["okf_candidates"] == []
    assert res["okf_candidate_ids"] == []
    assert res["okf_match_count"] == 0
    assert res["okf_latency_ms"] == 0.0
    assert res["okf_execution_status"] == "fast_path_bypassed"


# ===========================================================================
# 5. Removal of Legacy pre-BM25 OKF_BOOST in run_bm25 (AC-9)
# ===========================================================================

def test_removal_of_bm25_okf_boost_ac9():
    """Verify that run_bm25 no longer applies OKF_BOOST to candidate scores."""
    c1 = _make_chunk("c1", "doc1", "ws_bm25", "Python programming language overview", 1)
    c2 = _make_chunk("c2", "doc1", "ws_bm25", "Python syntax and semantics guide", 2)

    mock_repo = MagicMock()
    mock_repo.get_all_chunks.return_value = [c1, c2]

    # Pre-populate okf_seed_chunk_ids with c2
    state = {
        "query": "Python programming",
        "workspace_id": "ws_bm25",
        "okf_seed_chunk_ids": ["c2"],
        "stage_timings": {},
    }

    with patch("services.langgraph_pipeline._get_repo", return_value=mock_repo):
        res = run_bm25(state)
        # Scores are pure BM25 without OKF_BOOST multiplication
        # c1 has higher term match for "Python programming" than c2
        assert res["bm25_candidates"][0].id == "c1"


# ===========================================================================
# 6. Candidate Telemetry Exposure (AC-11)
# ===========================================================================

def test_candidate_telemetry_exposure_ac11():
    """Verify OKF candidate metadata is surfaced in retrieval_candidates trace."""
    okf_repo = get_okf_repo()
    b_repo = get_workspace_boundary_repo()
    repo = CloudRepository()

    ws_id = "ws_telemetry"
    doc_id = "doc_telem"
    now = "2026-10-26T00:00:00Z"

    b_repo.create_workspace(ws_id, owner_principal_id="test", workspace_id=ws_id)
    b_repo.create_source(ws_id, "doc.pdf", source_id=doc_id)
    b_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws_id, document_id=doc_id, source="doc.pdf", version="v1",
        format="pdf", created_at=now, persisted_rows=1, total_rows=1,
    ))

    ent = OkfEntityRecord(
        workspace_id=ws_id, entity_id="e_telem", entity_name="Solaris Corp",
        normalized_name="solaris corp", entity_type="organization", document_id=doc_id,
        document_version="v1", created_at=now,
    )
    ass = OkfPropertyAssertion(
        assertion_id="ass_telem", workspace_id=ws_id, entity_id="e_telem",
        entity_name="Solaris Corp", property_key="headquarters", property_value="Zurich",
        value_type="string", source_chunk_id="c_telem", document_id=doc_id,
        document_version="v1", page_number=1, created_at=now,
    )
    okf_repo.save_manifest(ws_id, OkfManifest(
        workspace_id=ws_id, document_id=doc_id, document_version="v1",
        entities=(ent,), assertions=(ass,), idempotency_key="k_telem", created_at=now,
    ))

    chunk = _make_chunk("c_telem", doc_id, ws_id, "Solaris Corp headquarters is in Zurich.", 1)
    doc = Document(id=doc_id, filename="doc.pdf", source_format="pdf", chunks=(chunk,), workspace_id=ws_id)
    repo.save(doc)

    response = pipeline.run(query="Where is Solaris Corp located?", workspace_id=ws_id, force_full_path=True)

    # Check top-level properties and retrieval_candidates trace
    assert response.okf_match_count >= 1
    assert "c_telem" in response.okf_candidate_ids
    assert response.okf_entity_state == "unique"
    assert response.okf_property_state == "unique"
    assert "okf" in response.retrieval_candidates
    assert "okf_candidate_ids" in response.retrieval_candidates
    assert "c_telem" in response.retrieval_candidates["okf"]


# ===========================================================================
# 7. Offline Ablation Comparison Suite (AC-12)
# ===========================================================================

def test_offline_ablation_suite_recall_precision_latency_ac12(monkeypatch):
    """Run ablation comparison measuring candidate recall@5, precision@3, and latency
    with OKF enabled vs disabled.
    """
    repo = CloudRepository()
    b_repo = get_workspace_boundary_repo()
    okf_repo = get_okf_repo()

    ws_id = "ws_ablation_okf"
    doc_id = "doc_eval"
    now = "2026-10-26T00:00:00Z"

    b_repo.create_workspace(ws_id, owner_principal_id="test", workspace_id=ws_id)
    b_repo.create_source(ws_id, "eval.pdf", source_id=doc_id)
    b_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws_id, document_id=doc_id, source="eval.pdf", version="v1",
        format="pdf", created_at=now, persisted_rows=4, total_rows=4,
    ))

    # Entity on page 3
    ent = OkfEntityRecord(
        workspace_id=ws_id, entity_id="e_eval", entity_name="Quantum Dynamics",
        normalized_name="quantum dynamics", entity_type="organization", document_id=doc_id,
        document_version="v1", created_at=now,
    )
    ass = OkfPropertyAssertion(
        assertion_id="ass_eval", workspace_id=ws_id, entity_id="e_eval",
        entity_name="Quantum Dynamics", property_key="revenue", property_value="$85 million",
        value_type="string", source_chunk_id="c_p3", document_id=doc_id,
        document_version="v1", page_number=3, created_at=now,
    )
    okf_repo.save_manifest(ws_id, OkfManifest(
        workspace_id=ws_id, document_id=doc_id, document_version="v1",
        entities=(ent,), assertions=(ass,), idempotency_key="k_eval", created_at=now,
    ))

    c1 = _make_chunk("c_p1", doc_id, ws_id, "Introduction to quantum dynamics and general physics.", 1)
    c2 = _make_chunk("c_p2", doc_id, ws_id, "Market competition and industry background overview.", 2)
    c3 = _make_chunk("c_p3", doc_id, ws_id, "Quantum Dynamics reported total annual revenue of $85 million.", 3)
    c4 = _make_chunk("c_p4", doc_id, ws_id, "Risk factors and regulatory uncertainties forward looking.", 4)
    doc = Document(id=doc_id, filename="eval.pdf", source_format="pdf", chunks=(c1, c2, c3, c4), workspace_id=ws_id)
    repo.save(doc)

    query = "What was the annual revenue for Quantum Dynamics?"

    # Warmup pipeline run to eliminate first-time module compilation jitter
    pipeline.run(query=query, workspace_id=ws_id, force_full_path=True)

    # Run with OKF enabled
    monkeypatch.setattr(settings, "OKF_ENABLED", True)
    t0 = time.perf_counter()
    res_enabled = pipeline.run(query=query, workspace_id=ws_id, force_full_path=True)
    lat_enabled = (time.perf_counter() - t0) * 1000.0

    # Run with OKF disabled
    monkeypatch.setattr(settings, "OKF_ENABLED", False)
    t0 = time.perf_counter()
    res_disabled = pipeline.run(query=query, workspace_id=ws_id, force_full_path=True)
    lat_disabled = (time.perf_counter() - t0) * 1000.0

    # Compare Recall@5: Target chunk c_p3 should be in RRF for both
    rrf_enabled = res_enabled.retrieval_candidates.get("rrf", [])
    rrf_disabled = res_disabled.retrieval_candidates.get("rrf", [])
    assert "c_p3" in rrf_enabled
    assert "c_p3" in rrf_disabled

    # Compare Precision@3: OKF boost elevates c_p3 to top ranks
    top3_enabled = rrf_enabled[:3]
    assert "c_p3" in top3_enabled

    # Latency budget: OKF overhead must remain within 50ms deadline (AC-8, AC-9)
    assert res_enabled.okf_latency_ms <= 50.0
    assert res_disabled.okf_latency_ms == 0.0
    assert res_enabled.stage_timings.get("okf_router_ms", 0.0) <= 50.0


# ===========================================================================
# 8. Benchmark Comparison Barrier Enforcement (AC-12)
# ===========================================================================

def test_benchmark_comparison_barrier_enforcement_ac12():
    """Verify validate_benchmark_comparison_preconditions blocks accuracy claims on non-canonical workspaces."""
    # Permitted canonical comparison workspace
    assert validate_benchmark_comparison_preconditions(
        baseline_run_id="run_base_001",
        candidate_run_id="run_cand_001",
        comparison_workspace_id="ws_canonical_comparison_0004",
    ) is True

    # Blocked on arbitrary or uncanonical workspace
    with pytest.raises(ValueError, match="Benchmark comparison claims are blocked"):
        validate_benchmark_comparison_preconditions(
            baseline_run_id="run_base_001",
            candidate_run_id="run_cand_001",
            comparison_workspace_id="ws_my_test_workspace",
        )


# ===========================================================================
# 9. Pipeline Tenant Boundary Isolation and Cross-Tenant Immunity (AC-6, AC-11)
# ===========================================================================

def test_query_pipeline_tenant_isolation_ac6_ac11():
    """Verify cross-tenant pipeline execution never leaks OKF candidates between workspaces."""
    repo = CloudRepository()
    b_repo = get_workspace_boundary_repo()
    okf_repo = get_okf_repo()

    ws_a = "ws_tenant_alpha"
    ws_b = "ws_tenant_beta"
    doc_a = "doc_alpha_confidential"
    now = "2026-10-26T00:00:00Z"

    # Workspace A setup with confidential OKF entity
    b_repo.create_workspace(ws_a, owner_principal_id="alice", workspace_id=ws_a)
    b_repo.create_source(ws_a, "confidential.pdf", source_id=doc_a)
    b_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws_a, document_id=doc_a, source="confidential.pdf", version="v1",
        format="pdf", created_at=now, persisted_rows=1, total_rows=1,
    ))
    ent = OkfEntityRecord(
        workspace_id=ws_a, entity_id="e_alpha", entity_name="Project Excalibur",
        normalized_name="project excalibur", entity_type="concept", document_id=doc_a,
        document_version="v1", created_at=now,
    )
    ass = OkfPropertyAssertion(
        assertion_id="ass_alpha_budget", workspace_id=ws_a, entity_id="e_alpha",
        entity_name="Project Excalibur", property_key="budget", property_value="$500M",
        value_type="string", source_chunk_id="c_alpha_1", document_id=doc_a,
        document_version="v1", page_number=1, created_at=now,
    )
    okf_repo.save_manifest(ws_a, OkfManifest(
        workspace_id=ws_a, document_id=doc_a, document_version="v1",
        entities=(ent,), assertions=(ass,), idempotency_key="k_alpha", created_at=now,
    ))
    chunk_a = _make_chunk("c_alpha_1", doc_a, ws_a, "Project Excalibur budget is $500M.", 1)
    repo.save(Document(id=doc_a, filename="confidential.pdf", source_format="pdf", chunks=(chunk_a,), workspace_id=ws_a))

    # Workspace B setup without this entity
    b_repo.create_workspace(ws_b, owner_principal_id="bob", workspace_id=ws_b)

    # Bob queries his own workspace for Project Excalibur
    res_b = pipeline.run(query="What is the budget for Project Excalibur?", workspace_id=ws_b, force_full_path=True)

    # Must have 0 cross-tenant leakage
    assert res_b.okf_candidate_ids == []
    assert res_b.okf_match_count == 0
    assert res_b.okf_entity_state == "none"
    assert "c_alpha_1" not in res_b.retrieval_candidates.get("okf", [])
    assert "c_alpha_1" not in res_b.retrieval_candidates.get("rrf", [])


# ===========================================================================
# 10. Deterministic Entity Extraction Edge Cases (AC-1, AC-8)
# ===========================================================================

def test_deterministic_entity_extraction_edge_cases_ac1_ac8():
    """Verify deterministic extraction handles empty strings, stop words, quotes, and multi-word prefixes."""
    # Empty query
    assert extract_entity_names_from_query("") == []

    # Stop words only
    assert extract_entity_names_from_query("what is the in of for") == []

    # Quoted entities
    quoted = extract_entity_names_from_query('Tell me about "Apollo 11" mission details')
    assert "Apollo 11" in quoted

    # Multi-word officer prefix stripping
    officer_query = "Under Chief Executive Officer John Doe, what was the revenue?"
    extracted = extract_entity_names_from_query(officer_query)
    assert "John Doe" in extracted


# ===========================================================================
# 11. Router Timeout Fallback to Baseline (AC-8, AC-9)
# ===========================================================================

def test_okf_router_timeout_fallback_to_baseline_ac8():
    """Verify that a storage timeout in OKF lookup gracefully falls back without breaking pipeline."""
    with patch("services.retrieval.okf_retriever.OkfRetriever.lookup_sync") as mock_lookup:
        mock_lookup.return_value = OkfLookupResult(
            target_entity="Acme Corp",
            entity_state="none",
            property_state="none",
            execution_status="caller_timeout",
            latency_ms=50.1,
        )

        state = {
            "query": "What is the revenue for Acme Corp?",
            "workspace_id": "ws_test_timeout",
            "plan": Plan(fast_path=False, use_graph=False, stages=["okf", "bm25", "vector"]),
            "stage_timings": {},
        }
        res = run_okf_router(state)

        assert res["okf_candidates"] == []
        assert res["okf_candidate_ids"] == []
        assert res["okf_match_count"] == 0
        assert res["okf_execution_status"] == "caller_timeout"
        assert res["stage_timings"]["okf_router_ms"] >= 0.0
