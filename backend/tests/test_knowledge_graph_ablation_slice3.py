"""Offline ablation test suite and regression checks for Spec 0007 Slice 3:
Bounded Knowledge Graph Enrichment - Pipeline Fusion, Telemetry, and Offline Ablation.

Verifies:
1. Reciprocal rank fusion soft pooling with Knowledge Graph boost (k=60, graph_weight=1.10)
   without hard page exclusions or pruning baseline chunks (AC-10).
2. Counterexample discovery preservation: query matching KG relation on Page 2 preserves
   factual answer chunk on Page 18 in RRF and cites Page 18 correctly (AC-10).
3. Ambiguity & conflict candidate pooling: ambiguous entity names and conflicting relationships
   pooled with divided weight (0.55) (AC-10).
4. Fast-path query bypass: queries executing fast path skip Knowledge Graph lookups completely (AC-10).
5. Candidate telemetry exposure in retrieval_candidates trace: graph_candidate_ids,
   graph_match_count, graph_latency_ms, graph_traversed_nodes, graph_matched_edges,
   graph_was_truncated, graph_truncation_reason, graph_entity_state, graph_relationship_state (AC-12).
6. Offline ablation comparison: candidate page recall@5, precision@3, and latency
   with Knowledge Graph enabled versus disabled against baseline (AC-13).
7. Benchmark comparison precondition enforcement: blocking accuracy claims on non-canonical
   workspaces (AC-13).
8. Pipeline tenant boundary isolation and cross-tenant immunity (AC-6, AC-11).
9. Router timeout fallback to baseline: non-blocking graceful fallback within deadline budget (AC-8, AC-10).
10. Decoupled baseline ingestion and snapshot metadata: ADR-001 compliance (AC-3, AC-11, AC-12).
11. KnowledgeGraphStrategy direct retrieval contract and fast path bypass (AC-11).
12. Dynamic tombstone fail-closed enforcement during pipeline graph traversal (AC-7).
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
from services.retrieval.subgoal_decomposer import QueryPlan
from services.langgraph_pipeline import (
    _rrf_merge,
    pipeline,
    run_graph,
)
from db.knowledge_graph_repo import (
    InMemoryKnowledgeGraphRepository,
    get_knowledge_graph_repository,
    get_shared_knowledge_graph_repository,
)
from db.workspace_boundary_repo import get_workspace_boundary_repo
from schemas.contracts.boundary import DocumentSnapshot
from schemas.contracts.knowledge_graph import (
    KnowledgeGraphEntity,
    KnowledgeGraphLookupResult,
    KnowledgeGraphManifest,
    KnowledgeGraphRelationship,
    generate_publication_id,
)
from schemas.contracts.location import DocumentLocation
from services.retrieval.strategies.knowledge_graph import KnowledgeGraphStrategy


@pytest.fixture(autouse=True)
def clean_test_environment(monkeypatch):
    """Ensure in-memory repositories and test environment settings."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "test")
    monkeypatch.setattr(settings, "GRAPH_ENABLED", True)
    monkeypatch.setattr(settings, "GRAPH_FUSION_WEIGHT", 1.10)
    monkeypatch.setattr(settings, "GRAPH_AMBIGUOUS_FUSION_WEIGHT", 0.55)
    monkeypatch.setattr(settings, "GRAPH_TIMEOUT_MS", 75)
    monkeypatch.setattr(settings, "VECTOR_THRESHOLD", 0.0)
    b_repo = get_workspace_boundary_repo(reset=True)
    kg_repo = get_shared_knowledge_graph_repository(reset=True)
    store = get_shared_table_store(reset=True)
    _IN_MEMORY_DOCS.clear()
    _IN_MEMORY_CHUNKS.clear()
    yield b_repo, kg_repo, store
    get_workspace_boundary_repo(reset=True)
    get_shared_knowledge_graph_repository(reset=True)
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
    graph_weight: float | None = None,
    document_version: str = "v1",
    metadata: dict | None = None,
) -> Chunk:
    meta = dict(metadata) if metadata is not None else {}
    meta["document_version"] = document_version
    meta["version"] = document_version
    return Chunk(
        id=chunk_id,
        document_id=doc_id,
        workspace_id=workspace_id,
        text=text,
        source_format="pdf",
        page_number=page_number,
        element_type=element_type,
        graph_weight=graph_weight,
        metadata=meta,
        embedding_fast=embed_fast_local(text),
        embedding_full=_deterministic_vector(text, 1024),
    )


# ===========================================================================
# 1. Reciprocal Rank Fusion Soft Pooling (AC-10)
# ===========================================================================

def test_rrf_soft_pooling_with_kg_weight():
    """Verify RRF pools KG candidates with calibrated weight (1.10) without pruning baseline chunks."""
    bm25_c1 = _make_chunk("c_b1", "doc1", "ws1", "Operating revenue in 2024", 1)
    bm25_c2 = _make_chunk("c_b2", "doc1", "ws1", "Operating expenses in 2024", 2)
    vec_c1 = _make_chunk("c_v1", "doc1", "ws1", "Long term debt obligations", 3)
    vec_c2 = _make_chunk("c_v2", "doc1", "ws1", "Cash and cash equivalents", 4)
    kg_c1 = _make_chunk("c_g1", "doc1", "ws1", "Acme Corp acquired Beta Industries", 5, graph_weight=1.10)

    merged = _rrf_merge(
        bm25_chunks=[bm25_c1, bm25_c2],
        vector_chunks=[vec_c1, vec_c2],
        graph_chunks=[kg_c1],
        k=60,
        graph_weight=1.10,
    )

    merged_ids = [c.id for c in merged]
    # 1. Zero pruning: all 5 candidate chunks preserved in merged pool
    assert len(merged) == 5
    assert set(merged_ids) == {"c_b1", "c_b2", "c_v1", "c_v2", "c_g1"}

    # 2. KG candidate receives soft boost score: 1.10 / (60 + 0 + 1) = 1.10 / 61 ≈ 0.01803
    # BM25 rank 0: 1.0 / 61 ≈ 0.01639. Therefore kg_c1 outranks single-source BM25 rank 0.
    assert merged[0].id == "c_g1"

    # 3. Divided ambiguity / conflict weight test: with graph_weight=0.55, score is 0.55 / 61 ≈ 0.00902
    kg_ambig = _make_chunk("c_g_ambig", "doc1", "ws1", "Ambiguous subsidiary info", 5, graph_weight=0.55)
    merged_ambig = _rrf_merge(
        bm25_chunks=[bm25_c1],
        vector_chunks=[vec_c1],
        graph_chunks=[kg_ambig],
        k=60,
    )
    # Both BM25 and Vector rank 0 score 1.0 / 61; ambig scores 0.55 / 61, so ambig is ranked after
    assert merged_ambig[-1].id == "c_g_ambig"

    # 4. Empty KG candidates leaves baseline unaffected
    baseline_only = _rrf_merge(
        bm25_chunks=[bm25_c1, bm25_c2],
        vector_chunks=[vec_c1, vec_c2],
        graph_chunks=None,
    )
    assert len(baseline_only) == 4
    assert set(c.id for c in baseline_only) == {"c_b1", "c_b2", "c_v1", "c_v2"}


# ===========================================================================
# 2. Counterexample Discovery Preservation (AC-10)
# ===========================================================================

