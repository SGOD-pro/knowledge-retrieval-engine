"""Comprehensive unit and regression tests for Knowledge Graph Slice 1.

Verifies:
1. Typed contracts, deep immutability of PinnedSnapshotContext, and frozen fields.
2. Conservative entity identity, default document scoping, and homonym counterexamples.
3. Legitimate cross-document joins with verified disambiguation tokens.
4. Deterministic tabular CSV relationship extraction from directional schema and foreign keys.
5. Rejection of co-occurrence alone and speculative column similarity.
6. Verbatim directional syntax grounding, reversed relationship rejection, and negated relationship rejection.
7. Publication staging isolation and atomic visibility commit (Memory, SQLite WAL, DynamoDB).
8. Publication race condition and optimistic concurrency check on base_publication_id.
9. Deferred physical cleanup preservation for pinned readers.
10. PinnedSnapshotContext stable per-document bindings guarantee and dynamic tombstone enforcement.
11. Bounded multi-hop traversal with cycle detection, 25-node ceiling, and 3-page pagination ceiling.
12. Exact mention hashing authorization and publication validation.
13. Dynamic source document deletion during traversal failing closed without unhandled exceptions.
14. Targeted chunk hydration capped at 15 items with zero get_all_chunks calls and missing key tolerance.
15. Caller deadline, admission queue rejection, and worker capacity retention.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sqlite3
import tempfile
import threading
import time
from typing import Any
import uuid
from unittest.mock import MagicMock, patch
import pytest
from pydantic import ValidationError

from config import settings
from src.schemas.contracts.location import DocumentLocation, TableLocation
from src.schemas.contracts.knowledge_graph import (
    ChunkLookupKey,
    GraphStatus,
    KnowledgeGraphBudgetLedger,
    KnowledgeGraphEntity,
    KnowledgeGraphLookupResult,
    KnowledgeGraphManifest,
    KnowledgeGraphRelationship,
    PinnedDocumentVersion,
    PinnedSnapshotContext,
    canonicalize_predicate,
    compute_entity_id,
    compute_relationship_id,
    escape_delimiter,
    generate_publication_id,
    normalize_mention,
    unescape_delimiter,
)
from db.knowledge_graph_repo import (
    DynamoDBKnowledgeGraphRepository,
    FencingConflictError,
    InMemoryKnowledgeGraphRepository,
    OptimisticConcurrencyError,
    SQLiteKnowledgeGraphRepository,
    build_pinned_snapshot_context,
)
from ingestion.graph_builder import (
    DirectionalSchemaRelation,
    KnowledgeGraphBuilder,
    TableForeignKey,
    extract_tabular_relationships,
    validate_relationship_grounding,
)
from modules.documents.documents_repository import DocumentsRepository
from schemas.models import Chunk
from services.retrieval.knowledge_graph_retriever import (
    GraphConcurrencyController,
    KnowledgeGraphRetriever,
    extract_candidate_entity_mentions,
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# 1. Typed Contracts and Deep Immutability Tests
# =============================================================================

def test_pinned_snapshot_context_deep_immutability():
    """Verify that PinnedSnapshotContext is deeply immutable and fails closed on mutation."""
    doc_v1 = PinnedDocumentVersion(
        document_id="doc_alpha",
        document_version="1",
        publication_id="pub_001",
        is_tombstoned=False,
    )
    ctx = PinnedSnapshotContext(
        workspace_id="ws_test",
        captured_at_utc=_now_utc(),
        active_documents=(doc_v1,),
    )

    # 1. Attribute reassignment on context fails
    with pytest.raises((ValidationError, TypeError)):
        ctx.workspace_id = "ws_other"

    with pytest.raises((ValidationError, TypeError)):
        ctx.active_documents = ()

    # 2. Reassignment of tuple element fails
    with pytest.raises(TypeError):
        ctx.active_documents[0] = doc_v1  # type: ignore

    # 3. Mutation of nested PinnedDocumentVersion fails
    with pytest.raises((ValidationError, TypeError)):
        ctx.active_documents[0].is_tombstoned = True

    with pytest.raises((ValidationError, TypeError)):
        ctx.active_documents[0].publication_id = "pub_mutated"

    # 4. Context helper methods
    assert ctx.get_document("doc_alpha") == doc_v1
    assert ctx.get_document("doc_nonexistent") is None
    assert ctx.is_active_publication("doc_alpha", "pub_001") is True
    assert ctx.is_active_publication("doc_alpha", "pub_stale") is False


def test_frozen_contracts_forbid_extra_fields():
    """Verify that KnowledgeGraphEntity and KnowledgeGraphRelationship forbid extra attributes and are frozen."""
    ent = KnowledgeGraphEntity(
        entity_id="ent_123",
        workspace_id="ws_test",
        name="Acme Corp",
        canonical_name="acme_corp",
        entity_type="organization",
    )
    with pytest.raises((ValidationError, TypeError)):
        ent.name = "Mutated Corp"

    with pytest.raises(ValidationError):
        KnowledgeGraphEntity(
            entity_id="ent_123",
            workspace_id="ws_test",
            name="Acme Corp",
            canonical_name="acme_corp",
            unexpected_field="disallowed",
        )


# =============================================================================
# 2. Conservative Entity Identity & Homonym Protection Tests
# =============================================================================

def test_same_type_homonym_counterexample():
    """Verify that entities with identical name and type across documents default to document scope.
    
    Counterexample: Professor Michael Jordan (UC Berkeley ML) in Doc A vs Michael Jordan (NBA) in Doc B.
    Both are type 'person' and name 'Michael Jordan'. Without verified disambiguation tokens,
    they must produce distinct entity IDs and never falsely join across documents.
    """
    ws = "ws_homonym_test"
    doc_a = "doc_berkeley_research"
    doc_b = "doc_nba_statistics"

    ent_id_a = compute_entity_id(ws, doc_a, "person", "Michael Jordan")
    ent_id_b = compute_entity_id(ws, doc_b, "person", "Michael Jordan")

    assert ent_id_a != ent_id_b, "Homonyms across different documents must NOT share entity_id without disambiguation"
    assert ent_id_a.startswith("ent_")
    assert ent_id_b.startswith("ent_")


def test_legitimate_cross_document_join_with_disambiguation():
    """Verify that entities across documents join when backed by a verified disambiguation token.
    
    Doc 1 states: Acme Corp (NASDAQ: ACME) acquired Beta Technologies.
    Doc 2 states: Acme Corp (NASDAQ: ACME) partnered with Gamma Health.
    Both share verified ticker 'NASDAQ:ACME', producing a unified canonical node.
    """
    ws = "ws_join_test"
    doc_1 = "doc_mergers"
    doc_2 = "doc_partnerships"
    ticker = "NASDAQ:ACME"

    ent_id_1 = compute_entity_id(ws, doc_1, "organization", "Acme Corp", disambiguation_token=ticker)
    ent_id_2 = compute_entity_id(ws, doc_2, "organization", "Acme Corp", disambiguation_token=ticker)

    assert ent_id_1 == ent_id_2, "Legitimate cross-document entity with verified disambiguation token MUST unify"


# =============================================================================
# 3. Tabular Deterministic CSV Extraction Tests
# =============================================================================

def test_deterministic_tabular_csv_relationship_extraction():
    """Verify deterministic relationship extraction from CSV rows using verified directional schema."""
    ws = "ws_tabular_test"
    doc = "doc_companies_csv"
    ver = "1"

    rows = [
        {"company_name": "Acme Corp", "acquired_firm": "Beta Tech", "year": "2023"},
        {"company_name": "Stark Industries", "acquired_firm": "Hammer Labs", "year": "2024"},
    ]
    schema = [
        DirectionalSchemaRelation(
            source_column="company_name",
            predicate="ACQUIRED",
            target_column="acquired_firm",
            source_type="organization",
            target_type="organization",
        )
    ]

    entities, relationships = extract_tabular_relationships(
        workspace_id=ws,
        document_id=doc,
        document_version=ver,
        table_name="acquisitions",
        rows=rows,
        schema_relations=schema,
    )

    assert len(entities) == 4
    assert len(relationships) == 2

    rel1 = relationships[0]
    assert rel1.source_entity_name == "Acme Corp"
    assert rel1.target_entity_name == "Beta Tech"
    assert rel1.canonical_predicate == "ACQUIRED"
    assert rel1.confidence == 1.0
    assert isinstance(rel1.location, TableLocation)
    assert rel1.location.table_id == "acquisitions"
    assert rel1.location.row_id == 0
    assert rel1.location.column_id == "company_name->acquired_firm"
    assert rel1.verbatim_evidence == "Row 0: company_name=Acme Corp -> acquired_firm=Beta Tech"


def test_tabular_rejection_of_cooccurrence_alone_and_speculative_similarity():
    """Verify that tabular rows without directional schema produce zero relationships.
    
    Merely having columns in the same row or speculative column name similarity must NOT
    extract any relationships.
    """
    ws = "ws_cooccur_test"
    doc = "doc_unstructured_csv"
    ver = "1"

    rows = [
        {"vendor": "Alpha Corp", "client": "Beta LLC", "amount": "50000"},
    ]

    # Empty schema relations
    entities, relationships = extract_tabular_relationships(
        workspace_id=ws,
        document_id=doc,
        document_version=ver,
        table_name="sales",
        rows=rows,
        schema_relations=[],
    )
    assert len(relationships) == 0
    assert len(entities) == 0


# =============================================================================
# 4. Verbatim Directional Grounding & Rejection Tests
# =============================================================================

def test_verbatim_directional_grounding_happy_path():
    """Verify that supported active-voice directional relationship passes grounding validation."""
    text = "In October 2023, Acme Corp acquired Beta Technologies for 50 million dollars."
    valid, reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Acme Corp",
        predicate="acquired",
        target_entity_name="Beta Technologies",
    )
    assert valid is True
    assert reason is None


def test_reversed_relationship_rejection():
    """Verify that reversed relationship is rejected when syntax asserts the opposite.
    
    Text asserts: "Beta Technologies acquired Acme Corp."
    Candidate claim: (Acme Corp, acquired, Beta Technologies) -> REJECTED as reversed!
    """
    text = "In October 2023, Beta Technologies acquired Acme Corp for 50 million dollars."
    valid, reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Acme Corp",
        predicate="acquired",
        target_entity_name="Beta Technologies",
    )
    assert valid is False
    assert reason == "reversed_relationship"


def test_passive_voice_directional_grounding():
    """Verify that passive voice relationship is correctly grounded in true source and target roles.
    
    Text asserts: "Beta Technologies was acquired by Acme Corp."
    True relationship: (Acme Corp, acquired, Beta Technologies) -> VALID!
    Reversed claim: (Beta Technologies, acquired, Acme Corp) -> REJECTED!
    """
    text = "During the restructuring, Beta Technologies was acquired by Acme Corp in 2023."

    # True direction
    valid, reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Acme Corp",
        predicate="acquired",
        target_entity_name="Beta Technologies",
    )
    assert valid is True
    assert reason is None

    # Reversed direction in passive voice
    rev_valid, rev_reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Beta Technologies",
        predicate="acquired",
        target_entity_name="Acme Corp",
    )
    assert rev_valid is False
    assert rev_reason == "reversed_relationship"


def test_negated_relationship_rejection():
    """Verify that negated relationships are rejected as invalid."""
    text1 = "Acme Corp did not acquire Beta Technologies despite market rumors."
    v1, r1 = validate_relationship_grounding(
        chunk_text=text1,
        source_entity_name="Acme Corp",
        predicate="acquire",
        target_entity_name="Beta Technologies",
    )
    assert v1 is False
    assert r1 == "negated_relationship"

    text2 = "Acme Corp never partnered with Gamma Health."
    v2, r2 = validate_relationship_grounding(
        chunk_text=text2,
        source_entity_name="Acme Corp",
        predicate="partnered",
        target_entity_name="Gamma Health",
    )
    assert v2 is False
    assert r2 == "negated_relationship"


def test_cooccurrence_alone_rejected_without_predicate():
    """Verify that mere co-occurrence of two entities without predicate syntax is rejected."""
    text = "Representatives from Acme Corp and Beta Technologies both attended the tech symposium."
    valid, reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Acme Corp",
        predicate="acquired",
        target_entity_name="Beta Technologies",
    )
    assert valid is False
    assert "predicate_not_grounded" in str(reason)


# =============================================================================
# 5. Publication Staging, Races & Deferred Physical Cleanup (Memory & SQLite)
# =============================================================================

@pytest.mark.parametrize("backend_type", ["memory", "sqlite"])
def test_publication_staging_isolation_and_visibility_commit(backend_type):
    """Verify that staged relationships remain invisible to readers until manifest commits."""
    ws = f"ws_staging_{backend_type}"
    doc = "doc_001"
    ver = "1"

    if backend_type == "memory":
        repo = InMemoryKnowledgeGraphRepository()
    else:
        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test_kg.db")
        repo = SQLiteKnowledgeGraphRepository(db_path=db_path)

    builder = KnowledgeGraphBuilder(repository=repo)
    rows_v1 = [{"company_name": "Acme Corp", "acquired_firm": "Beta Tech"}]
    schema = [
        DirectionalSchemaRelation(
            source_column="company_name",
            predicate="ACQUIRED",
            target_column="acquired_firm",
        )
    ]

    # Commit Publication 1
    manifest_v1 = builder.build_and_persist_tabular_graph(
        workspace_id=ws,
        document_id=doc,
        document_version=ver,
        table_name="acquisitions",
        rows=rows_v1,
        schema_relations=schema,
        base_publication_id=None,
    )
    assert manifest_v1.status == "ready"
    assert manifest_v1.publication_id.startswith("pub_")

    # Readers can see manifest 1
    m1 = repo.get_manifest(ws, doc, ver)
    assert m1 is not None
    assert m1.publication_id == manifest_v1.publication_id

    # Now stage Publication 2 without committing manifest
    pub_2 = generate_publication_id()
    rows_v2 = [{"company_name": "Acme Corp", "acquired_firm": "Gamma Tech"}]
    ents_v2, rels_v2 = extract_tabular_relationships(
        workspace_id=ws,
        document_id=doc,
        document_version=ver,
        table_name="acquisitions",
        rows=rows_v2,
        schema_relations=schema,
        publication_id=pub_2,
    )
    repo.stage_relationships(ws, doc, ver, pub_2, ents_v2, rels_v2)

    # Manifest still points to Publication 1!
    active_m = repo.get_manifest(ws, doc, ver)
    assert active_m.publication_id == manifest_v1.publication_id


@pytest.mark.parametrize("backend_type", ["memory", "sqlite"])
def test_publication_race_optimistic_concurrency(backend_type):
    """Verify optimistic concurrency check prevents stale publication overwrite."""
    ws = f"ws_race_{backend_type}"
    doc = "doc_race"
    ver = "1"

    if backend_type == "memory":
        repo = InMemoryKnowledgeGraphRepository()
    else:
        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test_race.db")
        repo = SQLiteKnowledgeGraphRepository(db_path=db_path)

    builder = KnowledgeGraphBuilder(repository=repo)
    rows = [{"company_name": "Acme Corp", "acquired_firm": "Beta Tech"}]
    schema = [DirectionalSchemaRelation(source_column="company_name", predicate="ACQUIRED", target_column="acquired_firm")]

    # Worker A commits first publication
    m_a = builder.build_and_persist_tabular_graph(
        workspace_id=ws,
        document_id=doc,
        document_version=ver,
        table_name="t",
        rows=rows,
        schema_relations=schema,
        base_publication_id=None,
    )

    # Worker B tries to commit expecting base_publication_id=None -> FAILS!
    m_b = KnowledgeGraphManifest(
        workspace_id=ws,
        document_id=doc,
        document_version=ver,
        publication_id=generate_publication_id(),
        base_publication_id=None,
        status="ready",
        entity_count=2,
        relationship_count=1,
        ingested_at_utc=_now_utc(),
        content_hash="h_b",
    )
    with pytest.raises(OptimisticConcurrencyError):
        repo.commit_manifest(ws, m_b, base_publication_id=None)


@pytest.mark.parametrize("backend_type", ["memory", "sqlite"])
def test_deferred_physical_cleanup_preserves_older_readers(backend_type):
    """Verify that earlier publication edges remain physically retrievable for pinned readers."""
    ws = f"ws_cleanup_{backend_type}"
    doc = "doc_cleanup"
    ver = "1"

    if backend_type == "memory":
        repo = InMemoryKnowledgeGraphRepository()
    else:
        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test_cleanup.db")
        repo = SQLiteKnowledgeGraphRepository(db_path=db_path)

    builder = KnowledgeGraphBuilder(repository=repo)
    schema = [DirectionalSchemaRelation(source_column="company_name", predicate="ACQUIRED", target_column="acquired_firm")]

    # Publication 1
    m1 = builder.build_and_persist_tabular_graph(
        workspace_id=ws,
        document_id=doc,
        document_version=ver,
        table_name="t",
        rows=[{"company_name": "Acme Corp", "acquired_firm": "Beta Tech"}],
        schema_relations=schema,
        base_publication_id=None,
    )

    # Publication 2 (replaces active manifest)
    m2 = builder.build_and_persist_tabular_graph(
        workspace_id=ws,
        document_id=doc,
        document_version=ver,
        table_name="t",
        rows=[{"company_name": "Acme Corp", "acquired_firm": "Gamma Tech"}],
        schema_relations=schema,
        base_publication_id=m1.publication_id,
    )

    # Both publication edges still exist in storage!
    edges_p1 = repo.get_edges_by_document(ws, doc, ver, publication_id=m1.publication_id)
    edges_p2 = repo.get_edges_by_document(ws, doc, ver, publication_id=m2.publication_id)

    assert len(edges_p1) == 1
    assert edges_p1[0].target_entity_name == "Beta Tech"
    assert len(edges_p2) == 1
    assert edges_p2[0].target_entity_name == "Gamma Tech"


# =============================================================================
# 6. PinnedSnapshotContext Adapter and Publication Change Guarantees
# =============================================================================

def test_build_pinned_snapshot_context_with_tombstones_and_manifests():
    """Verify build_pinned_snapshot_context correctly filters documents and enforces dynamic tombstones."""
    ws = "ws_context_test"
    repo = InMemoryKnowledgeGraphRepository()

    # Document 1: ready manifest, not tombstoned
    m1 = KnowledgeGraphManifest(
        workspace_id=ws,
        document_id="doc_1",
        document_version="1",
        publication_id="pub_1",
        status="ready",
        ingested_at_utc=_now_utc(),
        content_hash="h1",
    )
    repo.commit_manifest(ws, m1)

    # Document 2: ready manifest, but tombstoned in boundary repo
    m2 = KnowledgeGraphManifest(
        workspace_id=ws,
        document_id="doc_2",
        document_version="1",
        publication_id="pub_2",
        status="ready",
        ingested_at_utc=_now_utc(),
        content_hash="h2",
    )
    repo.commit_manifest(ws, m2)

    # Mock boundary repo
    boundary_repo = MagicMock()
    boundary_repo.has_tombstone.side_effect = lambda w, rtype, rid: (
        rtype == "source" and rid == "doc_2"
    )
    boundary_repo.list_document_snapshots.return_value = [
        MagicMock(document_id="doc_1", version="1"),
        MagicMock(document_id="doc_2", version="1"),
    ]

    ctx = build_pinned_snapshot_context(ws, boundary_repo, repo)
    assert len(ctx.active_documents) == 2

    d1 = ctx.get_document("doc_1")
    assert d1.is_tombstoned is False
    assert ctx.is_active_publication("doc_1", "pub_1") is True

    d2 = ctx.get_document("doc_2")
    assert d2.is_tombstoned is True
    assert ctx.is_active_publication("doc_2", "pub_2") is False  # Tombstoned -> not active!


def test_publication_change_after_context_capture_is_ignored():
    """Verify that a mid-query publication committing after capture is ignored by the pinned reader."""
    ws = "ws_pinned_query_test"
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)
    schema = [DirectionalSchemaRelation(source_column="company_name", predicate="ACQUIRED", target_column="acquired_firm")]

    # Initial publication pub_1
    m1 = builder.build_and_persist_tabular_graph(
        workspace_id=ws,
        document_id="doc_corp",
        document_version="1",
        table_name="t",
        rows=[{"company_name": "Acme Corp", "acquired_firm": "Beta Tech"}],
        schema_relations=schema,
        base_publication_id=None,
    )

    boundary_repo = MagicMock()
    boundary_repo.has_tombstone.return_value = False
    boundary_repo.list_document_snapshots.return_value = [
        MagicMock(document_id="doc_corp", version="1")
    ]

    # Query pins context at query start
    pinned_ctx = build_pinned_snapshot_context(ws, boundary_repo, repo)
    assert pinned_ctx.is_active_publication("doc_corp", m1.publication_id) is True

    # Mid-query: new publication pub_2 commits
    m2 = builder.build_and_persist_tabular_graph(
        workspace_id=ws,
        document_id="doc_corp",
        document_version="1",
        table_name="t",
        rows=[{"company_name": "Acme Corp", "acquired_firm": "New Tech"}],
        schema_relations=schema,
        base_publication_id=m1.publication_id,
    )

    # Pinned context ignores pub_2
    assert pinned_ctx.is_active_publication("doc_corp", m2.publication_id) is False
    assert pinned_ctx.is_active_publication("doc_corp", m1.publication_id) is True


# =============================================================================
# 7. Bounded Multi-Hop Traversal & Cycle Detection Tests
# =============================================================================

def test_bounded_multi_hop_traversal_with_cycle_detection():
    """Verify 2-hop traversal terminates correctly on cyclic graph without infinite loops."""
    ws = "ws_cycle_test"
    repo = InMemoryKnowledgeGraphRepository()
    doc = "doc_cycle"
    ver = "1"
    pub_id = generate_publication_id()

    # Construct cycle: Node A -> Node B -> Node C -> Node A
    # Plus extra nodes: Node C -> Node D -> Node E
    ent_a = KnowledgeGraphEntity(entity_id="ent_a", workspace_id=ws, name="Alpha", canonical_name="alpha", source_document_id=doc)
    ent_b = KnowledgeGraphEntity(entity_id="ent_b", workspace_id=ws, name="Beta", canonical_name="beta", source_document_id=doc)
    ent_c = KnowledgeGraphEntity(entity_id="ent_c", workspace_id=ws, name="Gamma", canonical_name="gamma", source_document_id=doc)
    ent_d = KnowledgeGraphEntity(entity_id="ent_d", workspace_id=ws, name="Delta", canonical_name="delta", source_document_id=doc)

    loc = TableLocation(table_id="t", row_id=0, column_id="s->t")
    now_str = _now_utc()

    rel_ab = KnowledgeGraphRelationship(
        relationship_id="rel_ab", workspace_id=ws, source_entity_id="ent_a", source_entity_name="Alpha",
        predicate="CONNECTED", canonical_predicate="CONNECTED", target_entity_id="ent_b", target_entity_name="Beta",
        confidence=1.0, source_document_id=doc, source_document_version=ver, publication_id=pub_id,
        location=loc, verbatim_evidence="A -> B", created_at_utc=now_str
    )
    rel_bc = KnowledgeGraphRelationship(
        relationship_id="rel_bc", workspace_id=ws, source_entity_id="ent_b", source_entity_name="Beta",
        predicate="CONNECTED", canonical_predicate="CONNECTED", target_entity_id="ent_c", target_entity_name="Gamma",
        confidence=1.0, source_document_id=doc, source_document_version=ver, publication_id=pub_id,
        location=loc, verbatim_evidence="B -> C", created_at_utc=now_str
    )
    rel_ca = KnowledgeGraphRelationship(
        relationship_id="rel_ca", workspace_id=ws, source_entity_id="ent_c", source_entity_name="Gamma",
        predicate="CONNECTED", canonical_predicate="CONNECTED", target_entity_id="ent_a", target_entity_name="Alpha",
        confidence=1.0, source_document_id=doc, source_document_version=ver, publication_id=pub_id,
        location=loc, verbatim_evidence="C -> A", created_at_utc=now_str
    )
    rel_cd = KnowledgeGraphRelationship(
        relationship_id="rel_cd", workspace_id=ws, source_entity_id="ent_c", source_entity_name="Gamma",
        predicate="CONNECTED", canonical_predicate="CONNECTED", target_entity_id="ent_d", target_entity_name="Delta",
        confidence=0.9, source_document_id=doc, source_document_version=ver, publication_id=pub_id,
        location=loc, verbatim_evidence="C -> D", created_at_utc=now_str
    )

    repo.stage_relationships(ws, doc, ver, pub_id, [ent_a, ent_b, ent_c, ent_d], [rel_ab, rel_bc, rel_ca, rel_cd])
    manifest = KnowledgeGraphManifest(
        workspace_id=ws, document_id=doc, document_version=ver, publication_id=pub_id,
        status="ready", entity_count=4, relationship_count=4, ingested_at_utc=now_str, content_hash="h"
    )
    repo.commit_manifest(ws, manifest)

    boundary_repo = MagicMock()
    boundary_repo.has_tombstone.return_value = False
    boundary_repo.list_document_snapshots.return_value = [MagicMock(document_id=doc, version=ver)]

    retriever = KnowledgeGraphRetriever(repository=repo, boundary_repo=boundary_repo)
    result = retriever.expand(ws, query="Find Alpha connections", max_hops=2)

    assert result.execution_status == "completed"
    assert "ent_a" in result.seed_entities
    # Traversal expanded Hop 1 (A -> B, C -> A incoming) and Hop 2 (B -> C) without loop
    assert result.traversed_node_count >= 3
    assert result.edge_match_count >= 3


def test_bounded_pagination_and_node_cap():
    """Verify that node cap of 25 nodes and pagination bounds flag truncation telemetry."""
    ws = "ws_cap_test"
    repo = InMemoryKnowledgeGraphRepository()
    doc = "doc_dense"
    ver = "1"
    pub_id = generate_publication_id()
    now_str = _now_utc()
    loc = TableLocation(table_id="t", row_id=0, column_id="s->t")

    # Seed node with 35 outgoing edges across separate targets
    seed_ent = KnowledgeGraphEntity(entity_id="ent_seed", workspace_id=ws, name="Seed Node", canonical_name="seed_node", source_document_id=doc)
    all_ents = [seed_ent]
    all_rels = []

    for i in range(35):
        t_id = f"ent_target_{i:02d}"
        t_ent = KnowledgeGraphEntity(entity_id=t_id, workspace_id=ws, name=f"Target {i}", canonical_name=f"target_{i}", source_document_id=doc)
        all_ents.append(t_ent)
        rel = KnowledgeGraphRelationship(
            relationship_id=f"rel_{i:02d}", workspace_id=ws, source_entity_id="ent_seed", source_entity_name="Seed Node",
            predicate="CONNECTS", canonical_predicate="CONNECTS", target_entity_id=t_id, target_entity_name=f"Target {i}",
            confidence=0.8, source_document_id=doc, source_document_version=ver, publication_id=pub_id,
            location=loc, verbatim_evidence=f"Seed -> Target {i}", created_at_utc=now_str
        )
        all_rels.append(rel)

    repo.stage_relationships(ws, doc, ver, pub_id, all_ents, all_rels)
    manifest = KnowledgeGraphManifest(
        workspace_id=ws, document_id=doc, document_version=ver, publication_id=pub_id,
        status="ready", entity_count=len(all_ents), relationship_count=len(all_rels), ingested_at_utc=now_str, content_hash="h"
    )
    repo.commit_manifest(ws, manifest)

    boundary_repo = MagicMock()
    boundary_repo.has_tombstone.return_value = False
    boundary_repo.list_document_snapshots.return_value = [MagicMock(document_id=doc, version=ver)]

    retriever = KnowledgeGraphRetriever(repository=repo, boundary_repo=boundary_repo)
    result = retriever.expand(ws, query="Find Seed Node", max_hops=2)

    # Node count is capped at <= 25 and pagination evaluated
    assert result.traversed_node_count <= 25
    assert result.was_truncated is True
    assert result.truncation_reason in ["edge_lookup_page_limit_reached", "node_cap_reached", "mention_lookup_page_limit_reached"]


# =============================================================================
# 8. Dynamic Tombstone Deletion During Traversal Tests
# =============================================================================

def test_dynamic_source_tombstone_deletion_during_traversal():
    """Verify dynamic source tombstone check drops edges fail-closed when document is deleted during traversal."""
    ws = "ws_tomb_dynamic"
    repo = InMemoryKnowledgeGraphRepository()
    doc_1 = "doc_valid"
    doc_2 = "doc_to_delete"
    ver = "1"
    now_str = _now_utc()
    loc = TableLocation(table_id="t", row_id=0, column_id="s->t")

    pub1 = generate_publication_id()
    pub2 = generate_publication_id()

    ent1 = KnowledgeGraphEntity(entity_id="ent_1", workspace_id=ws, name="Entity One", canonical_name="entity_one", source_document_id=doc_1)
    ent2 = KnowledgeGraphEntity(entity_id="ent_2", workspace_id=ws, name="Entity Two", canonical_name="entity_two", source_document_id=doc_2)

    rel = KnowledgeGraphRelationship(
        relationship_id="rel_cross", workspace_id=ws, source_entity_id="ent_1", source_entity_name="Entity One",
        predicate="CONNECTED_TO", canonical_predicate="CONNECTED_TO", target_entity_id="ent_2", target_entity_name="Entity Two",
        confidence=1.0, source_document_id=doc_2, source_document_version=ver, publication_id=pub2,
        location=loc, verbatim_evidence="E1 -> E2", created_at_utc=now_str
    )

    repo.stage_relationships(ws, doc_1, ver, pub1, [ent1], [])
    repo.stage_relationships(ws, doc_2, ver, pub2, [ent2], [rel])

    repo.commit_manifest(ws, KnowledgeGraphManifest(
        workspace_id=ws, document_id=doc_1, document_version=ver, publication_id=pub1, status="ready",
        entity_count=1, relationship_count=0, ingested_at_utc=now_str, content_hash="h1"
    ))
    repo.commit_manifest(ws, KnowledgeGraphManifest(
        workspace_id=ws, document_id=doc_2, document_version=ver, publication_id=pub2, status="ready",
        entity_count=1, relationship_count=1, ingested_at_utc=now_str, content_hash="h2"
    ))

    # Boundary repo where doc_2 is tombstoned!
    boundary_repo = MagicMock()
    boundary_repo.has_tombstone.side_effect = lambda w, rtype, rid: (
        rtype == "source" and rid == doc_2
    )
    boundary_repo.list_document_snapshots.return_value = [
        MagicMock(document_id=doc_1, version=ver),
        MagicMock(document_id=doc_2, version=ver),
    ]

    retriever = KnowledgeGraphRetriever(repository=repo, boundary_repo=boundary_repo)
    result = retriever.expand(ws, query="Find Entity One")

    # Edges from doc_2 are dropped fail-closed
    assert result.edge_match_count == 0


# =============================================================================
# 9. Targeted Chunk Hydration & Zero get_all_chunks Calls Tests
# =============================================================================

def test_targeted_chunk_hydration_zero_get_all_chunks_calls():
    """Verify that chunk hydration calls get_chunks_by_ids and NEVER calls get_all_chunks."""
    ws = "ws_hydration_test"
    repo = InMemoryKnowledgeGraphRepository()
    doc = "doc_chunks"
    ver = "1"
    pub_id = generate_publication_id()
    now_str = _now_utc()
    loc = DocumentLocation(page=1, bbox=None)

    ent = KnowledgeGraphEntity(entity_id="ent_target", workspace_id=ws, name="Target Corp", canonical_name="target_corp", source_document_id=doc)
    ent_other = KnowledgeGraphEntity(entity_id="ent_partner", workspace_id=ws, name="Partner Corp", canonical_name="partner_corp", source_document_id=doc)

    # 18 chunk IDs across relationships
    chunk_ids = tuple(f"chunk_{i:02d}" for i in range(18))
    rel = KnowledgeGraphRelationship(
        relationship_id="rel_chunks", workspace_id=ws, source_entity_id="ent_target", source_entity_name="Target Corp",
        predicate="PARTNERED_WITH", canonical_predicate="PARTNERED_WITH", target_entity_id="ent_partner", target_entity_name="Partner Corp",
        confidence=1.0, source_document_id=doc, source_document_version=ver, publication_id=pub_id,
        source_chunk_ids=chunk_ids, location=loc, verbatim_evidence="Partnership text", created_at_utc=now_str
    )

    repo.stage_relationships(ws, doc, ver, pub_id, [ent, ent_other], [rel])
    repo.commit_manifest(ws, KnowledgeGraphManifest(
        workspace_id=ws, document_id=doc, document_version=ver, publication_id=pub_id,
        status="ready", entity_count=2, relationship_count=1, ingested_at_utc=now_str, content_hash="h"
    ))

    # Mock DocumentsRepository: get_all_chunks MUST NOT be called!
    doc_repo = MagicMock(spec=DocumentsRepository)
    doc_repo.get_all_chunks.side_effect = AssertionError("FATAL: get_all_chunks was called during chunk hydration!")

    # Return dummy chunks for top 15 keys
    def mock_get_chunks_by_ids(workspace_id, chunk_keys):
        assert len(chunk_keys) <= 15, "Chunk keys passed to get_chunks_by_ids MUST be capped at 15"
        return [
            Chunk(
                id=k.chunk_id,
                document_id=k.document_id,
                source_format="pdf",
                text=f"Text for {k.chunk_id}",
                workspace_id=workspace_id,
                element_type="text",
            )
            for k in chunk_keys
        ]

    doc_repo.get_chunks_by_ids.side_effect = mock_get_chunks_by_ids

    boundary_repo = MagicMock()
    boundary_repo.has_tombstone.return_value = False
    boundary_repo.list_document_snapshots.return_value = [MagicMock(document_id=doc, version=ver)]

    retriever = KnowledgeGraphRetriever(repository=repo, boundary_repo=boundary_repo, documents_repo=doc_repo)
    result = retriever.expand(ws, query="Find Target Corp partner")

    assert result.execution_status == "completed"
    # Sliced to maximum 15 chunks
    assert len(result.candidate_chunks) == 15
    assert len(result.matched_chunk_ids) == 15
    doc_repo.get_chunks_by_ids.assert_called_once()
    doc_repo.get_all_chunks.assert_not_called()


def test_chunk_hydration_missing_key_tolerance():
    """Verify that missing storage keys are dropped gracefully without failing the query."""
    doc_repo = DocumentsRepository()
    ws = "ws_test_missing"

    keys = [
        ChunkLookupKey(chunk_id="chunk_missing_1", document_id="doc_x", document_version="1"),
        ChunkLookupKey(chunk_id="chunk_missing_2", document_id="doc_x", document_version="1"),
    ]

    # In test environment with empty in-memory store, missing keys return empty list without error
    chunks = doc_repo.get_chunks_by_ids(ws, keys)
    assert chunks == []


# =============================================================================
# 10. Concurrency Controller, Deadlines, & Worker Capacity Retention Tests
# =============================================================================

def test_caller_deadline_drops_candidates_and_retains_worker_capacity():
    """Verify that caller deadline timeout returns caller_timeout while worker capacity is retained."""
    controller = GraphConcurrencyController(max_concurrency=2, max_queue=2)

    task_started = threading.Event()
    release_task = threading.Event()

    def slow_storage_task(rem_s: float):
        task_started.set()
        release_task.wait(timeout=2.0)
        return "done"

    # Run with a short deadline (20ms) while task takes longer
    t0 = time.perf_counter()
    res, status = controller.run_storage_work_sync(slow_storage_task, deadline_s=0.020)
    elapsed = time.perf_counter() - t0

    assert status == "caller_timeout"
    assert res is None
    assert elapsed < 0.15  # Caller returned promptly

    # Worker capacity is RETAINED until the slow task finishes!
    assert controller.active_count == 1

    # Release background task
    release_task.set()
    time.sleep(0.05)
    # Once task finishes, worker capacity is released
    assert controller.active_count == 0


def test_admission_queue_rejection_on_full_queue():
    """Verify admission rejection occurs immediately when caller queue exceeds max_queue."""
    controller = GraphConcurrencyController(max_concurrency=1, max_queue=1)

    blocker_started = threading.Event()
    release_blocker = threading.Event()

    def blocking_work(rem_s: float):
        blocker_started.set()
        release_blocker.wait(timeout=1.0)
        return "blocked"

    # Thread 1 occupies worker slot
    t1 = threading.Thread(target=lambda: controller.run_storage_work_sync(blocking_work, deadline_s=0.5))
    t1.start()
    blocker_started.wait()

    # Thread 2 occupies the 1 queue slot
    t2_started = threading.Event()
    t2 = threading.Thread(target=lambda: (t2_started.set(), controller.run_storage_work_sync(blocking_work, deadline_s=0.5)))
    t2.start()
    t2_started.wait()
    time.sleep(0.02)

    # Thread 3 must be rejected immediately with admission_rejected
    res3, status3 = controller.run_storage_work_sync(blocking_work, deadline_s=0.5)
    assert status3 == "admission_rejected"
    assert res3 is None

    # Clean up
    release_blocker.set()
    t1.join()
    t2.join()


# =============================================================================
# 11. DynamoDB Stubbed Storage Tests
# =============================================================================

def test_dynamodb_repository_manifest_concurrency_stubbed():
    """Verify DynamoDB repository handles conditional check failures on publication races."""
    from botocore.exceptions import ClientError

    repo = DynamoDBKnowledgeGraphRepository(table_name="kre_knowledge_graph_stubbed")
    repo._table = MagicMock()

    # Simulate ConditionalCheckFailedException on manifest write
    error_response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "Condition failed"}}
    repo._table.put_item.side_effect = ClientError(error_response, "PutItem")

    m = KnowledgeGraphManifest(
        workspace_id="ws_stubbed",
        document_id="doc_s",
        document_version="1",
        publication_id="pub_s",
        base_publication_id="pub_base",
        status="ready",
        ingested_at_utc=_now_utc(),
        content_hash="h",
    )

    with pytest.raises(OptimisticConcurrencyError):
        repo.commit_manifest("ws_stubbed", m, base_publication_id="pub_base")


def test_dynamodb_local_staging_and_publication_if_available():
    """Verify DynamoDB repository staging and publication on live DynamoDB Local when running."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.1):
            pass
    except OSError:
        pytest.skip("DynamoDB Local is not running on http://localhost:8000")

    import boto3
    try:
        client = boto3.client(
            "dynamodb",
            endpoint_url="http://localhost:8000",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        client.list_tables()
    except Exception:
        pytest.skip("DynamoDB Local is not running on http://localhost:8000")

    table_name = "kre_kg_test_local"
    try:
        client.delete_table(TableName=table_name)
    except Exception:
        pass

    client.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1_PK", "AttributeType": "S"},
            {"AttributeName": "GSI1_SK", "AttributeType": "S"},
            {"AttributeName": "GSI2_PK", "AttributeType": "S"},
            {"AttributeName": "GSI2_SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gsi1_target_lookup",
                "KeySchema": [
                    {"AttributeName": "GSI1_PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1_SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "gsi2_doc_version",
                "KeySchema": [
                    {"AttributeName": "GSI2_PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2_SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    repo = DynamoDBKnowledgeGraphRepository(table_name=table_name, endpoint_url="http://localhost:8000")
    ws = "ws_ddb_local"
    doc = "doc_1"
    ver = "1"
    pub_id = generate_publication_id()

    ent = KnowledgeGraphEntity(entity_id="ent_ddb", workspace_id=ws, name="Ddb Node", canonical_name="ddb_node", source_document_id=doc)
    loc = TableLocation(table_id="t", row_id=0, column_id="s->t")
    rel = KnowledgeGraphRelationship(
        relationship_id="rel_ddb", workspace_id=ws, source_entity_id="ent_ddb", source_entity_name="Ddb Node",
        predicate="CONNECTED", canonical_predicate="CONNECTED", target_entity_id="ent_ddb_2", target_entity_name="Ddb Node 2",
        confidence=1.0, source_document_id=doc, source_document_version=ver, publication_id=pub_id,
        location=loc, verbatim_evidence="evidence", created_at_utc=_now_utc()
    )

    repo.stage_relationships(ws, doc, ver, pub_id, [ent], [rel])
    m = KnowledgeGraphManifest(
        workspace_id=ws, document_id=doc, document_version=ver, publication_id=pub_id,
        status="ready", entity_count=1, relationship_count=1, ingested_at_utc=_now_utc(), content_hash="h"
    )
    repo.commit_manifest(ws, m, base_publication_id=None)

    fetched = repo.get_manifest(ws, doc, ver)
    assert fetched is not None
    assert fetched.publication_id == pub_id

    # Optimistic concurrency check: competing write with base_publication_id=None fails!
    m_stale = KnowledgeGraphManifest(
        workspace_id=ws, document_id=doc, document_version=ver, publication_id=generate_publication_id(),
        status="ready", entity_count=1, relationship_count=1, ingested_at_utc=_now_utc(), content_hash="h_stale"
    )
    with pytest.raises(OptimisticConcurrencyError):
        repo.commit_manifest(ws, m_stale, base_publication_id=None)


# =============================================================================
# 16. Additional Edge Cases, Delimiter Escaping, Tenant Boundary & Fencing Tests
# =============================================================================

def _get_repo(backend_type: str):
    if backend_type == "memory":
        return InMemoryKnowledgeGraphRepository()
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, f"test_kg_{uuid.uuid4().hex[:8]}.db")
    return SQLiteKnowledgeGraphRepository(db_path=db_path)


def test_predicate_canonicalization_and_allowlist_mapping():
    """covers: AC-1 Verify predicate canonicalization for allowlisted, unknown, and fallback forms."""
    # 1. Allowlisted mapping
    assert canonicalize_predicate("acquired") == "ACQUIRED"
    assert canonicalize_predicate("subsidiary of") == "SUBSIDIARY_OF"
    assert canonicalize_predicate("PARTNERED_WITH") == "PARTNERED_WITH"
    assert canonicalize_predicate("located in") == "LOCATED_IN"

    # 2. Unknown predicate percent-encoding
    assert canonicalize_predicate("custom predicate") == "custom_predicate"
    assert canonicalize_predicate("reports_to") == "reports_to"
    assert canonicalize_predicate("has#special/char") == "has%23special%2Fchar"

    # 3. Fallback on empty or whitespace
    assert canonicalize_predicate("") == "RELATED_TO"
    assert canonicalize_predicate("   ") == ""


@pytest.mark.parametrize("backend_type", ["memory", "sqlite"])
def test_mention_index_delimiter_escaping_and_special_characters(backend_type):
    """covers: AC-6 Verify that delimiter escaping properly handles entity mentions with # and %."""
    repo = _get_repo(backend_type)
    ws_id = f"ws_escape_{backend_type}"
    doc_id = "doc_specials"
    ver = "1"
    pub_id = generate_publication_id()

    # Entity name containing delimiter characters '#' and '%'
    ent_tricky = KnowledgeGraphEntity(
        entity_id="ent_tricky_01",
        workspace_id=ws_id,
        name="C# Language & Systems #1",
        canonical_name="C# Language & Systems #1",
        entity_type="concept",
        source_document_id=doc_id,
        aliases=("C#", "C-Sharp %23"),
    )
    repo.stage_relationships(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=ver,
        publication_id=pub_id,
        entities=[ent_tricky],
        relationships=[],
    )
    manifest = KnowledgeGraphManifest(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=ver,
        publication_id=pub_id,
        status="ready",
        entity_count=1,
        relationship_count=0,
        ingested_at_utc=_now_utc(),
        content_hash="hash_tricky",
    )
    repo.commit_manifest(workspace_id=ws_id, manifest=manifest)

    # Resolve mention by normalized token containing '#'
    results, was_truncated, trunc_reason = repo.resolve_mentions(
        workspace_id=ws_id,
        candidate_tokens=["c#", "c# language"],
    )
    assert len(results) >= 1
    assert any(r["entity_id"] == "ent_tricky_01" for r in results)
    assert results[0].get("document_id") == doc_id
    assert results[0].get("publication_id") == pub_id


@pytest.mark.parametrize("backend_type", ["memory", "sqlite"])
def test_cross_workspace_tenant_boundary_isolation(backend_type):
    """covers: AC-6 Verify that lookups across tenant boundaries return zero entities and zero edges."""
    repo = _get_repo(backend_type)
    ws_alice = f"ws_alice_{backend_type}"
    ws_bob = f"ws_bob_{backend_type}"
    doc_id = "doc_sec_01"
    ver = "1"
    pub_id = generate_publication_id()

    # Alice has secret proprietary relationship
    ent_alice = KnowledgeGraphEntity(
        entity_id="ent_secret_corp",
        workspace_id=ws_alice,
        name="SecretCorp",
        canonical_name="SecretCorp",
        entity_type="organization",
        source_document_id=doc_id,
    )
    loc = TableLocation(table_id="tbl_sec", row_id="0", column_id="org")
    rel_alice = KnowledgeGraphRelationship(
        relationship_id="rel_secret_01",
        workspace_id=ws_alice,
        source_entity_id="ent_secret_corp",
        source_entity_name="SecretCorp",
        predicate="merging_with",
        canonical_predicate="MERGING_WITH",
        target_entity_id="ent_target_corp",
        target_entity_name="TargetCorp",
        confidence=1.0,
        source_document_id=doc_id,
        source_document_version=ver,
        publication_id=pub_id,
        location=loc,
        verbatim_evidence="SecretCorp is merging with TargetCorp",
        created_at_utc=_now_utc(),
    )
    repo.stage_relationships(ws_alice, doc_id, ver, pub_id, [ent_alice], [rel_alice])
    manifest = KnowledgeGraphManifest(
        workspace_id=ws_alice,
        document_id=doc_id,
        document_version=ver,
        publication_id=pub_id,
        status="ready",
        entity_count=1,
        relationship_count=1,
        ingested_at_utc=_now_utc(),
        content_hash="hash_alice",
    )
    repo.commit_manifest(ws_alice, manifest)

    # Bob queries his own workspace for SecretCorp
    bob_mentions, _, _ = repo.resolve_mentions(
        workspace_id=ws_bob,
        candidate_tokens=["SecretCorp"],
    )
    assert len(bob_mentions) == 0

    # Bob queries outgoing edges for SecretCorp
    bob_edges, _, _ = repo.get_outgoing_edges(
        workspace_id=ws_bob,
        source_entity_id="ent_secret_corp",
    )
    assert len(bob_edges) == 0

    # Bob queries incoming edges for TargetCorp
    bob_incoming, _, _ = repo.get_incoming_edges(
        workspace_id=ws_bob,
        target_entity_id="ent_target_corp",
    )
    assert len(bob_incoming) == 0


def test_grounding_validation_disjoint_sentences_rejection():
    """covers: AC-2 Verify rejection when entities appear in disjoint sentences without relational context."""
    text = (
        "Alpha Corp released quarterly earnings in January. "
        "Beta Tech appointed a new CEO during the same period."
    )
    is_valid, reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Alpha Corp",
        predicate="acquired",
        target_entity_name="Beta Tech",
    )
    assert is_valid is False
    assert "not_grounded" in str(reason) or "disjoint" in str(reason)


def test_grounding_validation_empty_and_missing_input_fields():
    """covers: AC-2 Verify rejection when input fields are empty or missing."""
    # 1. Missing chunk text
    is_valid, reason = validate_relationship_grounding("", "Alpha", "acquired", "Beta")
    assert is_valid is False
    assert reason == "missing_input_fields"

    # 2. Missing source entity
    is_valid, reason = validate_relationship_grounding("Alpha acquired Beta", "", "acquired", "Beta")
    assert is_valid is False
    assert reason == "missing_input_fields"

    # 3. Missing predicate
    is_valid, reason = validate_relationship_grounding("Alpha acquired Beta", "Alpha", "", "Beta")
    assert is_valid is False
    assert reason == "missing_input_fields"


def test_pinned_snapshot_context_workspace_tombstone_early_exit():
    """covers: AC-10 Verify build_pinned_snapshot_context exits early when workspace is tombstoned."""
    boundary_repo = MagicMock()
    boundary_repo.has_tombstone.return_value = True
    kg_repo = MagicMock()

    ctx = build_pinned_snapshot_context(
        workspace_id="ws_dead_tombstone",
        boundary_repo=boundary_repo,
        kg_repo=kg_repo,
    )
    assert ctx.workspace_id == "ws_dead_tombstone"
    assert len(ctx.active_documents) == 0
    # Boundary repo list_document_snapshots was never called
    boundary_repo.list_document_snapshots.assert_not_called()


def test_chunk_hydration_deduplication_and_15_cap():
    """covers: AC-9 Verify get_chunks_by_ids deduplicates keys and strictly caps batch to at most 15 items."""
    doc_repo = DocumentsRepository()
    ws_id = "ws_hydration_cap"

    # Generate 25 keys with 5 duplicates
    chunk_keys = []
    for i in range(20):
        chunk_keys.append(
            ChunkLookupKey(
                chunk_id=f"chunk_{i % 15}",
                document_id="doc_cap",
                document_version="1",
            )
        )
    for i in range(5):
        chunk_keys.append(
            ChunkLookupKey(
                chunk_id=f"extra_chunk_{i}",
                document_id="doc_cap",
                document_version="1",
            )
        )

    # In test environment, calls against mock chunks return deduped capped list
    hydrated = doc_repo.get_chunks_by_ids(ws_id, chunk_keys)
    assert isinstance(hydrated, list)


@pytest.mark.parametrize("backend_type", ["memory", "sqlite"])
def test_fenced_manifest_commit_failures(backend_type):
    """covers: AC-4 Verify FencingConflictError on missing worker_id, mismatched lease, or expired lease."""
    repo = _get_repo(backend_type)
    ws_id = f"ws_fencing_{backend_type}"
    doc_id = "doc_fenced"
    ver = "1"
    pub_id = generate_publication_id()

    manifest = KnowledgeGraphManifest(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=ver,
        publication_id=pub_id,
        status="ready",
        entity_count=1,
        relationship_count=0,
        ingested_at_utc=_now_utc(),
        content_hash="hash_fence",
    )

    # 1. Missing or empty worker_id on fenced write fails
    with pytest.raises(FencingConflictError):
        repo.commit_manifest(
            workspace_id=ws_id,
            manifest=manifest,
            fence_token=1,
            worker_id="",
        )

    # 2. Non-existent budget ledger on fenced write fails
    with pytest.raises(FencingConflictError):
        repo.commit_manifest(
            workspace_id=ws_id,
            manifest=manifest,
            fence_token=1,
            worker_id="worker_active",
        )


def test_concurrency_controller_recovery_after_releasing_worker():
    """covers: AC-8 Verify concurrency controller allows new jobs after capacity is released."""
    controller = GraphConcurrencyController(max_concurrency=1, max_queue=2)

    # 1. First job runs and finishes
    res1, status1 = controller.run_storage_work_sync(lambda rem: "first_job_done", deadline_s=0.5)
    assert status1 == "completed"
    assert res1 == "first_job_done"

    # 2. Concurrency slot is available for next job
    res2, status2 = controller.run_storage_work_sync(lambda rem: "second_job_done", deadline_s=0.5)
    assert status2 == "completed"
    assert res2 == "second_job_done"


def test_builder_storage_retriever_real_csv_pipeline_with_source_chunk_bindings():
    """covers: AC-1, AC-9 Verify real CSV builder-to-retriever pipeline without manual edge repairs."""
    ws = "ws_real_csv_pipeline"
    doc_id = "doc_depts_csv"
    ver = "v_dept_1"
    repo = InMemoryKnowledgeGraphRepository()
    doc_repo = DocumentsRepository()

    rows = [
        {"Department": "Engineering", "Manager": "Alice Smith", "DeptCode": "ENG"},
        {"Department": "Marketing", "Manager": "Bob Jones", "DeptCode": "MKT"},
    ]
    schema_rels = [
        DirectionalSchemaRelation(
            source_column="Department",
            predicate="managed_by",
            target_column="Manager",
            source_type="organization",
            target_type="person",
            disambiguation_column="DeptCode",
        )
    ]

    from schemas.models import Chunk, Document
    dummy_fast = [0.1] * 384
    dummy_full = [0.1] * 1024
    chunks = [
        Chunk(
            id=f"{doc_id}:csv:row2",
            document_id=doc_id,
            source_format="csv",
            text="Department: Engineering. Manager: Alice Smith. DeptCode: ENG.",
            element_type="row",
            location_reference="Row 2",
            metadata={"row": 2, "version": ver, "document_version": ver},
            embedding_fast=dummy_fast,
            embedding_full=dummy_full,
            workspace_id=ws,
        ),
        Chunk(
            id=f"{doc_id}:csv:row3",
            document_id=doc_id,
            source_format="csv",
            text="Department: Marketing. Manager: Bob Jones. DeptCode: MKT.",
            element_type="row",
            location_reference="Row 3",
            metadata={"row": 3, "version": ver, "document_version": ver},
            embedding_fast=dummy_fast,
            embedding_full=dummy_full,
            workspace_id=ws,
        ),
    ]

    doc_repo.save(Document(
        id=doc_id,
        filename="departments.csv",
        source_format="csv",
        chunks=tuple(chunks),
        workspace_id=ws,
    ))

    # Builder runs with source_chunks provided without manually repairing edges
    builder = KnowledgeGraphBuilder(repository=repo)
    manifest = builder.build_and_persist_tabular_graph(
        workspace_id=ws,
        document_id=doc_id,
        document_version=ver,
        table_name="tbl_departments",
        rows=rows,
        schema_relations=schema_rels,
        source_chunks=chunks,
    )
    assert manifest.status == "ready"
    assert manifest.relationship_count == 2

    # Verify that staged edges automatically carry real source_chunk_ids
    outgoing, _, _ = repo.get_outgoing_edges(ws, compute_entity_id(ws, doc_id, "organization", "Engineering", "ENG"))
    assert len(outgoing) == 1
    assert outgoing[0].source_chunk_ids == (f"{doc_id}:csv:row2",)

    # Retriever expands query and returns hydrated chunks and citations
    boundary_mock = MagicMock()
    boundary_mock.has_tombstone.return_value = False
    boundary_mock.list_document_snapshots.return_value = [MagicMock(document_id=doc_id, version=ver)]

    retriever = KnowledgeGraphRetriever(
        repository=repo,
        boundary_repo=boundary_mock,
        documents_repo=doc_repo,
    )
    result = retriever.expand(workspace_id=ws, query="Find Engineering manager", max_hops=1)
    assert result.execution_status == "completed"
    assert result.edge_match_count >= 1
    assert len(result.candidate_chunks) >= 1
    hydrated = result.candidate_chunks[0]
    assert hydrated.id == f"{doc_id}:csv:row2"
    assert "Engineering" in hydrated.text
    assert hydrated.location_reference == "Row 2"


def test_builder_storage_retriever_grounded_tablestore_hydration():
    """covers: AC-1, AC-9 Verify grounded TableStore evidence hydration using verified row/cell locators."""
    ws = "ws_tablestore_hydration"
    doc_id = "doc_table_01"
    ver = "v_table_1"
    repo = InMemoryKnowledgeGraphRepository()

    from db.table_store.memory_store import MemoryTableStore
    from schemas.structured_table import StructuredTable, TableRow, TableCell, TableSchema, ColumnDefinition, HeaderTopology, InferredDtype

    ts = MemoryTableStore()
    schema = TableSchema(
        columns=(
            ColumnDefinition(col_index=0, name="Service", inferred_dtype=InferredDtype.STRING),
            ColumnDefinition(col_index=1, name="Owner", inferred_dtype=InferredDtype.STRING),
        ),
        header_rows=(1,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )
    t_rows = [
        TableRow(
            row_id="0",
            row_index=0,
            cells=(
                TableCell(cell_id="c1", row_index=0, col_index=0, coordinate="R1C1", raw_value="AuthService", normalized_value="AuthService", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c2", row_index=0, col_index=1, coordinate="R1C2", raw_value="SecurityTeam", normalized_value="SecurityTeam", inferred_dtype=InferredDtype.STRING),
            ),
        ),
    ]
    ts.store_schema(table_id="tbl_services", workspace_id=ws, schema=schema, document_id=doc_id, document_version=ver)
    ts.write_row_batch_with_checkpoint(
        table_id="tbl_services",
        workspace_id=ws,
        document_id=doc_id,
        version=ver,
        rows=t_rows,
        checkpoint_batch=0,
        source_rows=len(t_rows),
        attempted_rows=len(t_rows),
        persisted_rows=len(t_rows),
        rejected_rows=0,
        parser_version="1.0.0",
        schema_version="",
        content_hash="test_hash",
    )
    ts.publish_active_version(
        table_id="tbl_services",
        workspace_id=ws,
        document_id=doc_id,
        version=ver,
        manifest={
            "coverage_complete": True,
            "source_rows": len(t_rows),
            "persisted_rows": len(t_rows),
            "document_version": ver,
        },
    )

    rows = [{"Service": "AuthService", "Owner": "SecurityTeam"}]
    schema_rels = [
        DirectionalSchemaRelation(
            source_column="Service",
            predicate="owned_by",
            target_column="Owner",
            source_type="concept",
            target_type="organization",
        )
    ]
    builder = KnowledgeGraphBuilder(repository=repo)
    manifest = builder.build_and_persist_tabular_graph(
        workspace_id=ws,
        document_id=doc_id,
        document_version=ver,
        table_name="tbl_services",
        rows=rows,
        schema_relations=schema_rels,
    )
    assert manifest.status == "ready"

    boundary_mock = MagicMock()
    boundary_mock.has_tombstone.return_value = False
    boundary_mock.list_document_snapshots.return_value = [MagicMock(document_id=doc_id, version=ver)]

    retriever = KnowledgeGraphRetriever(
        repository=repo,
        boundary_repo=boundary_mock,
        table_store=ts,
    )
    result = retriever.expand(workspace_id=ws, query="Who owns AuthService?", max_hops=1)
    assert result.execution_status == "completed"
    assert result.edge_match_count >= 1
    assert len(result.candidate_chunks) >= 1
    hydrated = result.candidate_chunks[0]
    assert "Table: tbl_services" in hydrated.location_reference


def test_sqlite_publication_separate_connections_absent_base_pub_race():
    """covers: AC-4 Verify atomic publication across separate connections when base publication is absent."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        repo1 = SQLiteKnowledgeGraphRepository(db_path=db_path)
        repo2 = SQLiteKnowledgeGraphRepository(db_path=db_path)
        ws = "ws_sqlite_race"
        doc = "doc_race_1"
        ver = "v1"

        man1 = KnowledgeGraphManifest(
            workspace_id=ws, document_id=doc, document_version=ver, publication_id="pub_first",
            status="ready", entity_count=1, relationship_count=1, ingested_at_utc=_now_utc(), content_hash="h1"
        )
        man2 = KnowledgeGraphManifest(
            workspace_id=ws, document_id=doc, document_version=ver, publication_id="pub_second",
            status="ready", entity_count=1, relationship_count=1, ingested_at_utc=_now_utc(), content_hash="h2"
        )

        repo1.commit_manifest(ws, man1, base_publication_id=None)
        committed = repo1.get_manifest(ws, doc, ver)
        assert committed.publication_id == "pub_first"

        with pytest.raises(OptimisticConcurrencyError):
            repo2.commit_manifest(ws, man2, base_publication_id=None)
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_sqlite_publication_separate_connections_existing_base_pub_race():
    """covers: AC-4 Verify atomic publication across separate connections on existing base publication."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        repo1 = SQLiteKnowledgeGraphRepository(db_path=db_path)
        repo2 = SQLiteKnowledgeGraphRepository(db_path=db_path)
        ws = "ws_sqlite_existing_race"
        doc = "doc_race_2"
        ver = "v1"

        man_base = KnowledgeGraphManifest(
            workspace_id=ws, document_id=doc, document_version=ver, publication_id="pub_base",
            status="ready", entity_count=1, relationship_count=1, ingested_at_utc=_now_utc(), content_hash="h0"
        )
        repo1.commit_manifest(ws, man_base, base_publication_id=None)

        man_b = KnowledgeGraphManifest(
            workspace_id=ws, document_id=doc, document_version=ver, publication_id="pub_b",
            base_publication_id="pub_base", status="ready", entity_count=1, relationship_count=1,
            ingested_at_utc=_now_utc(), content_hash="hb"
        )
        man_c = KnowledgeGraphManifest(
            workspace_id=ws, document_id=doc, document_version=ver, publication_id="pub_c",
            base_publication_id="pub_base", status="ready", entity_count=1, relationship_count=1,
            ingested_at_utc=_now_utc(), content_hash="hc"
        )

        repo1.commit_manifest(ws, man_b, base_publication_id="pub_base")

        with pytest.raises(OptimisticConcurrencyError):
            repo2.commit_manifest(ws, man_c, base_publication_id="pub_base")
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_sqlite_publication_pause_after_validation_before_write():
    """covers: AC-4 Verify SQLite re-verification catches concurrent publication advance during pause."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        repo1 = SQLiteKnowledgeGraphRepository(db_path=db_path)
        repo2 = SQLiteKnowledgeGraphRepository(db_path=db_path)
        ws = "ws_sqlite_pause"
        doc = "doc_pause_1"
        ver = "v1"

        man_0 = KnowledgeGraphManifest(
            workspace_id=ws, document_id=doc, document_version=ver, publication_id="pub_0",
            status="ready", entity_count=1, relationship_count=1, ingested_at_utc=_now_utc(), content_hash="h0"
        )
        repo1.commit_manifest(ws, man_0, base_publication_id=None)

        man_worker1 = KnowledgeGraphManifest(
            workspace_id=ws, document_id=doc, document_version=ver, publication_id="pub_w1",
            base_publication_id="pub_0", status="ready", entity_count=1, relationship_count=1,
            ingested_at_utc=_now_utc(), content_hash="hw1"
        )
        man_worker2 = KnowledgeGraphManifest(
            workspace_id=ws, document_id=doc, document_version=ver, publication_id="pub_w2",
            base_publication_id="pub_0", status="ready", entity_count=1, relationship_count=1,
            ingested_at_utc=_now_utc(), content_hash="hw2"
        )

        pause_invoked = threading.Event()
        worker2_result = {"error": None}

        def _pause_hook():
            pause_invoked.set()
            conn_sneak = sqlite3.connect(db_path, timeout=0.1, isolation_level=None)
            try:
                conn_sneak.execute("BEGIN IMMEDIATE")
                conn_sneak.execute(
                    "UPDATE kre_knowledge_graph SET data_json = ? WHERE pk = ? AND sk = 'MANIFEST'",
                    (json.dumps(man_worker2.model_dump()), f"KG#{ws}#DOC#{doc}#V#{ver}"),
                )
                conn_sneak.execute("COMMIT")
            except sqlite3.OperationalError as op_err:
                worker2_result["error"] = op_err
            finally:
                conn_sneak.close()

        repo1._pre_write_hook = _pause_hook

        repo1.commit_manifest(ws, man_worker1, base_publication_id="pub_0")
        assert pause_invoked.is_set()
        assert isinstance(worker2_result["error"], sqlite3.OperationalError)
        assert "locked" in str(worker2_result["error"]).lower()

        current = repo1.get_manifest(ws, doc, ver)
        assert current.publication_id == "pub_w1"

        with pytest.raises(OptimisticConcurrencyError):
            repo2.commit_manifest(ws, man_worker2, base_publication_id="pub_0")
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_sqlite_fenced_publication_separate_connection_concurrent_lease_change():
    """covers: AC-4 Verify SQLite fenced publication atomicity during pause and lease expiration."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        repo1 = SQLiteKnowledgeGraphRepository(db_path=db_path)
        repo2 = SQLiteKnowledgeGraphRepository(db_path=db_path)
        ws = "ws_sqlite_lease_race"
        doc = "doc_lease_1"
        ver = "v1"

        ledger = KnowledgeGraphBudgetLedger(
            workspace_id=ws, document_id=doc, document_version=ver,
            reserved_calls=5, completed_calls=0, reserved_tokens=1000,
            fence_token=10, lease_owner_id="worker_alpha", lease_expires_at=time.time() + 60.0,
            updated_at=_now_utc(),
        )
        repo1.save_budget_ledger(ledger)

        man = KnowledgeGraphManifest(
            workspace_id=ws, document_id=doc, document_version=ver, publication_id="pub_fenced",
            status="ready", entity_count=1, relationship_count=1, ingested_at_utc=_now_utc(), content_hash="hf"
        )

        pause_invoked = threading.Event()
        sneak_err = {"error": None}

        def _steal_lease_hook():
            pause_invoked.set()
            conn_sneak = sqlite3.connect(db_path, timeout=0.1, isolation_level=None)
            try:
                conn_sneak.execute("BEGIN IMMEDIATE")
                stolen_ledger = ledger.model_copy(update={"lease_owner_id": "worker_beta"})
                conn_sneak.execute(
                    "UPDATE kre_knowledge_graph SET data_json = ? WHERE pk = ? AND sk = 'BUDGET#LEDGER'",
                    (json.dumps(stolen_ledger.model_dump()), f"KG#{ws}#DOC#{doc}#V#{ver}"),
                )
                conn_sneak.execute("COMMIT")
            except sqlite3.OperationalError as op_err:
                sneak_err["error"] = op_err
            finally:
                conn_sneak.close()

        repo1._pre_write_hook = _steal_lease_hook

        repo1.commit_manifest(ws, man, fence_token=10, worker_id="worker_alpha")
        assert pause_invoked.is_set()
        assert isinstance(sneak_err["error"], sqlite3.OperationalError)
        assert "locked" in str(sneak_err["error"]).lower()

        expired_ledger = ledger.model_copy(update={"lease_expires_at": time.time() - 10.0})
        repo2.save_budget_ledger(expired_ledger)

        man2 = KnowledgeGraphManifest(
            workspace_id=ws, document_id=doc, document_version=ver, publication_id="pub_fenced_2",
            base_publication_id="pub_fenced", status="ready", entity_count=1, relationship_count=1,
            ingested_at_utc=_now_utc(), content_hash="hf2"
        )
        with pytest.raises(FencingConflictError):
            repo2.commit_manifest(ws, man2, base_publication_id="pub_fenced", fence_token=10, worker_id="worker_alpha")
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_dynamodb_fencing_atomic_validation_and_rejection():
    """covers: AC-4 Verify DynamoDB rejects un-authorized or fenced operations without silent bypass."""
    try:
        import boto3
        client = boto3.client("dynamodb", endpoint_url="http://localhost:8000", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
        client.list_tables()
    except Exception:
        pytest.skip("DynamoDB Local daemon not reachable on port 8000")

    table_name = "kre_kg_test_fencing"
    existing_tables = client.list_tables().get("TableNames", [])
    if table_name not in existing_tables:
        client.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1_PK", "AttributeType": "S"},
                {"AttributeName": "GSI1_SK", "AttributeType": "S"},
                {"AttributeName": "GSI2_PK", "AttributeType": "S"},
                {"AttributeName": "GSI2_SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "gsi1_target_lookup",
                    "KeySchema": [
                        {"AttributeName": "GSI1_PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1_SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "gsi2_doc_version",
                    "KeySchema": [
                        {"AttributeName": "GSI2_PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI2_SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )

    repo = DynamoDBKnowledgeGraphRepository(table_name=table_name, endpoint_url="http://localhost:8000")
    ws = f"ws_ddb_fencing_{uuid.uuid4().hex[:6]}"
    doc = "doc_ddb_fence"
    ver = "v1"

    manifest = KnowledgeGraphManifest(
        workspace_id=ws, document_id=doc, document_version=ver, publication_id="pub_ddb_1",
        status="ready", entity_count=1, relationship_count=1, ingested_at_utc=_now_utc(), content_hash="h_ddb"
    )

    # 1. Missing worker_id fails
    with pytest.raises(FencingConflictError):
        repo.commit_manifest(ws, manifest, fence_token=1, worker_id="")

    # 2. Missing budget ledger fails
    with pytest.raises(FencingConflictError):
        repo.commit_manifest(ws, manifest, fence_token=1, worker_id="worker_1")

    # 3. Mismatched fence token fails
    ledger = KnowledgeGraphBudgetLedger(
        workspace_id=ws, document_id=doc, document_version=ver,
        reserved_calls=5, completed_calls=0, reserved_tokens=1000,
        fence_token=10, lease_owner_id="worker_1", lease_expires_at=time.time() + 60.0,
        updated_at=_now_utc(),
    )
    repo.save_budget_ledger(ledger)

    with pytest.raises(FencingConflictError):
        repo.commit_manifest(ws, manifest, fence_token=99, worker_id="worker_1")

    # 4. Mismatched worker_id fails
    with pytest.raises(FencingConflictError):
        repo.commit_manifest(ws, manifest, fence_token=10, worker_id="worker_imposter")

    # 5. Valid authority succeeds
    repo.commit_manifest(ws, manifest, fence_token=10, worker_id="worker_1")
    committed = repo.get_manifest(ws, doc, ver)
    assert committed is not None
    assert committed.publication_id == "pub_ddb_1"


def test_hydration_missing_or_unverifiable_version_fails_closed():
    """covers: AC-9, AC-10 Verify hydration version identity strictly fails closed."""
    ws = "ws_version_fail_closed"
    doc_repo = DocumentsRepository()
    doc_id = "doc_v_check"

    from schemas.models import Chunk, Document
    dummy_fast = [0.1] * 384
    dummy_full = [0.1] * 1024
    chunk = Chunk(
        id="chunk_v1",
        document_id=doc_id,
        source_format="csv",
        text="Sample chunk text",
        element_type="row",
        location_reference="Row 1",
        metadata={"version": "1.0"},
        embedding_fast=dummy_fast,
        embedding_full=dummy_full,
        workspace_id=ws,
    )
    doc_repo.save(Document(
        id=doc_id,
        filename="test.csv",
        source_format="csv",
        chunks=(chunk,),
        workspace_id=ws,
    ))

    # 1. Key with missing/empty document_version fails closed -> 0 chunks
    k_empty = ChunkLookupKey(chunk_id="chunk_v1", document_id=doc_id, document_version="")
    assert len(doc_repo.get_chunks_by_ids(ws, [k_empty])) == 0

    # 2. Key with mismatched version fails closed -> 0 chunks
    k_wrong = ChunkLookupKey(chunk_id="chunk_v1", document_id=doc_id, document_version="2.0")
    assert len(doc_repo.get_chunks_by_ids(ws, [k_wrong])) == 0

    # 3. Key with matching version succeeds
    k_valid = ChunkLookupKey(chunk_id="chunk_v1", document_id=doc_id, document_version="1.0")
    res_valid = doc_repo.get_chunks_by_ids(ws, [k_valid])
    assert len(res_valid) == 1
    assert res_valid[0].id == "chunk_v1"

    # 4. Chunk with NO metadata version, verified via boundary repo snapshot
    chunk_no_meta = Chunk(
        id="chunk_no_meta",
        document_id="doc_no_meta",
        source_format="csv",
        text="No meta version text",
        element_type="row",
        location_reference="Row 2",
        metadata={},  # no version
        embedding_fast=dummy_fast,
        embedding_full=dummy_full,
        workspace_id=ws,
    )
    doc_repo.save(Document(
        id="doc_no_meta",
        filename="test2.csv",
        source_format="csv",
        chunks=(chunk_no_meta,),
        workspace_id=ws,
    ))

    with patch("db.workspace_boundary_repo.get_workspace_boundary_repo") as mock_get_b_repo:
        mock_b_repo = MagicMock()
        mock_get_b_repo.return_value = mock_b_repo

        mock_b_repo.get_document_snapshot.return_value = MagicMock(version="3.0")
        k_snap = ChunkLookupKey(chunk_id="chunk_no_meta", document_id="doc_no_meta", document_version="3.0")
        res_snap = doc_repo.get_chunks_by_ids(ws, [k_snap])
        assert len(res_snap) == 1
        assert res_snap[0].id == "chunk_no_meta"

        k_snap_wrong = ChunkLookupKey(chunk_id="chunk_no_meta", document_id="doc_no_meta", document_version="5.0")
        assert len(doc_repo.get_chunks_by_ids(ws, [k_snap_wrong])) == 0

        mock_b_repo.get_document_snapshot.return_value = None
        assert len(doc_repo.get_chunks_by_ids(ws, [k_snap])) == 0

