"""Focused offline regression test suite for Proposed 0006 Slice 1:
OKF factual extraction → grounding validation → partitioned storage → isolated lookup.

Covers:
- Typed schema validation, contracts, and immutability (AC-1, AC-4, AC-5).
- Stable assertion identity and document-scoped entity ID derivation (AC-4, AC-5).
- Curated table schema and column header extraction for CSVs with 0 model tokens (AC-1).
- Verbatim chunk grounding validation and rejection of unsupported model claims (AC-2).
- Ingestion resource bounds, token caps, and idempotency replay (AC-3).
- ADR-001 fail open decoupled ingestion resilience (AC-1, AC-3, AC-11).
- Conflicting property value retention under distinct provenance (AC-4).
- Same-name entity ambiguity handling across documents (AC-5).
- Workspace partition boundary isolation with zero cross-tenant leakage (AC-6).
- Immediate tombstone filtering inside worker tasks (AC-7).
- Deadline enforcement (50ms) and bounded concurrency admission (AC-8).
- Concurrency worker capacity retention until underlying storage finishes (AC-8).
- Non-destructive legacy cutover excluding legacy unpartitioned records (AC-10).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import threading
import time
from unittest.mock import MagicMock, patch
import pytest

from src.db.okf_repo import (
    InMemoryOkfRepository,
    SQLiteOkfRepository,
    create_dedicated_storage_config,
    disable_client_retries,
)
from src.db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
from src.ingestion.okf_builder import (
    OkfBuilder,
    extract_and_persist_okf,
    is_value_grounded,
)
from src.schemas.contracts.boundary import DocumentSnapshot
from src.schemas.contracts.okf import (
    OkfEntityRecord,
    OkfLookupResult,
    OkfManifest,
    OkfPropertyAssertion,
    compute_assertion_id,
    compute_entity_id,
    compute_idempotency_key,
    normalize_entity_name,
    normalize_property_key,
)
from src.schemas.models import Chunk
from src.services.retrieval.okf_lookup import (
    OkfConcurrencyController,
    extract_entity_names_from_query,
)
from src.services.retrieval.okf_retriever import OkfRetriever


# ===========================================================================
# 1. Schema Contracts, Immutability, and Identity Derivations
# ===========================================================================

def test_schema_contracts_and_immutability():
    """Verify contracts enforce frozen immutability and reject unexpected fields."""
    ent = OkfEntityRecord(
        workspace_id="ws_1",
        entity_id="e1",
        entity_name="Acme Corp",
        normalized_name="acme corp",
        entity_type="organization",
        document_id="doc_1",
        document_version="v1",
        created_at="2026-10-26T00:00:00Z",
    )
    with pytest.raises(Exception):
        ent.entity_name = "Modified"  # Frozen

    with pytest.raises(Exception):
        OkfEntityRecord(
            workspace_id="ws_1",
            entity_id="e1",
            entity_name="Acme Corp",
            normalized_name="acme corp",
            document_id="doc_1",
            document_version="v1",
            created_at="2026-10-26T00:00:00Z",
            extra_field="illegal",  # extra='forbid'
        )

    ass = OkfPropertyAssertion(
        assertion_id="a1",
        workspace_id="ws_1",
        entity_id="e1",
        entity_name="Acme Corp",
        property_key="revenue",
        property_value="$10M",
        value_type="string",
        source_chunk_id="chunk_1",
        document_id="doc_1",
        document_version="v1",
        created_at="2026-10-26T00:00:00Z",
    )
    with pytest.raises(Exception):
        ass.property_value = "$20M"  # Frozen


def test_identity_derivation_formulas():
    """Verify deterministic formulas for entity_id, assertion_id, and idempotency_key."""
    ws = "ws_test"
    doc_id = "doc_alpha"
    ver = "ver_123"
    norm_name = "acme corp"

    e_id = compute_entity_id(ws, doc_id, norm_name)
    assert len(e_id) == 16
    # Stable across runs
    assert e_id == compute_entity_id(ws, doc_id, norm_name)

    ass_id = compute_assertion_id(ws, doc_id, ver, e_id, "revenue", "$10M", "chunk_42")
    assert len(ass_id) == 24
    assert ass_id == compute_assertion_id(ws, doc_id, ver, e_id, "revenue", "$10M", "chunk_42")

    # Varying chunk ID preserves distinct assertion ID
    ass_id_2 = compute_assertion_id(ws, doc_id, ver, e_id, "revenue", "$10M", "chunk_43")
    assert ass_id != ass_id_2

    idem_key = compute_idempotency_key(ws, doc_id, ver)
    assert len(idem_key) == 64
    assert idem_key == compute_idempotency_key(ws, doc_id, ver)


# ===========================================================================
# 2. Curated Table Schema Extraction for CSVs (0 Model Tokens)
# ===========================================================================

def test_curated_table_schema_csv_extraction():
    """Verify tabular CSV files extract table entity and column schemas with 0 model calls."""
    repo = InMemoryOkfRepository()
    builder = OkfBuilder(repo=repo)

    chunks = [
        Chunk(
            id="doc_csv:row1",
            document_id="doc_csv",
            source_format="csv",
            text="Year: 2024. Revenue: 1000.",
            element_type="row",
            metadata={"headers": ["Year", "Revenue", "Industry"]},
        )
    ]

    manifest = builder.extract_and_persist(
        workspace_id="ws_csv",
        document_id="doc_csv",
        document_version="v1",
        format="csv",
        chunks=chunks,
        table_name="survay",
    )

    assert manifest.status == "ready"
    assert manifest.model_calls == 0
    assert manifest.extraction_tokens == 0
    assert len(manifest.entities) == 1
    assert manifest.entities[0].entity_type == "table"
    assert manifest.entities[0].entity_name == "survay"

    # Columns extracted as property assertions
    assert len(manifest.assertions) == 3
    keys = {a.property_key for a in manifest.assertions}
    assert keys == {"column:Year", "column:Revenue", "column:Industry"}
    for a in manifest.assertions:
        assert a.value_type == "schema_type"
        assert a.table_name == "survay"
        assert a.source_chunk_id == "doc_csv:row1"
        assert a.confidence_score == 1.0

    # Verify persisted in repository
    assert repo.has_ready_okf("ws_csv", "doc_csv", "v1") is True
    persisted_ass, _ = repo.list_assertions_by_document("ws_csv", "doc_csv", "v1")
    assert len(persisted_ass) == 3


# ===========================================================================
# 3. Verbatim Chunk Grounding Validation and Rejection of Hallucinations
# ===========================================================================

def test_chunk_grounding_rejection_unsupported_claims():
    """Verify assertions failing membership, locator, or verbatim text match are rejected."""
    repo = InMemoryOkfRepository()

    # Active chunk
    ch1 = Chunk(
        id="doc_p:c1",
        document_id="doc_p",
        source_format="pdf",
        text="Acme Corp reported $15M in revenue for fiscal year 2025.",
        element_type="paragraph",
        page_number=1,
    )
    # Chunk with missing locator
    ch_no_loc = Chunk(
        id="doc_p:c_noloc",
        document_id="doc_p",
        source_format="pdf",
        text="Beta LLC was acquired for $5M.",
        element_type="paragraph",
        page_number=None,
        location_reference=None,
    )

    # Mock Bedrock client returning candidate claims
    model_output = json.dumps([
        # Valid claim
        {
            "entity_name": "Acme Corp",
            "property_key": "revenue",
            "property_value": "$15M",
            "source_chunk_id": "doc_p:c1",
        },
        # Hallucinated value not appearing in chunk text
        {
            "entity_name": "Acme Corp",
            "property_key": "profit",
            "property_value": "$99M",  # Not in chunk
            "source_chunk_id": "doc_p:c1",
        },
        # Unauthorized chunk ID not in active chunks
        {
            "entity_name": "Unknown Corp",
            "property_key": "location",
            "property_value": "Seattle",
            "source_chunk_id": "doc_p:unauthorized_chunk_99",
        },
        # Chunk with missing locator
        {
            "entity_name": "Beta LLC",
            "property_key": "deal_size",
            "property_value": "$5M",
            "source_chunk_id": "doc_p:c_noloc",
        },
    ])

    mock_bedrock = MagicMock()
    mock_bedrock.converse.return_value = {
        "output": {"message": {"content": [{"text": model_output}]}},
        "usage": {"inputTokens": 200, "outputTokens": 80},
    }

    builder = OkfBuilder(repo=repo, bedrock_client=mock_bedrock)
    manifest = builder.extract_and_persist(
        workspace_id="ws_grounding",
        document_id="doc_p",
        document_version="v1",
        format="pdf",
        chunks=[ch1, ch_no_loc],
    )

    # Only the 1 valid claim should be accepted
    assert len(manifest.assertions) == 1
    assert manifest.assertions[0].property_key == "revenue"
    assert manifest.assertions[0].property_value == "$15M"

    # Exactly 3 unsupported claims rejected
    assert manifest.rejected_assertions_count == 3
    assert manifest.status == "ready"


def test_is_value_grounded_helpers():
    """Verify verbatim and normalized token grounding checks."""
    chunk_text = "Operating income reached $4,191 thousand in 2025."
    assert is_value_grounded("$4,191", chunk_text) is True
    assert is_value_grounded("4191", chunk_text) is True
    assert is_value_grounded("2025", chunk_text) is True
    assert is_value_grounded("Operating income", chunk_text) is True
    # Non existent
    assert is_value_grounded("$9,999", chunk_text) is False
    assert is_value_grounded("Net Loss", chunk_text) is False
    # False positive prevention: boundary enforcement
    assert is_value_grounded("cat", "We improved communication in the team.") is False
    assert is_value_grounded("10", "The budget is $1000.") is False
    assert is_value_grounded("Acme Corp", "Acme Corporation reported strong growth.") is False
    assert is_value_grounded("Acme Corp", "Acme Corp reported strong growth.") is True


# ===========================================================================
# 4. Ingestion Resource Bounds, Token Caps, and Idempotency
# ===========================================================================

def test_ingestion_budget_enforcement():
    """Verify Bedrock calls enforce max calls, token caps, and provider deadlines."""
    repo = InMemoryOkfRepository()
    mock_bedrock = MagicMock()
    mock_bedrock.converse.return_value = {
        "output": {"message": {"content": [{"text": "[]"}]}},
        "usage": {"inputTokens": 500, "outputTokens": 50},
    }

    # Generate 20 chunks
    chunks = [
        Chunk(
            id=f"doc:c{i}",
            document_id="doc_budget",
            source_format="pdf",
            text=f"Paragraph {i} describing financial metrics and organizational structure.",
            element_type="paragraph",
            page_number=i + 1,
        )
        for i in range(20)
    ]

    builder = OkfBuilder(repo=repo, bedrock_client=mock_bedrock)
    manifest = builder.extract_and_persist(
        workspace_id="ws_b",
        document_id="doc_budget",
        document_version="v1",
        format="pdf",
        chunks=chunks,
    )

    # Must stay within max 2 Bedrock calls (AC-3)
    assert mock_bedrock.converse.call_count <= 2
    assert manifest.model_calls == mock_bedrock.converse.call_count
    assert manifest.extraction_tokens == mock_bedrock.converse.call_count * 550


def test_idempotent_replay_zero_additional_model_calls():
    """Replaying an already extracted document version reuses assertions with 0 model calls."""
    repo = InMemoryOkfRepository()
    mock_bedrock = MagicMock()
    mock_bedrock.converse.return_value = {
        "output": {"message": {"content": [{"text": json.dumps([
            {"entity_name": "Acme", "property_key": "ceo", "property_value": "Jane", "source_chunk_id": "c1"}
        ])}]}},
        "usage": {"inputTokens": 100, "outputTokens": 20},
    }

    ch1 = Chunk(id="c1", document_id="doc_idem", source_format="pdf", text="Acme CEO Jane announced results.", element_type="p", page_number=1)
    builder = OkfBuilder(repo=repo, bedrock_client=mock_bedrock)

    # First pass: executes 1 Bedrock call
    m1 = builder.extract_and_persist("ws_idem", "doc_idem", "v1", "pdf", [ch1])
    assert m1.model_calls == 1
    assert mock_bedrock.converse.call_count == 1

    # Second pass: replaying same version reuses persisted assertions with 0 model calls
    m2 = builder.extract_and_persist("ws_idem", "doc_idem", "v1", "pdf", [ch1])
    assert m2.idempotency_key == m1.idempotency_key
    assert mock_bedrock.converse.call_count == 1  # Not called again!


def test_adr001_fail_open_ingestion_resilience():
    """Injecting a model provider exception records status='failed' without raising errors."""
    repo = InMemoryOkfRepository()
    mock_bedrock = MagicMock()
    mock_bedrock.converse.side_effect = RuntimeError("Bedrock timeout or rate limit")

    ch1 = Chunk(id="c1", document_id="doc_err", source_format="pdf", text="Sample text", element_type="p", page_number=1)
    builder = OkfBuilder(repo=repo, bedrock_client=mock_bedrock)

    # Does not raise! Fails open per ADR-001
    manifest = builder.extract_and_persist("ws_err", "doc_err", "v1", "pdf", [ch1])
    assert manifest.status == "failed"
    assert len(manifest.assertions) == 0


# ===========================================================================
# 5. Conflicting Values, Same-Name Ambiguity, and Scoped Retrieval
# ===========================================================================

def test_conflicting_property_values_retention():
    """Doc A asserts revenue as $10M; Doc B asserts revenue as $12M. Both persist and conflict."""
    repo = InMemoryOkfRepository()
    retriever = OkfRetriever(repository=repo)

    now = datetime.now(timezone.utc).isoformat()
    # Doc A assertion
    ass_a = OkfPropertyAssertion(
        assertion_id="ass_a",
        workspace_id="ws_conflict",
        entity_id="ent_acme",
        entity_name="Acme Corp",
        property_key="revenue",
        property_value="$10M",
        value_type="string",
        source_chunk_id="chunk_doc_a",
        document_id="doc_a",
        document_version="v1",
        created_at=now,
    )
    # Doc B assertion
    ass_b = OkfPropertyAssertion(
        assertion_id="ass_b",
        workspace_id="ws_conflict",
        entity_id="ent_acme",
        entity_name="Acme Corp",
        property_key="revenue",
        property_value="$12M",
        value_type="string",
        source_chunk_id="chunk_doc_b",
        document_id="doc_b",
        document_version="v1",
        created_at=now,
    )

    ent = OkfEntityRecord(
        workspace_id="ws_conflict",
        entity_id="ent_acme",
        entity_name="Acme Corp",
        normalized_name="acme corp",
        entity_type="organization",
        document_id="doc_a",
        document_version="v1",
        created_at=now,
    )

    man_a = OkfManifest(
        workspace_id="ws_conflict",
        document_id="doc_a",
        document_version="v1",
        entities=(ent,),
        assertions=(ass_a,),
        idempotency_key="k1",
        created_at=now,
    )
    man_b = OkfManifest(
        workspace_id="ws_conflict",
        document_id="doc_b",
        document_version="v1",
        entities=(ent,),
        assertions=(ass_b,),
        idempotency_key="k2",
        created_at=now,
    )
    repo.save_manifest("ws_conflict", man_a)
    repo.save_manifest("ws_conflict", man_b)

    # Query for revenue
    res = retriever.lookup_sync(
        workspace_id="ws_conflict",
        query="What was Acme Corp revenue?",
        target_entities=["Acme Corp"],
    )

    assert res.execution_status == "completed"
    assert res.property_state == "conflicting"
    assert len(res.matched_assertions) == 2
    vals = {a.property_value for a in res.matched_assertions}
    assert vals == {"$10M", "$12M"}
    chunks = set(res.candidate_chunk_ids)
    assert chunks == {"chunk_doc_a", "chunk_doc_b"}


def test_same_name_entity_ambiguity():
    """Doc A has John Smith (CEO); Doc B has John Smith (Author). Distinct entity IDs and ambiguity."""
    repo = InMemoryOkfRepository()
    retriever = OkfRetriever(repository=repo)
    now = datetime.now(timezone.utc).isoformat()

    # Distinct entity IDs scoped to document
    e_id_a = compute_entity_id("ws_ambig", "doc_ceo", "john smith")
    e_id_b = compute_entity_id("ws_ambig", "doc_author", "john smith")
    assert e_id_a != e_id_b

    ent_a = OkfEntityRecord(
        workspace_id="ws_ambig",
        entity_id=e_id_a,
        entity_name="John Smith",
        normalized_name="john smith",
        entity_type="person",
        document_id="doc_ceo",
        document_version="v1",
        created_at=now,
    )
    ent_b = OkfEntityRecord(
        workspace_id="ws_ambig",
        entity_id=e_id_b,
        entity_name="John Smith",
        normalized_name="john smith",
        entity_type="person",
        document_id="doc_author",
        document_version="v1",
        created_at=now,
    )

    ass_a = OkfPropertyAssertion(
        assertion_id="ass_ceo",
        workspace_id="ws_ambig",
        entity_id=e_id_a,
        entity_name="John Smith",
        property_key="title",
        property_value="CEO",
        value_type="string",
        source_chunk_id="chunk_ceo",
        document_id="doc_ceo",
        document_version="v1",
        created_at=now,
    )
    ass_b = OkfPropertyAssertion(
        assertion_id="ass_author",
        workspace_id="ws_ambig",
        entity_id=e_id_b,
        entity_name="John Smith",
        property_key="title",
        property_value="Author",
        value_type="string",
        source_chunk_id="chunk_author",
        document_id="doc_author",
        document_version="v1",
        created_at=now,
    )

    repo.save_manifest("ws_ambig", OkfManifest(
        workspace_id="ws_ambig", document_id="doc_ceo", document_version="v1",
        entities=(ent_a,), assertions=(ass_a,), idempotency_key="k1", created_at=now,
    ))
    repo.save_manifest("ws_ambig", OkfManifest(
        workspace_id="ws_ambig", document_id="doc_author", document_version="v1",
        entities=(ent_b,), assertions=(ass_b,), idempotency_key="k2", created_at=now,
    ))

    res = retriever.lookup_sync("ws_ambig", "Who is John Smith?", target_entities=["John Smith"])
    assert res.entity_state == "ambiguous"
    assert len(res.matched_assertions) == 2
    roles = {a.property_value for a in res.matched_assertions}
    assert roles == {"CEO", "Author"}

    # Distinct entities across distinct documents must NOT be marked ambiguous
    ent_jane = OkfEntityRecord(
        workspace_id="ws_ambig",
        entity_id="e_jane",
        entity_name="Jane Doe",
        normalized_name="jane doe",
        entity_type="person",
        document_id="doc_other",
        document_version="v1",
        created_at=now,
    )
    repo.save_manifest("ws_ambig", OkfManifest(
        workspace_id="ws_ambig", document_id="doc_other", document_version="v1",
        entities=(ent_jane,), assertions=(), idempotency_key="k3", created_at=now,
    ))
    res_distinct = retriever.lookup_sync(
        "ws_ambig",
        "John Smith and Jane Doe",
        target_entities=["John Smith", "Jane Doe"],
    )
    # John Smith is still ambiguous across doc_ceo and doc_author, so workspace query has same name ambiguity
    assert res_distinct.entity_state == "ambiguous"

    # In a clean workspace with distinct entities from distinct docs:
    repo_clean = InMemoryOkfRepository()
    retriever_clean = OkfRetriever(repository=repo_clean)
    repo_clean.save_manifest("ws_clean", OkfManifest(
        workspace_id="ws_clean", document_id="doc1", document_version="v1",
        entities=(ent_a,), assertions=(), idempotency_key="k1", created_at=now,
    ))
    repo_clean.save_manifest("ws_clean", OkfManifest(
        workspace_id="ws_clean", document_id="doc2", document_version="v1",
        entities=(ent_jane,), assertions=(), idempotency_key="k2", created_at=now,
    ))
    res_clean = retriever_clean.lookup_sync(
        "ws_clean",
        "John Smith and Jane Doe",
        target_entities=["John Smith", "Jane Doe"],
    )
    # Distinct names: entity_state must be 'unique', NOT 'ambiguous'
    assert res_clean.entity_state == "unique"


# ===========================================================================
# 6. Boundary Isolation and Tombstones
# ===========================================================================

def test_partitioned_workspace_isolation_zero_leakage():
    """Facts in ws_a are never returned to queries in ws_b."""
    repo = InMemoryOkfRepository()
    retriever = OkfRetriever(repository=repo)
    now = datetime.now(timezone.utc).isoformat()

    ent = OkfEntityRecord(
        workspace_id="ws_a",
        entity_id="e_secret",
        entity_name="Secret Project",
        normalized_name="secret project",
        entity_type="organization",
        document_id="doc_a",
        document_version="v1",
        created_at=now,
    )
    ass = OkfPropertyAssertion(
        assertion_id="ass_secret",
        workspace_id="ws_a",
        entity_id="e_secret",
        entity_name="Secret Project",
        property_key="budget",
        property_value="$100M",
        value_type="string",
        source_chunk_id="chunk_s",
        document_id="doc_a",
        document_version="v1",
        created_at=now,
    )
    repo.save_manifest("ws_a", OkfManifest(
        workspace_id="ws_a", document_id="doc_a", document_version="v1",
        entities=(ent,), assertions=(ass,), idempotency_key="k", created_at=now,
    ))

    # Query from ws_b
    res_b = retriever.lookup_sync("ws_b", "What is Secret Project budget?", target_entities=["Secret Project"])
    assert res_b.entity_state == "none"
    assert len(res_b.matched_assertions) == 0

    # Query from ws_a
    res_a = retriever.lookup_sync("ws_a", "What is Secret Project budget?", target_entities=["Secret Project"])
    assert res_a.entity_state == "unique"
    assert len(res_a.matched_assertions) == 1
    assert res_a.matched_assertions[0].property_value == "$100M"


def test_tombstone_filtering_immediate_enforcement():
    """Active deletion tombstones immediately filter out assertions from deleted documents."""
    repo = InMemoryOkfRepository()
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    retriever = OkfRetriever(repository=repo, boundary_repo=boundary_repo)
    now = datetime.now(timezone.utc).isoformat()

    ent1 = OkfEntityRecord(
        workspace_id="ws_tomb",
        entity_id="e1",
        entity_name="Tombstone Corp",
        normalized_name="tombstone corp",
        entity_type="organization",
        document_id="doc_active",
        document_version="v1",
        created_at=now,
    )
    ent2 = OkfEntityRecord(
        workspace_id="ws_tomb",
        entity_id="e2",
        entity_name="Tombstone Corp",
        normalized_name="tombstone corp",
        entity_type="organization",
        document_id="doc_deleted",
        document_version="v1",
        created_at=now,
    )
    ass1 = OkfPropertyAssertion(
        assertion_id="a1",
        workspace_id="ws_tomb",
        entity_id="e1",
        entity_name="Tombstone Corp",
        property_key="location",
        property_value="Denver",
        value_type="string",
        source_chunk_id="c1",
        document_id="doc_active",
        document_version="v1",
        created_at=now,
    )
    ass2 = OkfPropertyAssertion(
        assertion_id="a2",
        workspace_id="ws_tomb",
        entity_id="e2",
        entity_name="Tombstone Corp",
        property_key="location",
        property_value="Atlanta",
        value_type="string",
        source_chunk_id="c2",
        document_id="doc_deleted",
        document_version="v1",
        created_at=now,
    )

    repo.save_manifest("ws_tomb", OkfManifest(
        workspace_id="ws_tomb", document_id="doc_active", document_version="v1",
        entities=(ent1,), assertions=(ass1,), idempotency_key="k1", created_at=now,
    ))
    repo.save_manifest("ws_tomb", OkfManifest(
        workspace_id="ws_tomb", document_id="doc_deleted", document_version="v1",
        entities=(ent2,), assertions=(ass2,), idempotency_key="k2", created_at=now,
    ))

    boundary_repo.create_workspace("ws_tomb", owner_principal_id="admin", workspace_id="ws_tomb")
    boundary_repo.create_source("ws_tomb", "active doc", source_id="doc_active")
    boundary_repo.create_source("ws_tomb", "deleted doc", source_id="doc_deleted")

    # Record tombstone for doc_deleted
    boundary_repo.record_source_tombstone("ws_tomb", "doc_deleted", caller_principal_id="admin")

    res = retriever.lookup_sync("ws_tomb", "Where is Tombstone Corp?", target_entities=["Tombstone Corp"])
    # doc_deleted assertions must be excluded immediately
    assert len(res.matched_assertions) == 1
    assert res.matched_assertions[0].document_id == "doc_active"
    assert res.matched_assertions[0].property_value == "Denver"

    # Now tombstone entire workspace
    boundary_repo.tombstone_workspace("ws_tomb", caller_principal_id="admin")
    res_ws = retriever.lookup_sync("ws_tomb", "Where is Tombstone Corp?", target_entities=["Tombstone Corp"])
    assert res_ws.execution_status == "tombstoned"
    assert len(res_ws.matched_assertions) == 0


# ===========================================================================
# 7. Deadline Enforcement and Concurrency Controller
# ===========================================================================

def test_storage_deadline_enforcement_and_timeout():
    """Injecting a delay beyond 50ms deadline cleanly drops candidates with caller_timeout."""
    repo = InMemoryOkfRepository()
    controller = OkfConcurrencyController(max_concurrency=2, max_queue=4)
    retriever = OkfRetriever(repository=repo, controller=controller)

    # Slow function simulating delayed storage
    def slow_resolve(*args, **kwargs):
        time.sleep(0.080)
        return []

    repo.resolve_entities = slow_resolve  # type: ignore

    res = retriever.lookup_sync(
        workspace_id="ws_to",
        query="Tell me about Acme",
        target_entities=["Acme"],
        deadline_ms=20,  # 20ms deadline
    )

    assert res.execution_status == "caller_timeout"
    assert len(res.matched_assertions) == 0


def test_concurrency_controller_admission_queue_bounding():
    """Submissions exceeding queue capacity are shed immediately with admission_rejected."""
    controller = OkfConcurrencyController(max_concurrency=1, max_queue=2)

    def slow_worker(remaining_s):
        time.sleep(0.100)
        return "done"

    import concurrent.futures
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=5)

    # Submit 1 active + 2 queued + 2 excess = 5 total
    futures = [
        pool.submit(controller.run_storage_work_sync, slow_worker, deadline_s=0.200)
        for _ in range(5)
    ]

    statuses = [f.result()[1] for f in futures]
    assert "admission_rejected" in statuses
    pool.shutdown(wait=True)


def test_concurrency_capacity_retained_until_underlying_thread_finishes():
    """Worker capacity slot remains occupied until slow storage thread actually exits."""
    controller = OkfConcurrencyController(max_concurrency=1, max_queue=1)
    thread_exited = threading.Event()

    def very_slow_worker(remaining_s):
        time.sleep(0.060)
        thread_exited.set()
        return "ok"

    # Caller has a 20ms deadline; worker takes 60ms
    _, status = controller.run_storage_work_sync(very_slow_worker, deadline_s=0.020)
    assert status == "caller_timeout"

    # Even though caller timed out, the active worker slot is still retained!
    assert controller.active_count == 1

    # Wait for background thread to finish
    assert thread_exited.wait(timeout=0.200) is True
    # Give event loop / lock tiny moment to release
    time.sleep(0.010)
    assert controller.active_count == 0


# ===========================================================================
# 8. SQLite Repository Implementation Coverage
# ===========================================================================

def test_sqlite_okf_repository_operations(tmp_path):
    """Verify SQLiteOkfRepository persists manifests, GSI1 resolution, and GSI2 queries."""
    db_file = tmp_path / "test_okf.db"
    sqlite_repo = SQLiteOkfRepository(db_path=db_file)

    now = datetime.now(timezone.utc).isoformat()
    ent = OkfEntityRecord(
        workspace_id="ws_sq",
        entity_id="e_sq1",
        entity_name="Starlight Corp",
        normalized_name="starlight corp",
        entity_type="organization",
        document_id="doc_sq",
        document_version="v1",
        created_at=now,
    )
    ass = OkfPropertyAssertion(
        assertion_id="a_sq1",
        workspace_id="ws_sq",
        entity_id="e_sq1",
        entity_name="Starlight Corp",
        property_key="headquarters",
        property_value="Tokyo",
        value_type="string",
        source_chunk_id="c_sq1",
        document_id="doc_sq",
        document_version="v1",
        created_at=now,
    )
    man = OkfManifest(
        workspace_id="ws_sq",
        document_id="doc_sq",
        document_version="v1",
        entities=(ent,),
        assertions=(ass,),
        status="ready",
        idempotency_key="idem_sq",
        created_at=now,
    )

    sqlite_repo.save_manifest("ws_sq", man)

    # GSI1 entity resolution
    resolved = sqlite_repo.resolve_entities("ws_sq", ["starlight corp"])
    assert len(resolved) == 1
    assert resolved[0].entity_id == "e_sq1"

    # Main table assertion lookup
    assertions = sqlite_repo.get_assertions_for_entity("ws_sq", "e_sq1")
    assert len(assertions) == 1
    assert assertions[0].property_value == "Tokyo"

    # GSI2 document readiness
    assert sqlite_repo.has_ready_okf("ws_sq", "doc_sq", "v1") is True
    assert sqlite_repo.has_ready_okf("ws_sq", "doc_sq", "v999") is False

    # GSI2 list assertions
    items, token = sqlite_repo.list_assertions_by_document("ws_sq", "doc_sq", "v1", limit=10)
    assert len(items) == 1
    assert token is None

    # Retrieve manifest
    retrieved_man = sqlite_repo.get_manifest("ws_sq", "doc_sq", "v1")
    assert retrieved_man is not None
    assert retrieved_man.idempotency_key == "idem_sq"


def test_okf_repository_factory_and_proxy():
    """Verify OkfRepository class dynamically returns configured repository instance."""
    from src.db.okf_repo import OkfRepository, OkfRepositoryBase, get_okf_repo

    repo_inst = OkfRepository()
    assert isinstance(repo_inst, OkfRepositoryBase)
    assert repo_inst is get_okf_repo()


def test_async_concurrency_controller_admission_and_timeout():
    """Verify asynchronous storage work bounds admission queue and caller deadlines."""
    async def _run():
        controller = OkfConcurrencyController(max_concurrency=2, max_queue=2)

        # 1. Normal execution
        def fast_work(rem_s: float):
            return "fast_done"

        res, status = await controller.run_storage_work(fast_work, deadline_s=0.1)
        assert res == "fast_done"
        assert status == "completed"

        # 2. Slow work exceeding caller deadline
        event = threading.Event()

        def slow_work(rem_s: float):
            event.wait(timeout=0.2)
            return "slow_done"

        res_to, status_to = await controller.run_storage_work(slow_work, deadline_s=0.02)
        assert res_to is None
        assert status_to == "caller_timeout"
        # Capacity is retained while thread is still waiting
        assert controller.active_count > 0
        event.set()
        # Wait for thread release
        await asyncio.sleep(0.05)
        assert controller.active_count == 0

    asyncio.run(_run())


def test_dedicated_storage_config_and_retry_disabling():
    """Verify botocore storage config disables retries and enforces tight socket timeouts."""
    cfg = create_dedicated_storage_config(connect_timeout=0.025, read_timeout=0.035)
    assert cfg.connect_timeout == 0.025
    assert cfg.read_timeout == 0.035
    assert cfg.retries == {"max_attempts": 1, "mode": "standard"}

    mock_client = MagicMock()
    mock_events = MagicMock()
    mock_client.meta.events = mock_events
    disable_client_retries(mock_client)
    assert mock_events.unregister.call_count >= 1