def test_counterexample_discovery_preservation_ac10():
    """Query mentions corporate acquisition (on Page 2) while answer fact resides on Page 18.
    Proves that matching a KG relationship does not exclude or prune the factual answer chunk."""
    repo = CloudRepository()
    b_repo = get_workspace_boundary_repo()
    kg_repo = get_shared_knowledge_graph_repository()

    ws_id = "ws_counterexample_kg"
    doc_id = "doc_corp_report"
    doc_ver = "v1"
    now_str = "2026-11-17T00:00:00Z"

    # 1. Boundary snapshot
    snapshot = DocumentSnapshot(
        workspace_id=ws_id,
        document_id=doc_id,
        source=f"{doc_id}.pdf",
        version=doc_ver,
        format="pdf",
        created_at=now_str,
        persisted_rows=2,
        total_rows=2,
        graph_status="ready",
        graph_entities_count=2,
        graph_relations_count=1,
    )
    b_repo.create_workspace(ws_id, owner_principal_id="test_user", workspace_id=ws_id)
    b_repo.create_source(ws_id, f"{doc_id}.pdf", source_id=doc_id)
    b_repo.save_document_snapshot(snapshot)

    # 2. KG Manifest with acquisition on Page 2
    pub_id = generate_publication_id()
    ent_source = KnowledgeGraphEntity(
        entity_id=f"e_{doc_id}_acme",
        workspace_id=ws_id,
        name="Acme Corp",
        canonical_name="acme_corp",
        entity_type="organization",
        source_document_id=doc_id,
    )
    ent_target = KnowledgeGraphEntity(
        entity_id=f"e_{doc_id}_beta",
        workspace_id=ws_id,
        name="Beta Industries",
        canonical_name="beta_industries",
        entity_type="organization",
        source_document_id=doc_id,
    )
    rel = KnowledgeGraphRelationship(
        relationship_id=f"rel_{doc_id}_acq",
        workspace_id=ws_id,
        source_entity_id=ent_source.entity_id,
        source_entity_name=ent_source.name,
        predicate="ACQUIRED",
        canonical_predicate="ACQUIRED",
        target_entity_id=ent_target.entity_id,
        target_entity_name=ent_target.name,
        confidence=1.0,
        source_document_id=doc_id,
        source_document_version=doc_ver,
        publication_id=pub_id,
        source_chunk_ids=(f"{doc_id}:c_p2",),
        location=DocumentLocation(page=2),
        verbatim_evidence="Acme Corp acquired Beta Industries.",
        created_at_utc=now_str,
    )

    kg_repo.stage_relationships(
        ws_id, doc_id, doc_ver, pub_id,
        [ent_source, ent_target],
        [rel],
    )
    kg_repo.commit_manifest(
        ws_id,
        KnowledgeGraphManifest(
            workspace_id=ws_id,
            document_id=doc_id,
            document_version=doc_ver,
            publication_id=pub_id,
            status="ready",
            entity_count=2,
            relationship_count=1,
            ingested_at_utc=now_str,
            content_hash="hash_counter",
        ),
    )

    # 3. Chunks: Page 2 (acquisition) and Page 18 (factual answer about server migration)
    chunk_p2 = _make_chunk(
        chunk_id=f"{doc_id}:c_p2",
        doc_id=doc_id,
        workspace_id=ws_id,
        text="Acme Corp completed the acquisition of Beta Industries in the fourth quarter.",
        page_number=2,
        element_type="paragraph",
        document_version=doc_ver,
    )
    chunk_p18 = _make_chunk(
        chunk_id=f"{doc_id}:c_p18",
        doc_id=doc_id,
        workspace_id=ws_id,
        text="Under the cloud migration project, 350 database instances were migrated to Aurora.",
        page_number=18,
        element_type="paragraph",
        document_version=doc_ver,
    )

    doc = Document(
        id=doc_id,
        filename=f"{doc_id}.pdf",
        source_format="pdf",
        chunks=(chunk_p2, chunk_p18),
        workspace_id=ws_id,
    )
    repo.save(doc)

    # 4. Query mentioning Acme Corp and Beta Industries but asking about migration on page 18
    query = "Under the acquisition of Beta Industries by Acme Corp, how many database instances were migrated to Aurora?"
    response = pipeline.run(query=query, workspace_id=ws_id, force_full_path=True)

    # Assertions for AC-10:
    # A. KG candidate from Page 2 was retrieved
    assert f"{doc_id}:c_p2" in response.graph_candidate_ids
    assert response.graph_match_count >= 1

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
# 3. Ambiguity & Conflict Weight Division (AC-10)
# ===========================================================================

def test_ambiguity_and_conflict_weight_division_ac10():
    """Verify that ambiguous entity names or conflicting relationships receive divided weight (0.55)."""
    ws_id = "ws_ambig_graph_test"
    doc_id = "doc_ambig"
    doc_ver = "v1"

    c_ambig = _make_chunk("c_ambig_1", doc_id, ws_id, "Ambiguous entity chunk", 1)

    # Construct lookup result with ambiguous entity state
    ambig_lookup = KnowledgeGraphLookupResult(
        seed_entities=("Omega Corp",),
        entity_state="ambiguous",
        relationship_state="unique",
        execution_status="completed",
        candidate_chunks=(c_ambig,),
        matched_chunk_ids=("c_ambig_1",),
        traversed_node_count=2,
        edge_match_count=1,
    )

    with patch("services.retrieval.knowledge_graph_retriever.KnowledgeGraphRetriever.expand", return_value=ambig_lookup):
        state = {
            "query": "Where is Omega Corp located?",
            "workspace_id": ws_id,
            "stage_timings": {},
        }
        res = run_graph(state)

        assert res["graph_entity_state"] == "ambiguous"
        assert len(res["graph_chunks"]) == 1
        assert getattr(res["graph_chunks"][0], "graph_weight") == pytest.approx(0.55, rel=1e-3)

    # Construct lookup result with conflicting relationship state
    conflict_lookup = KnowledgeGraphLookupResult(
        seed_entities=("Omega Corp",),
        entity_state="unique",
        relationship_state="conflicting",
        execution_status="completed",
        candidate_chunks=(c_ambig,),
        matched_chunk_ids=("c_ambig_1",),
        traversed_node_count=2,
        edge_match_count=2,
    )

    with patch("services.retrieval.knowledge_graph_retriever.KnowledgeGraphRetriever.expand", return_value=conflict_lookup):
        state = {
            "query": "Who is the parent company of Omega Corp?",
            "workspace_id": ws_id,
            "stage_timings": {},
        }
        res = run_graph(state)

        assert res["graph_relationship_state"] == "conflicting"
        assert len(res["graph_chunks"]) == 1
        assert getattr(res["graph_chunks"][0], "graph_weight") == pytest.approx(0.55, rel=1e-3)


# ===========================================================================
# 4. Fast-Path Query Bypass (AC-10)
# ===========================================================================

def test_fast_path_bypass_graph_ac10():
    """Verify queries executing on fast path bypass Knowledge Graph lookups completely (0 tokens, 0ms)."""
    fast_plan = Plan(fast_path=True, use_graph=False, stages=["vector", "extraction"])
    state = {
        "query": "What was the total revenue?",
        "workspace_id": "ws_fast_kg",
        "plan": fast_plan,
        "stage_timings": {},
    }

    res = run_graph(state)
    assert res["graph_chunks"] == []
    assert res["graph_candidate_ids"] == []
    assert res["graph_match_count"] == 0
    assert res["graph_latency_ms"] == 0.0
    assert res["graph_execution_status"] == "fast_path_bypassed"


# ===========================================================================
# 5. Candidate Telemetry Exposure (AC-12)
# ===========================================================================

def test_candidate_telemetry_exposure_ac12():
    """Verify Knowledge Graph candidate metadata is surfaced in retrieval_candidates trace."""
    kg_repo = get_shared_knowledge_graph_repository()
    b_repo = get_workspace_boundary_repo()
    repo = CloudRepository()

    ws_id = "ws_telemetry_kg"
    doc_id = "doc_telem_kg"
    doc_ver = "v1"
    now = "2026-11-17T00:00:00Z"
    pub_id = generate_publication_id()

    b_repo.create_workspace(ws_id, owner_principal_id="test", workspace_id=ws_id)
    b_repo.create_source(ws_id, "doc.pdf", source_id=doc_id)
    b_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws_id, document_id=doc_id, source="doc.pdf", version=doc_ver,
        format="pdf", created_at=now, persisted_rows=1, total_rows=1,
        graph_status="ready", graph_entities_count=2, graph_relations_count=1,
    ))

    ent_src = KnowledgeGraphEntity(
        entity_id="e_telem_src", workspace_id=ws_id, name="Solaris Corp",
        canonical_name="solaris_corp", entity_type="organization", source_document_id=doc_id,
    )
    ent_tgt = KnowledgeGraphEntity(
        entity_id="e_telem_tgt", workspace_id=ws_id, name="Helios Ltd",
        canonical_name="helios_ltd", entity_type="organization", source_document_id=doc_id,
    )
    rel = KnowledgeGraphRelationship(
        relationship_id="rel_telem_1", workspace_id=ws_id, source_entity_id=ent_src.entity_id,
        source_entity_name=ent_src.name, predicate="SUBSIDIARY_OF", canonical_predicate="SUBSIDIARY_OF",
        target_entity_id=ent_tgt.entity_id, target_entity_name=ent_tgt.name,
        confidence=1.0, source_document_id=doc_id, source_document_version=doc_ver,
        publication_id=pub_id, source_chunk_ids=("c_telem_kg",),
        location=DocumentLocation(page=1), verbatim_evidence="Solaris Corp is a subsidiary of Helios Ltd.",
        created_at_utc=now,
    )

    kg_repo.stage_relationships(ws_id, doc_id, doc_ver, pub_id, [ent_src, ent_tgt], [rel])
    kg_repo.commit_manifest(ws_id, KnowledgeGraphManifest(
        workspace_id=ws_id, document_id=doc_id, document_version=doc_ver, publication_id=pub_id,
        status="ready", entity_count=2, relationship_count=1, ingested_at_utc=now, content_hash="k_telem",
    ))

    chunk = _make_chunk("c_telem_kg", doc_id, ws_id, "Solaris Corp is a subsidiary of Helios Ltd.", 1, document_version=doc_ver)
    doc = Document(id=doc_id, filename="doc.pdf", source_format="pdf", chunks=(chunk,), workspace_id=ws_id)
    repo.save(doc)

    response = pipeline.run(query="Is Solaris Corp a subsidiary of Helios Ltd?", workspace_id=ws_id, force_full_path=True)

    # Check top-level properties and retrieval_candidates trace
    assert response.graph_match_count >= 1
    assert "c_telem_kg" in response.graph_candidate_ids
    assert response.graph_traversed_nodes >= 1
    assert response.graph_matched_edges >= 1
    assert response.graph_was_truncated is False
    assert response.graph_truncation_reason is None
    assert response.graph_entity_state in ("unique", "ambiguous")
    assert response.graph_relationship_state in ("unique", "none")

    # Trace telemetry exposure
    cands = response.retrieval_candidates
    assert "graph" in cands
    assert "graph_candidate_ids" in cands
    assert "c_telem_kg" in cands["graph"]
    assert "graph_latency_ms" in cands
    assert "graph_traversed_nodes" in cands
    assert "graph_matched_edges" in cands
    assert "graph_was_truncated" in cands


# ===========================================================================
# 6. Offline Ablation Comparison Suite (AC-13)
# ===========================================================================

def test_offline_ablation_suite_recall_precision_latency_ac13(monkeypatch):
    """Run ablation comparison measuring candidate recall@5, precision@3, and latency
    with Knowledge Graph enabled vs disabled.
    """
    repo = CloudRepository()
    b_repo = get_workspace_boundary_repo()
    kg_repo = get_shared_knowledge_graph_repository()

    ws_id = "ws_ablation_kg"
    doc_id = "doc_eval_kg"
    doc_ver = "v1"
    now = "2026-11-17T00:00:00Z"
    pub_id = generate_publication_id()

    b_repo.create_workspace(ws_id, owner_principal_id="test", workspace_id=ws_id)
    b_repo.create_source(ws_id, "eval.pdf", source_id=doc_id)
    b_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws_id, document_id=doc_id, source="eval.pdf", version=doc_ver,
        format="pdf", created_at=now, persisted_rows=4, total_rows=4,
        graph_status="ready", graph_entities_count=2, graph_relations_count=1,
    ))

    # Entity on page 3
    ent_a = KnowledgeGraphEntity(
        entity_id="e_eval_qd", workspace_id=ws_id, name="Quantum Dynamics",
        canonical_name="quantum_dynamics", entity_type="organization", source_document_id=doc_id,
    )
    ent_b = KnowledgeGraphEntity(
        entity_id="e_eval_ai", workspace_id=ws_id, name="Apex Holdings",
        canonical_name="apex_holdings", entity_type="organization", source_document_id=doc_id,
    )
    rel = KnowledgeGraphRelationship(
        relationship_id="rel_eval_parent", workspace_id=ws_id, source_entity_id=ent_a.entity_id,
        source_entity_name=ent_a.name, predicate="PARENT_COMPANY", canonical_predicate="PARENT_COMPANY",
        target_entity_id=ent_b.entity_id, target_entity_name=ent_b.name,
        confidence=1.0, source_document_id=doc_id, source_document_version=doc_ver,
        publication_id=pub_id, source_chunk_ids=("c_p3",),
        location=DocumentLocation(page=3), verbatim_evidence="Quantum Dynamics is controlled by Apex Holdings.",
        created_at_utc=now,
    )

    kg_repo.stage_relationships(ws_id, doc_id, doc_ver, pub_id, [ent_a, ent_b], [rel])
    kg_repo.commit_manifest(ws_id, KnowledgeGraphManifest(
        workspace_id=ws_id, document_id=doc_id, document_version=doc_ver, publication_id=pub_id,
        status="ready", entity_count=2, relationship_count=1, ingested_at_utc=now, content_hash="k_eval",
    ))

    c1 = _make_chunk("c_p1", doc_id, ws_id, "Introduction to quantum dynamics and general physics.", 1, document_version=doc_ver)
    c2 = _make_chunk("c_p2", doc_id, ws_id, "Market competition and industry background overview.", 2, document_version=doc_ver)
    c3 = _make_chunk("c_p3", doc_id, ws_id, "Quantum Dynamics is controlled by Apex Holdings with full voting control.", 3, document_version=doc_ver)
    c4 = _make_chunk("c_p4", doc_id, ws_id, "Risk factors and regulatory uncertainties forward looking statements.", 4, document_version=doc_ver)
    doc = Document(id=doc_id, filename="eval.pdf", source_format="pdf", chunks=(c1, c2, c3, c4), workspace_id=ws_id)
    repo.save(doc)

    query = "Who is the parent company of Quantum Dynamics?"

    # Warmup pipeline run
    pipeline.run(query=query, workspace_id=ws_id, force_full_path=True)

    # Run with Knowledge Graph enabled
    monkeypatch.setattr(settings, "GRAPH_ENABLED", True)
    res_enabled = pipeline.run(query=query, workspace_id=ws_id, force_full_path=True)

    # Run with Knowledge Graph disabled
    monkeypatch.setattr(settings, "GRAPH_ENABLED", False)
    res_disabled = pipeline.run(query=query, workspace_id=ws_id, force_full_path=True)

    # Compare Recall@5: Target chunk c_p3 should be in RRF for both
    rrf_enabled = res_enabled.retrieval_candidates.get("rrf", [])
    rrf_disabled = res_disabled.retrieval_candidates.get("rrf", [])
    assert "c_p3" in rrf_enabled
    assert "c_p3" in rrf_disabled

    # Compare Precision@3: KG boost elevates c_p3 to top ranks
    top3_enabled = rrf_enabled[:3]
    assert "c_p3" in top3_enabled

    # Latency budget: KG traversal must remain within 75ms deadline (AC-8, AC-10)
    assert res_enabled.graph_latency_ms <= 75.0
    assert res_disabled.graph_latency_ms == 0.0


# ===========================================================================
# 7. Benchmark Comparison Barrier Enforcement (AC-13)
# ===========================================================================

def test_benchmark_comparison_barrier_enforcement_ac13():
    """Verify validate_benchmark_comparison_preconditions blocks accuracy claims on non-canonical workspaces."""
    # Permitted canonical comparison workspace
    assert validate_benchmark_comparison_preconditions(
        baseline_run_id="run_base_kg_001",
        candidate_run_id="run_cand_kg_001",
        comparison_workspace_id="ws_canonical_comparison_0004",
    ) is True

    # Blocked on arbitrary or uncanonical workspace
    with pytest.raises(ValueError, match="Benchmark comparison claims are blocked"):
        validate_benchmark_comparison_preconditions(
            baseline_run_id="run_base_kg_001",
            candidate_run_id="run_cand_kg_001",
            comparison_workspace_id="ws_unauthorized_custom_workspace",
        )


# ===========================================================================
# 8. Pipeline Tenant Boundary Isolation and Cross-Tenant Immunity (AC-6, AC-11)
# ===========================================================================

def test_query_pipeline_tenant_isolation_ac6_ac11():
    """Verify cross-tenant pipeline execution never leaks KG candidates between workspaces."""
    repo = CloudRepository()
    b_repo = get_workspace_boundary_repo()
    kg_repo = get_shared_knowledge_graph_repository()

    ws_a = "ws_tenant_alpha_kg"
    ws_b = "ws_tenant_beta_kg"
    doc_a = "doc_alpha_confidential"
    doc_ver = "v1"
    now = "2026-11-17T00:00:00Z"
    pub_a = generate_publication_id()

    # Workspace A setup with confidential KG relationship
    b_repo.create_workspace(ws_a, owner_principal_id="alice", workspace_id=ws_a)
    b_repo.create_source(ws_a, "confidential.pdf", source_id=doc_a)
    b_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws_a, document_id=doc_a, source="confidential.pdf", version=doc_ver,
        format="pdf", created_at=now, persisted_rows=1, total_rows=1,
        graph_status="ready", graph_entities_count=2, graph_relations_count=1,
    ))

    ent_a1 = KnowledgeGraphEntity(
        entity_id="e_alpha_secret", workspace_id=ws_a, name="Project Aegis",
        canonical_name="project_aegis", entity_type="concept", source_document_id=doc_a,
    )
    ent_a2 = KnowledgeGraphEntity(
        entity_id="e_alpha_loc", workspace_id=ws_a, name="Site Zero",
        canonical_name="site_zero", entity_type="location", source_document_id=doc_a,
    )
    rel_a = KnowledgeGraphRelationship(
        relationship_id="rel_alpha_secret", workspace_id=ws_a, source_entity_id=ent_a1.entity_id,
        source_entity_name=ent_a1.name, predicate="LOCATED_AT", canonical_predicate="LOCATED_AT",
        target_entity_id=ent_a2.entity_id, target_entity_name=ent_a2.name,
        confidence=1.0, source_document_id=doc_a, source_document_version=doc_ver,
        publication_id=pub_a, source_chunk_ids=("c_alpha_kg_1",),
        location=DocumentLocation(page=1), verbatim_evidence="Project Aegis is located at Site Zero.",
        created_at_utc=now,
    )

    kg_repo.stage_relationships(ws_a, doc_a, doc_ver, pub_a, [ent_a1, ent_a2], [rel_a])
    kg_repo.commit_manifest(ws_a, KnowledgeGraphManifest(
        workspace_id=ws_a, document_id=doc_a, document_version=doc_ver, publication_id=pub_a,
        status="ready", entity_count=2, relationship_count=1, ingested_at_utc=now, content_hash="k_alpha",
    ))

    chunk_a = _make_chunk("c_alpha_kg_1", doc_a, ws_a, "Project Aegis is located at Site Zero under top security.", 1, document_version=doc_ver)
    repo.save(Document(id=doc_a, filename="confidential.pdf", source_format="pdf", chunks=(chunk_a,), workspace_id=ws_a))

    # Workspace B setup without this entity
    b_repo.create_workspace(ws_b, owner_principal_id="bob", workspace_id=ws_b)

    # Bob queries his own workspace for Project Aegis
    res_b = pipeline.run(query="Where is Project Aegis located?", workspace_id=ws_b, force_full_path=True)

    # Must have 0 cross-tenant leakage
    assert res_b.graph_candidate_ids == []
    assert res_b.graph_match_count == 0
    assert res_b.graph_entity_state == "none"
    assert "c_alpha_kg_1" not in res_b.retrieval_candidates.get("graph", [])
    assert "c_alpha_kg_1" not in res_b.retrieval_candidates.get("rrf", [])


# ===========================================================================
# 9. Router Timeout Fallback to Baseline (AC-8, AC-10)
# ===========================================================================

def test_graph_timeout_fallback_to_baseline_ac8():
    """Verify that a storage timeout in KG lookup gracefully falls back without breaking pipeline."""
    with patch("services.retrieval.knowledge_graph_retriever.KnowledgeGraphRetriever.expand") as mock_expand:
        mock_expand.return_value = KnowledgeGraphLookupResult(
            seed_entities=("Omega Corp",),
            entity_state="none",
            relationship_state="none",
            execution_status="caller_timeout",
            candidate_chunks=(),
            matched_chunk_ids=(),
            traversed_node_count=0,
            edge_match_count=0,
            latency_ms=75.5,
        )

        state = {
            "query": "Who owns Omega Corp?",
            "workspace_id": "ws_test_timeout",
            "plan": Plan(fast_path=False, use_graph=True, stages=["bm25", "vector", "knowledge_graph"]),
            "stage_timings": {},
        }
        res = run_graph(state)

        assert res["graph_chunks"] == []
        assert res["graph_candidate_ids"] == []
        assert res["graph_match_count"] == 0
        assert res["graph_execution_status"] == "caller_timeout"
        assert res["stage_timings"]["graph_ms"] >= 0.0


# ===========================================================================
# 10. Decoupled Baseline Ingestion & Snapshot Metadata (AC-3, AC-11, AC-12)
# ===========================================================================

def test_documents_service_snapshot_metadata_ac12():
    """Verify documents service enriches DocumentSnapshot with graph status and metadata
    while maintaining decoupled baseline resilience per ADR-001."""
    from modules.documents.documents_service import DocumentsService

    ws_id = "ws_enrichment_test"
    doc_id = "doc_enrich_1"

    # 1. Normal enrichment sets ready status and counts
    with patch("ingestion.graph_builder.extract_and_persist_graph") as mock_extract:
        mock_extract.return_value = {
            "status": "ready",
            "entities_count": 5,
            "relations_count": 3,
        }

        status, e_cnt, r_cnt = DocumentsService._enrich_graph_snapshot_metadata(
            workspace_id=ws_id,
            doc_id=doc_id,
            doc_ver="v1",
            filename="test.pdf",
            chunks=[],
        )

        assert status == "ready"
        assert e_cnt == 5
        assert r_cnt == 3

    # 2. Failure resilience: exception during graph extraction sets graph_status='failed'
    # without failing the caller or corrupting baseline readiness (ADR-001)
    with patch("ingestion.graph_builder.extract_and_persist_graph", side_effect=RuntimeError("Graph extraction crash")):
        status_f, e_f, r_f = DocumentsService._enrich_graph_snapshot_metadata(
            workspace_id=ws_id,
            doc_id=doc_id,
            doc_ver="v2",
            filename="test.pdf",
            chunks=[],
        )

        assert status_f == "failed"
        assert e_f == 0
        assert r_f == 0

    # 3. Empty extraction sets graph_status='empty' and builds valid DocumentSnapshot (Major finding fix)
    with patch("ingestion.graph_builder.extract_and_persist_graph") as mock_extract:
        mock_extract.return_value = {
            "status": "empty",
            "entities_count": 0,
            "relations_count": 0,
        }

        status_e, e_e, r_e = DocumentsService._enrich_graph_snapshot_metadata(
            workspace_id=ws_id,
            doc_id=doc_id,
            doc_ver="v3",
            filename="empty.csv",
            chunks=[],
        )

        assert status_e == "empty"
        assert e_e == 0
        assert r_e == 0

        # Verify DocumentSnapshot instantiates without validation error
        from schemas.contracts.boundary import DocumentSnapshot
        snapshot = DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source="empty.csv",
            version="v3",
            format="csv",
            created_at="2026-11-17T12:00:00Z",
            graph_status=status_e,
            graph_entities_count=e_e,
            graph_relations_count=r_e,
        )
        assert snapshot.graph_status == "empty"
        assert snapshot.graph_entities_count == 0
        assert snapshot.graph_relations_count == 0


# ===========================================================================
# 11. KnowledgeGraphStrategy Direct Retrieval Contract (AC-11)
# ===========================================================================

def test_knowledge_graph_strategy_retrieve_ac11(monkeypatch):
    """Verify KnowledgeGraphStrategy directly obeys GRAPH_ENABLED toggle and fast path bypass."""
    import asyncio
    strategy = KnowledgeGraphStrategy()

    # Feature toggle disabled returns empty result with status 'disabled'
    monkeypatch.setattr(settings, "GRAPH_ENABLED", False)
    res_disabled = asyncio.run(strategy.retrieve(query="Test Query", workspace_id="ws_strat"))
    assert res_disabled.items == []
    assert res_disabled.failure_reason == "graph_disabled"
    assert res_disabled.debug_trace.get("status") == "disabled"

    # Fast path query returns empty result with status 'fast_path_bypassed'
    monkeypatch.setattr(settings, "GRAPH_ENABLED", True)
    fast_plan = Plan(fast_path=True, use_graph=False)
    res_fast = asyncio.run(strategy.retrieve(query="Test Query", workspace_id="ws_strat", plan=fast_plan))
    assert res_fast.items == []
    assert res_fast.failure_reason == "fast_path_bypassed"
    assert res_fast.debug_trace.get("status") == "fast_path_bypassed"


# ===========================================================================
# 12. Dynamic Tombstone Fail-Closed Enforcement (AC-7)
# ===========================================================================

def test_dynamic_tombstone_during_pipeline_traversal_ac7():
    """Verify dynamic tombstone enforcement fails closed during pipeline graph traversal."""
    ws_id = "ws_tombstone_kg"
    b_repo = get_workspace_boundary_repo()
    b_repo.create_workspace(ws_id, owner_principal_id="test", workspace_id=ws_id)
    # Tombstone entire workspace
    b_repo.tombstone_workspace(ws_id, caller_principal_id="test")

    state = {
        "query": "Tell me about Acme Corp",
        "workspace_id": ws_id,
        "plan": Plan(fast_path=False, use_graph=True, stages=["bm25", "vector", "knowledge_graph"]),
        "stage_timings": {},
    }

    res = run_graph(state)
    assert res["graph_chunks"] == []
    assert res["graph_candidate_ids"] == []
    assert res["graph_match_count"] == 0


# ===========================================================================
# 13. Shared Repository Reset and Legacy Strategy Init (Review Minor Findings)
# ===========================================================================

def test_shared_repository_reset_and_legacy_strategy_init():
    """Verify shared repository reset returns a fresh instance and strategy gracefully accepts legacy repository."""
    # 1. Reset returns fresh non-None instance
    repo = get_shared_knowledge_graph_repository(reset=True)
    assert repo is not None
    assert hasattr(repo, "resolve_mentions")

    # 2. Legacy caller passing CloudRepository or mock lacking resolve_mentions
    class MockLegacyRepository:
        def get_chunks_by_ids(self, workspace_id, keys):
            return []

    legacy_repo = MockLegacyRepository()
    strat = KnowledgeGraphStrategy(repository=legacy_repo)
    assert strat.repository is not None
    assert hasattr(strat.repository, "resolve_mentions")
    assert strat.documents_repo is legacy_repo


# ===========================================================================
# 14. Real Application Path Ingestion Wiring (Slice 3 Wiring Blockers)
# ===========================================================================

def test_documents_service_csv_ingestion_with_authoritative_schema_relations():
    """Verify CSV ingestion resolves verified schema relations from TableStore and persists grounded graph."""
    import hashlib
    import tempfile
    from pathlib import Path
    from schemas.structured_table import TableSchema, ColumnDefinition, HeaderTopology, InferredDtype
    from ingestion.graph_builder import DirectionalSchemaRelation
    from modules.documents.documents_service import DocumentsService
    from services.retrieval.knowledge_graph_retriever import KnowledgeGraphRetriever

    ws_id = "ws_csv_app_wiring"
    doc_id = "doc_depts_app"
    b_repo = get_workspace_boundary_repo()
    b_repo.create_workspace(name="CSV App WS", owner_principal_id="test_user", workspace_id=ws_id)

    csv_text = (
        "Department,Manager,DeptCode\n"
        "Engineering,Alice Smith,ENG\n"
        "Research,Bob Jones,RES\n"
    )
    csv_bytes = csv_text.encode("utf-8")
    doc_ver = hashlib.sha256(csv_bytes).hexdigest()[:32]
    table_id = "departments"

    table_store = get_shared_table_store()
    # Store authoritative schema relations on TableStore for this exact version
    table_store.store_schema(
        table_id=table_id,
        workspace_id=ws_id,
        schema=TableSchema(
            columns=(
                ColumnDefinition(col_index=0, name="Department", inferred_dtype=InferredDtype.STRING),
                ColumnDefinition(col_index=1, name="Manager", inferred_dtype=InferredDtype.STRING),
                ColumnDefinition(col_index=2, name="DeptCode", inferred_dtype=InferredDtype.STRING),
            ),
            header_rows=(0,),
            header_topology=HeaderTopology.SINGLE_ROW,
            confidence=1.0,
            schema_relations=(
                DirectionalSchemaRelation(
                    source_column="Department",
                    predicate="managed_by",
                    target_column="Manager",
                    source_type="organization",
                    target_type="person",
                    disambiguation_column="DeptCode",
                ),
            ),
        ),
        document_id=doc_id,
        document_version=doc_ver,
    )

    doc_service = DocumentsService()
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(csv_bytes)
        tmp_path = tmp.name

    try:
        doc_service.background_ingest(
            temp_path_str=tmp_path,
            workspace_id=ws_id,
            doc_id=doc_id,
            filename="departments.csv",
            content=csv_bytes,
            size_str="100 B",
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # 1. Assert DocumentSnapshot records ready graph metadata
    snapshot = b_repo.get_document_snapshot(ws_id, doc_id)
    assert snapshot is not None
    assert snapshot.graph_status == "ready"
    assert snapshot.graph_entities_count == 4
    assert snapshot.graph_relations_count == 2

    # 2. Assert KnowledgeGraphManifest committed
    kg_repo = get_shared_knowledge_graph_repository()
    manifest = kg_repo.get_manifest(ws_id, doc_id, doc_ver, consistent_read=True)
    assert manifest is not None
    assert manifest.status == "ready"
    assert manifest.entity_count == 4
    assert manifest.relationship_count == 2

    # 3. Assert grounded retrieval through KnowledgeGraphRetriever
    retriever = KnowledgeGraphRetriever(repository=kg_repo, boundary_repo=b_repo)
    res = retriever.expand(workspace_id=ws_id, query="Engineering")
    assert res.edge_match_count >= 1
    assert len(res.matched_chunk_ids) >= 1
    assert f"{doc_id}:csv:row2" in res.matched_chunk_ids


def test_documents_service_csv_absent_schema_relations_yields_empty_manifest():
    """Verify CSV ingestion without schema relations publishes empty graph manifest while preserving Ready status."""
    import hashlib
    import tempfile
    from pathlib import Path
    from modules.documents.documents_service import DocumentsService

    ws_id = "ws_csv_empty_wiring"
    doc_id = "doc_items_empty"
    b_repo = get_workspace_boundary_repo()
    b_repo.create_workspace(name="CSV Empty WS", owner_principal_id="test_user", workspace_id=ws_id)

    csv_text = (
        "Item,Category,Price\n"
        "Widget,Hardware,10\n"
        "Gadget,Electronics,20\n"
    )
    csv_bytes = csv_text.encode("utf-8")
    doc_ver = hashlib.sha256(csv_bytes).hexdigest()[:32]

    doc_service = DocumentsService()
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(csv_bytes)
        tmp_path = tmp.name

    try:
        doc_service.background_ingest(
            temp_path_str=tmp_path,
            workspace_id=ws_id,
            doc_id=doc_id,
            filename="items.csv",
            content=csv_bytes,
            size_str="100 B",
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    snapshot = b_repo.get_document_snapshot(ws_id, doc_id)
    assert snapshot is not None
    assert snapshot.graph_status == "empty"
    assert snapshot.graph_entities_count == 0
    assert snapshot.graph_relations_count == 0

    from modules.documents.documents_repository import _WORKSPACE_DOCS
    doc_record = doc_service.repo.get(doc_id)
    assert doc_record is not None
    doc_entry = next((d for d in _WORKSPACE_DOCS.get(ws_id, []) if str(d.get("id")) == str(doc_id)), None)
    assert doc_entry is not None
    assert doc_entry["status"] == "Ready"


def test_documents_service_csv_cross_workspace_version_conflict_rejected():
    """Verify schema relations referencing another workspace or version are strictly rejected."""
    import hashlib
    import tempfile
    from pathlib import Path
    from schemas.structured_table import TableSchema, ColumnDefinition, HeaderTopology, InferredDtype
    from ingestion.graph_builder import DirectionalSchemaRelation
    from modules.documents.documents_service import DocumentsService

    ws_id = "ws_csv_active"
    doc_id = "doc_conflict_csv"
    b_repo = get_workspace_boundary_repo()
    b_repo.create_workspace(name="CSV Active", owner_principal_id="test_user", workspace_id=ws_id)

    csv_text = "ColA,ColB\nValA,ValB\n"
    csv_bytes = csv_text.encode("utf-8")
    doc_ver = hashlib.sha256(csv_bytes).hexdigest()[:32]
    table_id = "conflicttbl"

    table_store = get_shared_table_store()
    # Schema metadata points to a conflicting foreign workspace and version
    table_store.store_schema(
        table_id=table_id,
        workspace_id=ws_id,
        schema=TableSchema(
            columns=(
                ColumnDefinition(col_index=0, name="ColA", inferred_dtype=InferredDtype.STRING),
                ColumnDefinition(col_index=1, name="ColB", inferred_dtype=InferredDtype.STRING),
            ),
            header_rows=(0,),
            header_topology=HeaderTopology.SINGLE_ROW,
            confidence=1.0,
            schema_relations=(
                DirectionalSchemaRelation(
                    source_column="ColA",
                    predicate="relates_to",
                    target_column="ColB",
                ),
            ),
            metadata={"workspace_id": "ws_FOREIGN", "document_version": "v_DIFFERENT"},
        ),
        document_id=doc_id,
        document_version=doc_ver,
    )

    doc_service = DocumentsService()
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(csv_bytes)
        tmp_path = tmp.name

    try:
        doc_service.background_ingest(
            temp_path_str=tmp_path,
            workspace_id=ws_id,
            doc_id=doc_id,
            filename="conflicttbl.csv",
            content=csv_bytes,
            size_str="100 B",
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    snapshot = b_repo.get_document_snapshot(ws_id, doc_id)
    assert snapshot is not None
    # Conflicting schema relations are rejected, resulting in empty graph status
    assert snapshot.graph_status == "empty"
    assert snapshot.graph_entities_count == 0


def test_documents_service_pdf_ingestion_with_mocked_bedrock_provider():
    """Verify PDF ingestion calls CountTokens and Converse, reconciles spend, and publishes retrievable graph."""
    import hashlib
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from modules.documents.documents_service import DocumentsService
    from services.retrieval.knowledge_graph_retriever import KnowledgeGraphRetriever

    ws_id = "ws_pdf_app_wiring"
    doc_id = "doc_pdf_acme"
    b_repo = get_workspace_boundary_repo()
    b_repo.create_workspace(name="PDF App WS", owner_principal_id="test_user", workspace_id=ws_id)

    pdf_bytes = b"%PDF-1.4 sample pdf content for knowledge graph extraction."
    doc_ver = hashlib.sha256(pdf_bytes).hexdigest()[:32]

    ch_text = "Acme Corp acquired Beta Tech in 2024."
    chunk = Chunk(
        id=f"{doc_id}:c1",
        document_id=doc_id,
        workspace_id=ws_id,
        text=ch_text,
        source_format="pdf",
        element_type="paragraph",
        page_number=1,
        metadata={"document_version": doc_ver},
        embedding_fast=embed_fast_local(ch_text),
        embedding_full=_deterministic_vector(ch_text, 1024),
    )
    parsed_doc = Document(id=doc_id, filename="acme.pdf", source_format="pdf", chunks=[chunk], workspace_id=ws_id)

    class MockBedrockClient:
        def __init__(self):
            self.count_tokens_calls = 0
            self.converse_calls = 0

        def count_tokens(self, modelId, input):
            self.count_tokens_calls += 1
            return {"inputTokens": 140}

        def converse(self, modelId, messages, system=None, inferenceConfig=None, timeout_s=None):
            self.converse_calls += 1
            return {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": json.dumps({
                                    "entities": [
                                        {"name": "Acme Corp", "entity_type": "organization"},
                                        {"name": "Beta Tech", "entity_type": "organization"},
                                    ],
                                    "relationships": [
                                        {
                                            "source_entity": "Acme Corp",
                                            "predicate": "acquired",
                                            "target_entity": "Beta Tech",
                                            "chunk_id": f"{doc_id}:c1",
                                            "verbatim_evidence": "Acme Corp acquired Beta Tech in 2024.",
                                        }
                                    ],
                                })
                            }
                        ]
                    }
                },
                "usage": {"inputTokens": 140, "outputTokens": 50},
            }

    mock_bedrock = MockBedrockClient()
    doc_service = DocumentsService(bedrock_client=mock_bedrock)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        with patch("api.routes.ingest_document", return_value=parsed_doc), \
             patch("modules.documents.documents_service.ingest_document", return_value=parsed_doc):
            doc_service.background_ingest(
                temp_path_str=tmp_path,
                workspace_id=ws_id,
                doc_id=doc_id,
                filename="acme.pdf",
                content=pdf_bytes,
                size_str="100 B",
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # Assert CountTokens and Converse executed
    assert mock_bedrock.count_tokens_calls == 1
    assert mock_bedrock.converse_calls == 1

    # Assert durable ledger reconciled spend
    kg_repo = get_shared_knowledge_graph_repository()
    ledger = kg_repo.get_budget_ledger(ws_id, doc_id, doc_ver, consistent_read=True)
    assert ledger is not None
    assert ledger.spent_prompt_tokens == 140
    assert ledger.spent_output_tokens == 50
    assert ledger.completed_calls == 1

    # Assert DocumentSnapshot records ready graph status
    snapshot = b_repo.get_document_snapshot(ws_id, doc_id)
    assert snapshot is not None
    assert snapshot.graph_status == "ready"
    assert snapshot.graph_entities_count == 2
    assert snapshot.graph_relations_count == 1

    # Assert graph retrieval returns grounded chunk
    retriever = KnowledgeGraphRetriever(repository=kg_repo, boundary_repo=b_repo)
    res = retriever.expand(workspace_id=ws_id, query="Acme Corp")
    assert res.edge_match_count >= 1
    assert f"{doc_id}:c1" in res.matched_chunk_ids


def test_documents_service_pdf_missing_provider_returns_unavailable_baseline_ready():
    """Verify PDF ingestion with missing provider returns unavailable/failed and preserves baseline Ready status."""
    import hashlib
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from modules.documents.documents_service import DocumentsService

    ws_id = "ws_pdf_missing_prov"
    doc_id = "doc_missing_prov"
    b_repo = get_workspace_boundary_repo()
    b_repo.create_workspace(name="PDF Missing Prov", owner_principal_id="test_user", workspace_id=ws_id)

    pdf_bytes = b"%PDF-1.4 test document without client."
    doc_ver = hashlib.sha256(pdf_bytes).hexdigest()[:32]

    ch_text = "Some text."
    chunk = Chunk(
        id=f"{doc_id}:c1",
        document_id=doc_id,
        workspace_id=ws_id,
        text=ch_text,
        source_format="pdf",
        element_type="paragraph",
        page_number=1,
        metadata={"document_version": doc_ver},
        embedding_fast=embed_fast_local(ch_text),
        embedding_full=_deterministic_vector(ch_text, 1024),
    )
    parsed_doc = Document(id=doc_id, filename="missing.pdf", source_format="pdf", chunks=[chunk], workspace_id=ws_id)

    # DocumentsService without injected client in test environment (missing provider configuration)
    doc_service = DocumentsService(bedrock_client=None)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        with patch("api.routes.ingest_document", return_value=parsed_doc), \
             patch("modules.documents.documents_service.ingest_document", return_value=parsed_doc):
            doc_service.background_ingest(
                temp_path_str=tmp_path,
                workspace_id=ws_id,
                doc_id=doc_id,
                filename="missing.pdf",
                content=pdf_bytes,
                size_str="100 B",
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    snapshot = b_repo.get_document_snapshot(ws_id, doc_id)
    assert snapshot is not None
    # Missing provider must NEVER return a successful empty extraction ("empty") or "ready"
    assert snapshot.graph_status in ("unavailable", "failed")
    assert snapshot.graph_status != "ready"
    assert snapshot.graph_status != "empty"

    # Baseline publication remains Ready per ADR-001
    from modules.documents.documents_repository import _WORKSPACE_DOCS
    doc_record = doc_service.repo.get(doc_id)
    assert doc_record is not None
    doc_entry = next((d for d in _WORKSPACE_DOCS.get(ws_id, []) if str(d.get("id")) == str(doc_id)), None)
    assert doc_entry is not None
    assert doc_entry["status"] == "Ready"


def test_documents_service_pdf_provider_failure_reconciles_fail_closed():
    """Verify PDF provider call failure reconciles fail closed and preserves baseline Ready status."""
    import hashlib
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from modules.documents.documents_service import DocumentsService

    ws_id = "ws_pdf_prov_fail"
    doc_id = "doc_prov_fail"
    b_repo = get_workspace_boundary_repo()
    b_repo.create_workspace(name="PDF Prov Fail", owner_principal_id="test_user", workspace_id=ws_id)

    pdf_bytes = b"%PDF-1.4 failure test document."
    doc_ver = hashlib.sha256(pdf_bytes).hexdigest()[:32]

    ch_text = "Acme acquired Beta."
    chunk = Chunk(
        id=f"{doc_id}:c1",
        document_id=doc_id,
        workspace_id=ws_id,
        text=ch_text,
        source_format="pdf",
        element_type="paragraph",
        page_number=1,
        metadata={"document_version": doc_ver},
        embedding_fast=embed_fast_local(ch_text),
        embedding_full=_deterministic_vector(ch_text, 1024),
    )
    parsed_doc = Document(id=doc_id, filename="fail.pdf", source_format="pdf", chunks=[chunk], workspace_id=ws_id)

    class FailingBedrockClient:
        def count_tokens(self, modelId, input):
            return {"inputTokens": 100}

        def converse(self, *args, **kwargs):
            raise RuntimeError("Bedrock Converse network timeout")

    doc_service = DocumentsService(bedrock_client=FailingBedrockClient())

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        with patch("api.routes.ingest_document", return_value=parsed_doc), \
             patch("modules.documents.documents_service.ingest_document", return_value=parsed_doc):
            doc_service.background_ingest(
                temp_path_str=tmp_path,
                workspace_id=ws_id,
                doc_id=doc_id,
                filename="fail.pdf",
                content=pdf_bytes,
                size_str="100 B",
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    snapshot = b_repo.get_document_snapshot(ws_id, doc_id)
    assert snapshot is not None
    assert snapshot.graph_status == "failed"

    from modules.documents.documents_repository import _WORKSPACE_DOCS
    doc_record = doc_service.repo.get(doc_id)
    assert doc_record is not None
    doc_entry = next((d for d in _WORKSPACE_DOCS.get(ws_id, []) if str(d.get("id")) == str(doc_id)), None)
    assert doc_entry is not None
    assert doc_entry["status"] == "Ready"


def test_documents_service_pdf_valid_zero_relationships_yields_empty():
    """Verify that a valid provider call returning zero relationships produces status='empty' with recorded spend."""
    import hashlib
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from modules.documents.documents_service import DocumentsService

    ws_id = "ws_pdf_valid_empty"
    doc_id = "doc_pdf_empty_valid"
    b_repo = get_workspace_boundary_repo()
    b_repo.create_workspace(name="PDF Valid Empty", owner_principal_id="test_user", workspace_id=ws_id)

    pdf_bytes = b"%PDF-1.4 document with narrative having zero entity relationships."
    doc_ver = hashlib.sha256(pdf_bytes).hexdigest()[:32]

    ch_text = "The weather was sunny in May."
    chunk = Chunk(
        id=f"{doc_id}:c1",
        document_id=doc_id,
        workspace_id=ws_id,
        text=ch_text,
        source_format="pdf",
        element_type="paragraph",
        page_number=1,
        metadata={"document_version": doc_ver},
        embedding_fast=embed_fast_local(ch_text),
        embedding_full=_deterministic_vector(ch_text, 1024),
    )
    parsed_doc = Document(id=doc_id, filename="empty_rel.pdf", source_format="pdf", chunks=[chunk], workspace_id=ws_id)

    class ZeroRelBedrockClient:
        def count_tokens(self, modelId, input):
            return {"inputTokens": 110}

        def converse(self, *args, **kwargs):
            return {
                "output": {
                    "message": {
                        "content": [{"text": json.dumps({"entities": [], "relationships": []})}]
                    }
                },
                "usage": {"inputTokens": 110, "outputTokens": 20},
            }

    doc_service = DocumentsService(bedrock_client=ZeroRelBedrockClient())

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        with patch("api.routes.ingest_document", return_value=parsed_doc), \
             patch("modules.documents.documents_service.ingest_document", return_value=parsed_doc):
            doc_service.background_ingest(
                temp_path_str=tmp_path,
                workspace_id=ws_id,
                doc_id=doc_id,
                filename="empty_rel.pdf",
                content=pdf_bytes,
                size_str="100 B",
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    snapshot = b_repo.get_document_snapshot(ws_id, doc_id)
    assert snapshot is not None
    assert snapshot.graph_status == "empty"
    assert snapshot.graph_entities_count == 0
    assert snapshot.graph_relations_count == 0

    kg_repo = get_shared_knowledge_graph_repository()
    ledger = kg_repo.get_budget_ledger(ws_id, doc_id, doc_ver, consistent_read=True)
    assert ledger is not None
    assert ledger.spent_prompt_tokens == 110
    assert ledger.spent_output_tokens == 20
    assert ledger.completed_calls == 1


def test_bounded_recovery_from_stub_empty_manifest():
    """Verify that a stub empty manifest from missing provider can be recovered when valid client is provided."""
    import hashlib
    import json
    from ingestion.graph_builder import KnowledgeGraphBuilder
    from schemas.contracts.knowledge_graph import KnowledgeGraphManifest

    ws_id = "ws_stub_recovery"
    doc_id = "doc_stub_rec"
    doc_ver = "v_rec_1"
    kg_repo = get_shared_knowledge_graph_repository()
    builder = KnowledgeGraphBuilder(repository=kg_repo)

    idempotency_key = hashlib.sha256(f"{ws_id}:{doc_id}:{doc_ver}:kg".encode("utf-8")).hexdigest()

    # Commit an old stub empty manifest with zero completed calls in ledger
    stub_man = KnowledgeGraphManifest(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=doc_ver,
        publication_id="pub_stub",
        status="empty",
        entity_count=0,
        relationship_count=0,
        ingested_at_utc="2026-11-17T00:00:00Z",
        content_hash=idempotency_key,
    )
    kg_repo.commit_manifest(ws_id, stub_man)

    ch_text = "Acme Corp acquired Beta Tech in 2024."
    chunk = Chunk(
        id=f"{doc_id}:c1",
        document_id=doc_id,
        workspace_id=ws_id,
        text=ch_text,
        source_format="pdf",
        element_type="paragraph",
        page_number=1,
        metadata={"document_version": doc_ver},
        embedding_fast=embed_fast_local(ch_text),
        embedding_full=_deterministic_vector(ch_text, 1024),
    )

    class AuthoritativeBedrockClient:
        def __init__(self):
            self.calls = 0

        def count_tokens(self, modelId, input):
            return {"inputTokens": 120}

        def converse(self, *args, **kwargs):
            self.calls += 1
            return {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": json.dumps({
                                    "entities": [
                                        {"name": "Acme Corp", "entity_type": "organization"},
                                        {"name": "Beta Tech", "entity_type": "organization"},
                                    ],
                                    "relationships": [
                                        {
                                            "source_entity": "Acme Corp",
                                            "predicate": "acquired",
                                            "target_entity": "Beta Tech",
                                            "chunk_id": f"{doc_id}:c1",
                                            "verbatim_evidence": "Acme Corp acquired Beta Tech in 2024.",
                                        }
                                    ],
                                })
                            }
                        ]
                    }
                },
                "usage": {"inputTokens": 120, "outputTokens": 45},
            }

    auth_client = AuthoritativeBedrockClient()
    # Now execute with authoritative client - should bypass stub manifest and recover
    recovered_man = builder.build_and_persist_pdf_graph(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=doc_ver,
        chunks=[chunk],
        client=auth_client,
    )
    assert recovered_man.status == "ready"
    assert recovered_man.entity_count == 2
    assert recovered_man.relationship_count == 1
    assert auth_client.calls == 1

    # Replay should now perform idempotent skip
    replay_man = builder.build_and_persist_pdf_graph(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=doc_ver,
        chunks=[chunk],
        client=auth_client,
    )
    assert replay_man.status == "ready"
    assert auth_client.calls == 1  # 0 extra calls!


def test_baseline_published_before_enrichment_and_preserved_on_failure():
    """Verify baseline DocumentSnapshot is published before enrichment begins and remains Ready on failure."""
    import hashlib
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from modules.documents.documents_service import DocumentsService

    ws_id = "ws_baseline_decoupled"
    doc_id = "doc_base_decoupled"
    b_repo = get_workspace_boundary_repo()
    b_repo.create_workspace(name="Baseline Decoupled", owner_principal_id="test_user", workspace_id=ws_id)

    pdf_bytes = b"%PDF-1.4 baseline decoupling verification document."
    doc_ver = hashlib.sha256(pdf_bytes).hexdigest()[:32]

    ch_text = "Some text."
    chunk = Chunk(
        id=f"{doc_id}:c1",
        document_id=doc_id,
        workspace_id=ws_id,
        text=ch_text,
        source_format="pdf",
        element_type="paragraph",
        page_number=1,
        metadata={"document_version": doc_ver},
        embedding_fast=embed_fast_local(ch_text),
        embedding_full=_deterministic_vector(ch_text, 1024),
    )
    parsed_doc = Document(id=doc_id, filename="base.pdf", source_format="pdf", chunks=[chunk], workspace_id=ws_id)

    doc_service = DocumentsService()
    baseline_seen = False

    orig_enrich = doc_service._enrich_graph_snapshot_metadata

    def inspect_and_fail(*args, **kwargs):
        nonlocal baseline_seen
        # Check that baseline DocumentSnapshot already exists before enrichment returns
        snap = b_repo.get_document_snapshot(ws_id, doc_id)
        if snap is not None and snap.version == doc_ver:
            baseline_seen = True
        raise RuntimeError("Simulated graph builder crash")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        with patch("api.routes.ingest_document", return_value=parsed_doc), \
             patch("modules.documents.documents_service.ingest_document", return_value=parsed_doc), \
             patch.object(doc_service, "_enrich_graph_snapshot_metadata", side_effect=inspect_and_fail):
            doc_service.background_ingest(
                temp_path_str=tmp_path,
                workspace_id=ws_id,
                doc_id=doc_id,
                filename="base.pdf",
                content=pdf_bytes,
                size_str="100 B",
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    assert baseline_seen is True
    # Baseline document is Ready in repo despite graph failure
    from modules.documents.documents_repository import _WORKSPACE_DOCS
    doc_record = doc_service.repo.get(doc_id)
    assert doc_record is not None
    doc_entry = next((d for d in _WORKSPACE_DOCS.get(ws_id, []) if str(d.get("id")) == str(doc_id)), None)
    assert doc_entry is not None
    assert doc_entry["status"] == "Ready"
    # Final snapshot records graph_status='failed'
    snap = b_repo.get_document_snapshot(ws_id, doc_id)
    assert snap is not None
    assert snap.graph_status == "failed"

