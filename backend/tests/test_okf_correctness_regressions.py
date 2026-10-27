"""Focused regression tests for OKF correctness gaps.

Verifies:
1. Carrying authorized document scope, active versions, and ready manifests into pipeline OKF lookup.
2. Single document-level extraction budget across attempts, retries, and replays without silent reset.
3. Entity-property-value relational grounding rejecting wrong entity attribution (counterexample).
4. Baseline publication independent of optional OKF extraction, preserving baseline availability on failure.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from config import settings
from db.database import CloudRepository
from db.okf_repo import InMemoryOkfRepository, SQLiteOkfRepository, get_okf_repo
from db.workspace_boundary_repo import (
    DocumentSnapshot,
    InMemoryWorkspaceBoundaryRepository,
    get_workspace_boundary_repo,
)
from ingestion.embed_service import _deterministic_vector, embed_fast_local
from ingestion.okf_builder import (
    SUPPORTED_OKF_MODELS,
    DocumentExtractionBudget,
    OkfBuilder,
    OkfProviderConcurrencyController,
    _EXTRACTION_BUDGETS,
    count_request_tokens,
    get_okf_provider_controller,
    get_or_create_document_budget,
    is_value_grounded,
    reset_budget_ledger,
    reset_provider_controller,
    validate_assertion_grounding,
)
from modules.documents.documents_repository import _IN_MEMORY_CHUNKS, _IN_MEMORY_DOCS
from modules.documents.documents_service import DocumentsService
from schemas.contracts.okf import (
    OkfEntityRecord,
    OkfManifest,
    OkfPropertyAssertion,
    compute_assertion_id,
    compute_entity_id,
    compute_idempotency_key,
)
from schemas.models import Chunk, Document
from services.langgraph_pipeline import run_okf_router
from services.retrieval.okf_lookup import get_okf_controller
from services.retrieval.okf_retriever import OkfRetriever


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def clean_fixtures():
    reset_budget_ledger()
    _IN_MEMORY_DOCS.clear()
    _IN_MEMORY_CHUNKS.clear()
    b_repo = get_workspace_boundary_repo(reset=True)
    okf_repo = get_okf_repo(reset=True)
    yield b_repo, okf_repo
    reset_budget_ledger()
    _IN_MEMORY_DOCS.clear()
    _IN_MEMORY_CHUNKS.clear()


# ==============================================================================
# 1. Pipeline Scoping: Explicit Document Scope, Active Versions & Ready Manifests
# ==============================================================================


def test_pipeline_explicit_document_scope_and_version_filtering():
    """Verify authorized document_ids scope and active version constraints in pipeline OKF lookup."""
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    okf_repo = InMemoryOkfRepository()
    chunk_repo = CloudRepository()

    ws_id = "ws_pipeline_scope"
    now = _now()

    # Document A (active version v2)
    chunk_a2 = Chunk(
        id="doc_a:c2",
        document_id="doc_a",
        workspace_id=ws_id,
        source_format="pdf",
        text="Alpha Corp generated $100M in revenue for 2026.",
        element_type="paragraph",
        page_number=1,
        embedding_fast=embed_fast_local("Alpha Corp generated $100M in revenue for 2026."),
        embedding_full=_deterministic_vector("Alpha Corp generated $100M in revenue for 2026.", 1024),
    )
    # Document A old version v1 chunk
    chunk_a1 = Chunk(
        id="doc_a:c1",
        document_id="doc_a",
        workspace_id=ws_id,
        source_format="pdf",
        text="Alpha Corp generated $10M in revenue for 2025.",
        element_type="paragraph",
        page_number=1,
        embedding_fast=embed_fast_local("Alpha Corp generated $10M in revenue for 2025."),
        embedding_full=_deterministic_vector("Alpha Corp generated $10M in revenue for 2025.", 1024),
    )
    # Document B (active version v1)
    chunk_b1 = Chunk(
        id="doc_b:c1",
        document_id="doc_b",
        workspace_id=ws_id,
        source_format="pdf",
        text="Alpha Corp generated $200M in revenue according to secondary report.",
        element_type="paragraph",
        page_number=1,
        embedding_fast=embed_fast_local("Alpha Corp generated $200M in revenue according to secondary report."),
        embedding_full=_deterministic_vector("Alpha Corp generated $200M in revenue according to secondary report.", 1024),
    )

    doc_a = Document(id="doc_a", filename="alpha.pdf", source_format="pdf", chunks=[chunk_a1, chunk_a2], workspace_id=ws_id)
    doc_b = Document(id="doc_b", filename="beta.pdf", source_format="pdf", chunks=[chunk_b1], workspace_id=ws_id)
    chunk_repo.save(doc_a)
    chunk_repo.save(doc_b)

    # Active snapshots in boundary repo: doc_a is on v2, doc_b is on v1
    snap_a = DocumentSnapshot(
        workspace_id=ws_id,
        document_id="doc_a",
        source="alpha.pdf",
        version="v2",
        format="pdf",
        incomplete_coverage_flag=False,
        exclusions=(),
        persisted_rows=1,
        total_rows=1,
        created_at=now,
        okf_status="ready",
        okf_entities_count=1,
        okf_properties_count=1,
    )
    snap_b = DocumentSnapshot(
        workspace_id=ws_id,
        document_id="doc_b",
        source="beta.pdf",
        version="v1",
        format="pdf",
        incomplete_coverage_flag=False,
        exclusions=(),
        persisted_rows=1,
        total_rows=1,
        created_at=now,
        okf_status="ready",
        okf_entities_count=1,
        okf_properties_count=1,
    )
    boundary_repo.save_document_snapshot(snap_a)
    boundary_repo.save_document_snapshot(snap_b)

    # Seed OKF assertions:
    # 1. doc_a old version v1 assertion ($10M)
    e_a1_id = compute_entity_id(ws_id, "doc_a", "alpha corp")
    ass_a1 = OkfPropertyAssertion(
        assertion_id="ass_a1",
        workspace_id=ws_id,
        entity_id=e_a1_id,
        entity_name="Alpha Corp",
        property_key="revenue",
        property_value="$10M",
        source_chunk_id="doc_a:c1",
        document_id="doc_a",
        document_version="v1",  # Old version!
        confidence_score=1.0,
        validation_status="verified",
        created_at=now,
    )
    okf_repo.save_manifest(ws_id, OkfManifest(
        workspace_id=ws_id, document_id="doc_a", document_version="v1",
        entities=(OkfEntityRecord(workspace_id=ws_id, entity_id=e_a1_id, entity_name="Alpha Corp", normalized_name="alpha corp", entity_type="organization", document_id="doc_a", document_version="v1", created_at=now),),
        assertions=(ass_a1,), status="ready", idempotency_key="k_a1", created_at=now,
    ))

    # 2. doc_a active version v2 assertion ($100M)
    e_a2_id = compute_entity_id(ws_id, "doc_a", "alpha corp")
    ass_a2 = OkfPropertyAssertion(
        assertion_id="ass_a2",
        workspace_id=ws_id,
        entity_id=e_a2_id,
        entity_name="Alpha Corp",
        property_key="revenue",
        property_value="$100M",
        source_chunk_id="doc_a:c2",
        document_id="doc_a",
        document_version="v2",  # Active version!
        confidence_score=1.0,
        validation_status="verified",
        created_at=now,
    )
    okf_repo.save_manifest(ws_id, OkfManifest(
        workspace_id=ws_id, document_id="doc_a", document_version="v2",
        entities=(OkfEntityRecord(workspace_id=ws_id, entity_id=e_a2_id, entity_name="Alpha Corp", normalized_name="alpha corp", entity_type="organization", document_id="doc_a", document_version="v2", created_at=now),),
        assertions=(ass_a2,), status="ready", idempotency_key="k_a2", created_at=now,
    ))

    # 3. doc_b active version v1 assertion ($200M)
    e_b1_id = compute_entity_id(ws_id, "doc_b", "alpha corp")
    ass_b1 = OkfPropertyAssertion(
        assertion_id="ass_b1",
        workspace_id=ws_id,
        entity_id=e_b1_id,
        entity_name="Alpha Corp",
        property_key="revenue",
        property_value="$200M",
        source_chunk_id="doc_b:c1",
        document_id="doc_b",
        document_version="v1",
        confidence_score=1.0,
        validation_status="verified",
        created_at=now,
    )
    okf_repo.save_manifest(ws_id, OkfManifest(
        workspace_id=ws_id, document_id="doc_b", document_version="v1",
        entities=(OkfEntityRecord(workspace_id=ws_id, entity_id=e_b1_id, entity_name="Alpha Corp", normalized_name="alpha corp", entity_type="organization", document_id="doc_b", document_version="v1", created_at=now),),
        assertions=(ass_b1,), status="ready", idempotency_key="k_b1", created_at=now,
    ))

    retriever = OkfRetriever(
        repository=okf_repo,
        boundary_repo=boundary_repo,
        chunk_repo=chunk_repo,
    )

    # Test 1: Query restricted to document_ids=["doc_a"]
    # Must return ONLY active v2 assertion ($100M) and NEVER old v1 ($10M) or doc_b ($200M)
    res_scoped_a = retriever.lookup_sync(
        workspace_id=ws_id,
        query="What is Alpha Corp revenue?",
        target_entities=["Alpha Corp"],
        document_ids=["doc_a"],
    )
    assert len(res_scoped_a.matched_assertions) == 1
    assert res_scoped_a.matched_assertions[0].property_value == "$100M"
    assert res_scoped_a.matched_assertions[0].document_version == "v2"
    assert res_scoped_a.candidate_chunk_ids == ("doc_a:c2",)

    # Test 2: Pipeline execution with state["document_ids"] = ["doc_b"]
    with patch("services.langgraph_pipeline._get_repo", return_value=chunk_repo), \
         patch("db.okf_repo.get_okf_repo", return_value=okf_repo), \
         patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo):
        pipeline_state = {
            "workspace_id": ws_id,
            "query": "What is Alpha Corp revenue?",
            "document_ids": ["doc_b"],
        }
        res_pipeline = run_okf_router(pipeline_state)

        assert res_pipeline["okf_match_count"] == 1
        assert res_pipeline["okf_candidate_ids"] == ["doc_b:c1"]
        matched_vals = [p["property_value"] for p in res_pipeline["okf_properties"]]
        assert matched_vals == ["$200M"]


def test_pipeline_failed_manifest_skipping():
    """Verify that documents with failed OKF manifests are cleanly skipped by OKF lookup."""
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    okf_repo = InMemoryOkfRepository()
    ws_id = "ws_failed_manifest"
    now = _now()

    # Document snapshot marked as okf_status="failed"
    snap_failed = DocumentSnapshot(
        workspace_id=ws_id,
        document_id="doc_err",
        source="error.pdf",
        version="v1",
        format="pdf",
        incomplete_coverage_flag=False,
        exclusions=(),
        persisted_rows=1,
        total_rows=1,
        created_at=now,
        okf_status="failed",
        okf_entities_count=0,
        okf_properties_count=0,
    )
    boundary_repo.save_document_snapshot(snap_failed)

    # Manifest in okf_repo marked status="failed"
    okf_repo.save_manifest(ws_id, OkfManifest(
        workspace_id=ws_id,
        document_id="doc_err",
        document_version="v1",
        entities=(),
        assertions=(),
        status="failed",
        idempotency_key="k_err",
        created_at=now,
    ))

    retriever = OkfRetriever(
        repository=okf_repo,
        boundary_repo=boundary_repo,
    )

    res = retriever.lookup_sync(
        workspace_id=ws_id,
        query="What is the revenue?",
        target_entities=["Revenue"],
        document_ids=["doc_err"],
    )
    assert len(res.matched_assertions) == 0
    assert len(res.candidate_chunk_ids) == 0
    assert res.execution_status == "completed"


# ==============================================================================
# 2. Document Level Extraction Budget & Retry Exhaustion (AC-3)
# ==============================================================================


def test_extraction_budget_enforces_call_limit_and_retry_exhaustion():
    """Verify document-level extraction budget caps provider attempts at 2 across retries."""
    reset_budget_ledger()
    repo = InMemoryOkfRepository()

    ch1 = Chunk(id="d_b:c1", document_id="d_b", source_format="pdf", text="Acme Corp report.", element_type="paragraph", page_number=1)
    ch2 = Chunk(id="d_b:c2", document_id="d_b", source_format="pdf", text="Acme Corp finances.", element_type="paragraph", page_number=2)

    # Bedrock client that throttles on every attempt (HTTP 429)
    mock_client = MagicMock()
    mock_client.converse.side_effect = Exception("ThrottlingException: Rate exceeded (HTTP 429)")

    builder = OkfBuilder(repo=repo, bedrock_client=mock_client)

    manifest = builder.extract_and_persist(
        workspace_id="ws_budget",
        document_id="d_b",
        document_version="v1",
        format="pdf",
        chunks=[ch1, ch2],
    )

    # Should make at most 2 calls (initial + 1 retry) and fail open per ADR-001
    assert mock_client.converse.call_count == 2
    assert manifest.status == "failed"
    assert manifest.model_calls == 2

    # Replaying the failed document version must NOT reset the budget silently
    mock_client.converse.reset_mock()
    replay_manifest = builder.extract_and_persist(
        workspace_id="ws_budget",
        document_id="d_b",
        document_version="v1",
        format="pdf",
        chunks=[ch1, ch2],
    )
    # Zero new Bedrock calls on replay of exhausted budget
    assert mock_client.converse.call_count == 0
    assert replay_manifest.status == "failed"
    assert replay_manifest.model_calls == 2


def test_concurrent_ingestion_shares_single_document_budget():
    """Verify concurrent ingestion requests for the same document version share one budget."""
    reset_budget_ledger()
    repo = InMemoryOkfRepository()

    ch = Chunk(id="d_c:c1", document_id="d_c", source_format="pdf", text="Acme Corp reported $5M revenue.", element_type="paragraph", page_number=1)

    call_count = 0
    lock = threading.Lock()

    def _fake_converse(*args, **kwargs):
        nonlocal call_count
        with lock:
            call_count += 1
        time.sleep(0.05)
        return {
            "output": {"message": {"content": [{"text": json.dumps([{
                "entity_name": "Acme Corp", "property_key": "revenue", "property_value": "$5M", "source_chunk_id": "d_c:c1"
            }])}]}},
            "usage": {"inputTokens": 100, "outputTokens": 20},
        }

    mock_client = MagicMock()
    mock_client.converse.side_effect = _fake_converse

    builder = OkfBuilder(repo=repo, bedrock_client=mock_client)

    def _worker():
        builder.extract_and_persist(
            workspace_id="ws_concurrent",
            document_id="d_c",
            document_version="v1",
            format="pdf",
            chunks=[ch],
        )

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Total model calls across concurrent threads must be exactly 1 (idempotent lock & shared budget)
    assert call_count == 1
    manifest = repo.get_manifest("ws_concurrent", "d_c", "v1")
    assert manifest is not None
    assert manifest.status == "ready"
    assert manifest.model_calls == 1


# ==============================================================================
# 3. Relational Grounding & Counterexample: Real Value Attributed to Wrong Entity
# ==============================================================================


def test_relational_grounding_rejects_real_value_attributed_to_wrong_entity():
    """Counterexample: $15M is a real value in the chunk text, but belongs to Acme Corp, not Beta LLC.
    
    Verifies that value occurrence alone does not accept an assertion when attributed to the wrong entity.
    """
    chunk_text = (
        "Acme Corp reported $15M in revenue for fiscal year 2025. "
        "Competitor Beta LLC posted $2M in revenue."
    )

    # 1. Attributing $15M to Acme Corp is valid and grounded
    is_valid_acme, reason_acme = validate_assertion_grounding(
        entity_name="Acme Corp",
        property_key="revenue",
        property_value="$15M",
        chunk_text=chunk_text,
    )
    assert is_valid_acme is True
    assert reason_acme == "verified"

    # 2. Counterexample: Attributing $15M to Beta LLC MUST BE REJECTED
    # $15M is in chunk_text, Beta LLC is in chunk_text, revenue is in chunk_text,
    # but the relationship is wrong!
    is_valid_beta, reason_beta = validate_assertion_grounding(
        entity_name="Beta LLC",
        property_key="revenue",
        property_value="$15M",
        chunk_text=chunk_text,
    )
    assert is_valid_beta is False
    assert reason_beta == "wrong_entity_attribution"

    # 3. Attributing $2M to Beta LLC is valid and grounded
    is_valid_beta_2m, reason_beta_2m = validate_assertion_grounding(
        entity_name="Beta LLC",
        property_key="revenue",
        property_value="$2M",
        chunk_text=chunk_text,
    )
    assert is_valid_beta_2m is True
    assert reason_beta_2m == "verified"

    # 4. Entity not mentioned in chunk at all
    is_valid_unmentioned, reason_unmentioned = validate_assertion_grounding(
        entity_name="Gamma Industries",
        property_key="revenue",
        property_value="$15M",
        chunk_text=chunk_text,
    )
    assert is_valid_unmentioned is False
    assert reason_unmentioned == "entity_not_in_chunk"


def test_builder_rejects_wrong_entity_in_extraction_pass():
    """Verify that OkfBuilder rejects wrong-entity counterexamples during PDF ingestion."""
    repo = InMemoryOkfRepository()

    ch = Chunk(
        id="doc_counter:c1",
        document_id="doc_counter",
        source_format="pdf",
        text="Acme Corp reported $15M in revenue for fiscal year 2025. Beta LLC posted $2M in revenue.",
        element_type="paragraph",
        page_number=1,
    )

    model_output = json.dumps([
        # Valid claim for Acme Corp
        {"entity_name": "Acme Corp", "property_key": "revenue", "property_value": "$15M", "source_chunk_id": "doc_counter:c1"},
        # Counterexample claim: Real value ($15M) attributed to WRONG entity (Beta LLC)
        {"entity_name": "Beta LLC", "property_key": "revenue", "property_value": "$15M", "source_chunk_id": "doc_counter:c1"},
        # Valid claim for Beta LLC
        {"entity_name": "Beta LLC", "property_key": "revenue", "property_value": "$2M", "source_chunk_id": "doc_counter:c1"},
    ])

    mock_bedrock = MagicMock()
    mock_bedrock.converse.return_value = {
        "output": {"message": {"content": [{"text": model_output}]}},
        "usage": {"inputTokens": 100, "outputTokens": 50},
    }

    builder = OkfBuilder(repo=repo, bedrock_client=mock_bedrock)
    manifest = builder.extract_and_persist(
        workspace_id="ws_counter",
        document_id="doc_counter",
        document_version="v1",
        format="pdf",
        chunks=[ch],
    )

    # Exactly 2 valid claims accepted, 1 counterexample rejected
    assert len(manifest.assertions) == 2
    assert manifest.rejected_assertions_count == 1

    accepted_pairs = {(a.entity_name, a.property_value) for a in manifest.assertions}
    assert accepted_pairs == {("Acme Corp", "$15M"), ("Beta LLC", "$2M")}
    assert ("Beta LLC", "$15M") not in accepted_pairs


def test_relational_grounding_accepts_capitalized_metric_terms_and_property_keys():
    """Verify that capitalized property keys and sentence initial metric terms do not trigger false rejection."""
    # 1. Capitalized metric term at start of sentence
    text1 = "Acme Corp had strong financial results. Total revenue reached $15M in 2025."
    valid1, reason1 = validate_assertion_grounding(
        entity_name="Acme Corp",
        property_key="revenue",
        property_value="$15M",
        chunk_text=text1,
    )
    assert valid1 is True
    assert reason1 == "verified"

    # 2. Operating income capitalized next to value
    text2 = "Acme Corp announced fiscal updates. Operating income was $5M."
    valid2, reason2 = validate_assertion_grounding(
        entity_name="Acme Corp",
        property_key="operating_income",
        property_value="$5M",
        chunk_text=text2,
    )
    assert valid2 is True
    assert reason2 == "verified"

    # 3. Capitalized property name in same sentence
    text3 = "In 2025, Acme Corp recorded Total Revenue of $15M."
    valid3, reason3 = validate_assertion_grounding(
        entity_name="Acme Corp",
        property_key="revenue",
        property_value="$15M",
        chunk_text=text3,
    )
    assert valid3 is True
    assert reason3 == "verified"


# ==============================================================================
# 4. Baseline Readiness Published Independently of Optional OKF (ADR-001)
# ==============================================================================


def test_baseline_readiness_published_independently_on_okf_failure():
    """Verify that document baseline status is published as Ready and available even if OKF fails."""
    repo = CloudRepository()
    boundary_repo = get_workspace_boundary_repo()

    doc_service = DocumentsService(repo=repo)

    ws_id = "ws_adr001_decoupling"
    doc_id = "doc_decoupled"
    filename = "report.pdf"
    content = b"%PDF-1.4 test document content for baseline readiness."

    boundary_repo.create_workspace(workspace_id=ws_id, name="Test WS", owner_principal_id="test_user")

    text = "Sample baseline document text for testing."
    ch = Chunk(
        id=f"{doc_id}:c1",
        document_id=doc_id,
        workspace_id=ws_id,
        text=text,
        source_format="pdf",
        element_type="paragraph",
        page_number=1,
        embedding_fast=embed_fast_local(text),
        embedding_full=_deterministic_vector(text, 1024),
    )
    parsed_doc = Document(id=doc_id, filename=filename, source_format="pdf", chunks=[ch], workspace_id=ws_id)
    repo.add_placeholder_document(workspace_id=ws_id, doc_id=doc_id, filename=filename, format_str="pdf", size_str="100 KB")

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # Mock ingest_document to return parsed document and simulate OKF extraction failure
        with patch("api.routes.ingest_document", return_value=parsed_doc), \
             patch("modules.documents.documents_service.ingest_document", return_value=parsed_doc), \
             patch("ingestion.okf_builder.extract_and_persist_okf", side_effect=RuntimeError("Bedrock unavailable")):
            doc_service.background_ingest(
                temp_path_str=tmp_path,
                workspace_id=ws_id,
                doc_id=doc_id,
                filename=filename,
                content=content,
                size_str="100 KB",
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # 1. Document record and chunks must be published and accessible in repository
    doc_record = repo.get(doc_id)
    assert doc_record is not None
    assert len(doc_record.chunks) == 1

    # Document status in workspace must be published as Ready
    from modules.documents.documents_repository import _WORKSPACE_DOCS
    doc_entry = next(d for d in _WORKSPACE_DOCS[ws_id] if str(d.get("id")) == str(doc_id))
    assert doc_entry["status"] == "Ready"

    # 2. DocumentSnapshot must exist with okf_status="failed", preserving baseline
    snapshot = boundary_repo.get_document_snapshot(ws_id, doc_id)
    assert snapshot is not None
    assert snapshot.okf_status == "failed"
    assert snapshot.persisted_rows >= 0


# ==============================================================================
# 5. Complete Entity-Property-Value Relational Grounding (AC-2)
# ==============================================================================


def test_complete_entity_property_value_grounding_reproducer():
    """Reproducer: 'Acme reported revenue of 100.'
    
    revenue=100 passes; employee_count=100 fails.
    Rejects unsupported relationships without relying on value/entity co-occurrence alone.
    """
    text = "Acme reported revenue of 100."

    # 1. revenue=100 is supported by entity, property, and value
    ok_rev, reason_rev = validate_assertion_grounding(
        entity_name="Acme",
        property_key="revenue",
        property_value="100",
        chunk_text=text,
    )
    assert ok_rev is True
    assert reason_rev == "verified"

    # 2. employee_count=100 must fail (property relationship unsupported in text)
    ok_emp, reason_emp = validate_assertion_grounding(
        entity_name="Acme",
        property_key="employee_count",
        property_value="100",
        chunk_text=text,
    )
    assert ok_emp is False
    assert reason_emp == "unsupported_property"

    # 3. Multi-sentence test: Value 100 is in the revenue sentence, not the employee sentence
    multi_sentence_text = "Acme has 500 employees. Acme reported revenue of 100."
    ok_multi_rev, reason_multi_rev = validate_assertion_grounding(
        entity_name="Acme",
        property_key="revenue",
        property_value="100",
        chunk_text=multi_sentence_text,
    )
    assert ok_multi_rev is True
    assert reason_multi_rev == "verified"

    ok_multi_emp500, reason_multi_emp500 = validate_assertion_grounding(
        entity_name="Acme",
        property_key="employee_count",
        property_value="500",
        chunk_text=multi_sentence_text,
    )
    assert ok_multi_emp500 is True
    assert reason_multi_emp500 == "verified"

    # Attributing employee_count=100 must fail because 100 is tied to revenue, not employees
    ok_multi_emp100, reason_multi_emp100 = validate_assertion_grounding(
        entity_name="Acme",
        property_key="employee_count",
        property_value="100",
        chunk_text=multi_sentence_text,
    )
    assert ok_multi_emp100 is False
    assert reason_multi_emp100 == "unsupported_relationship"


# ==============================================================================
# 6. Fail-Closed Optional OKF Retrieval on Missing/Unavailable Metadata
# ==============================================================================


def test_retriever_fails_closed_when_snapshot_lookup_raises_error():
    """Verify that when snapshot discovery raises an error, retrieval fails closed to baseline."""
    repo = InMemoryOkfRepository()
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    retriever = OkfRetriever(repository=repo, boundary_repo=boundary_repo)

    ws_id = "ws_fail_closed_err"
    # Seed an entity and ready manifest
    now = "2026-10-26T12:00:00Z"
    ent = OkfEntityRecord(
        workspace_id=ws_id,
        entity_id="e_acme",
        entity_name="Acme Corp",
        normalized_name="acme corp",
        entity_type="organization",
        document_id="doc_fc",
        document_version="v1",
        created_at=now,
    )
    ass = OkfPropertyAssertion(
        assertion_id="ass_1",
        workspace_id=ws_id,
        entity_id="e_acme",
        entity_name="Acme Corp",
        property_key="revenue",
        property_value="$10M",
        value_type="string",
        source_chunk_id="chk_1",
        document_id="doc_fc",
        document_version="v1",
        created_at=now,
    )
    repo.save_manifest(ws_id, OkfManifest(
        workspace_id=ws_id,
        document_id="doc_fc",
        document_version="v1",
        entities=(ent,),
        assertions=(ass,),
        status="ready",
        idempotency_key="k_fc",
        created_at=now,
    ))

    # Mock list_document_snapshots to throw an unexpected exception
    with patch.object(boundary_repo, "list_document_snapshots", side_effect=RuntimeError("DynamoDB boundary unavailable")):
        res = retriever.lookup_sync(
            workspace_id=ws_id,
            query="What is Acme Corp revenue?",
            target_entities=["Acme Corp"],
        )

    # Must fail closed: entity_state="none", property_state="none", no assertions
    assert res.entity_state == "none"
    assert res.property_state == "none"
    assert len(res.matched_assertions) == 0
    assert len(res.candidate_chunk_ids) == 0


def test_retriever_fails_closed_when_manifest_metadata_is_missing():
    """Verify that entities without a ready manifest in okf_repo are rejected (missing metadata fails closed)."""
    repo = InMemoryOkfRepository()
    retriever = OkfRetriever(repository=repo)

    ws_id = "ws_missing_metadata"
    now = "2026-10-26T12:00:00Z"
    ent = OkfEntityRecord(
        workspace_id=ws_id,
        entity_id="e_orphaned",
        entity_name="Orphaned Entity",
        normalized_name="orphaned entity",
        entity_type="organization",
        document_id="doc_orphaned",
        document_version="v1",
        created_at=now,
    )
    ass = OkfPropertyAssertion(
        assertion_id="ass_orphaned",
        workspace_id=ws_id,
        entity_id="e_orphaned",
        entity_name="Orphaned Entity",
        property_key="revenue",
        property_value="$99M",
        value_type="string",
        source_chunk_id="chk_orphaned",
        document_id="doc_orphaned",
        document_version="v1",
        created_at=now,
    )
    # Save entity and assertion directly without creating a manifest in okf_repo
    repo._entities[(ws_id, ent.entity_id, ent.document_id, ent.document_version)] = ent
    repo._assertions[(ws_id, ass.entity_id, ass.assertion_id)] = ass

    res = retriever.lookup_sync(
        workspace_id=ws_id,
        query="What is Orphaned Entity revenue?",
        target_entities=["Orphaned Entity"],
    )

    # Missing manifest must reject the unverified assertion
    assert res.entity_state == "none"
    assert res.property_state == "none"
    assert len(res.matched_assertions) == 0


# ==============================================================================
# 7. Complete Upfront Budget Reservation & Shared Deadline Across Retries
# ==============================================================================


def test_upfront_budget_reservation_and_persistence_across_replays():
    """Verify complete prompt/output budget reservation, shared deadline across retries, and attempt persistence."""
    # 1. Test upfront budget reservation limits
    budget = DocumentExtractionBudget(
        workspace_id="ws_b",
        document_id="doc_b",
        document_version="v1",
        max_calls=2,
        max_input_tokens=4000,
        max_output_tokens=1200,
        max_elapsed_s=10.0,
    )

    # Reserve call 1 (2500 input tokens, 600 output tokens) -> fits
    assert budget.reserve_call(prompt_tokens=2500, max_output_tokens=600) is True
    assert budget.calls_made == 1
    assert budget.input_tokens_used == 2500
    assert budget.output_tokens_used == 600

    # Reserve call 2 (2000 input tokens) -> would exceed 4000 max_input_tokens -> rejected upfront
    assert budget.reserve_call(prompt_tokens=2000, max_output_tokens=600) is False
    assert budget.exhausted is True
    assert "input_token_limit_exceeded" in (budget.exhaustion_reason or "")

    # 2. Shared document deadline across retry delays
    repo = InMemoryOkfRepository()
    mock_bedrock = MagicMock()
    # Simulate throttling error
    mock_bedrock.converse.side_effect = Exception("ThrottlingException: HTTP 429 Too Many Requests")

    ch = Chunk(
        id="doc_deadline:c1",
        document_id="doc_deadline",
        source_format="pdf",
        text="Acme Corp reported $15M in revenue.",
        element_type="paragraph",
        page_number=1,
    )

    builder = OkfBuilder(repo=repo, bedrock_client=mock_bedrock)

    # Create budget with only 0.8s remaining (started 9.2s ago with 10.0s deadline)
    budget_tight = DocumentExtractionBudget(
        workspace_id="ws_tight",
        document_id="doc_tight",
        document_version="v1",
        max_elapsed_s=10.0,
        started_at=time.perf_counter() - 9.2,
    )

    t_start = time.perf_counter()
    prompt_out, stats = builder._invoke_bedrock_extraction(
        client=mock_bedrock,
        chunks=[ch],
        workspace_id="ws_tight",
        document_id="doc_tight",
        budget=budget_tight,
    )
    t_elapsed = time.perf_counter() - t_start

    # Must NOT sleep 2.0s when doing so would exceed the 10.0s deadline
    assert t_elapsed < 1.5
    assert budget_tight.exhausted is True
    assert "retry_would_exceed_deadline" in (budget_tight.exhaustion_reason or "")

    # 3. Preserve attempt accounting across restart/replay without silent reset
    ws_replay = "ws_replay_persist"
    doc_replay = "doc_replay_persist"
    ver_replay = "v1"
    now = "2026-10-26T12:00:00Z"

    # Pre-save a failed manifest with exhausted model calls (2 calls)
    failed_man = OkfManifest(
        workspace_id=ws_replay,
        document_id=doc_replay,
        document_version=ver_replay,
        entities=(),
        assertions=(),
        status="failed",
        model_calls=2,
        extraction_tokens=3500,
        idempotency_key="k_rep",
        created_at=now,
    )
    repo.save_manifest(ws_replay, failed_man)

    # Simulate service restart by wiping in-memory ledger
    reset_budget_ledger()

    # Attempt re-extraction with fresh mock client
    mock_call_tracker = MagicMock()
    replay_builder = OkfBuilder(repo=repo, bedrock_client=mock_call_tracker)
    man_result = replay_builder.extract_and_persist(
        workspace_id=ws_replay,
        document_id=doc_replay,
        document_version=ver_replay,
        format="pdf",
        chunks=[ch],
        force_reextract=False,
    )

    # Zero model calls made: durable manifest attempt count is preserved across restart
    assert mock_call_tracker.converse.call_count == 0
    assert man_result.status == "failed"
    assert man_result.model_calls == 2


def test_budget_ledger_bounded_capacity_and_eviction():
    """Verify that extraction budget ledger remains strictly bounded under capacity limits.

    1. When capacity is reached and all budgets are exhausted, eviction operates without unbounded growth.
    2. When unexhausted and exhausted budgets coexist, exhausted budgets are evicted preferentially.
    """
    reset_budget_ledger()
    test_capacity = 4

    with patch("ingestion.okf_builder._MAX_BUDGET_LEDGER_ENTRIES", test_capacity):
        # 1. Fill ledger up to capacity with exhausted budgets
        for i in range(test_capacity):
            b = get_or_create_document_budget(
                workspace_id="ws_b",
                document_id=f"doc_{i}",
                document_version="v1",
                idempotency_key=f"k_{i}",
            )
            b.exhausted = True
            b.exhaustion_reason = f"exhausted_{i}"

        assert len(_EXTRACTION_BUDGETS) == test_capacity

        # Add 5th budget: must evict an exhausted entry and stay strictly at capacity
        b5 = get_or_create_document_budget(
            workspace_id="ws_b",
            document_id="doc_new",
            document_version="v1",
            idempotency_key="k_new",
        )
        assert len(_EXTRACTION_BUDGETS) == test_capacity
        assert "k_new" in _EXTRACTION_BUDGETS
        assert "k_0" not in _EXTRACTION_BUDGETS  # Oldest exhausted evicted

        # 2. Preferential eviction: add unexhausted budget, then exhausted, verify exhausted evicted first
        reset_budget_ledger()
        # Create 2 unexhausted budgets
        b_act1 = get_or_create_document_budget("ws_b", "doc_act1", "v1", "k_act1")
        b_act2 = get_or_create_document_budget("ws_b", "doc_act2", "v1", "k_act2")

        # Create 2 exhausted budgets
        b_ex1 = get_or_create_document_budget("ws_b", "doc_ex1", "v1", "k_ex1")
        b_ex1.exhausted = True
        b_ex2 = get_or_create_document_budget("ws_b", "doc_ex2", "v1", "k_ex2")
        b_ex2.exhausted = True

        assert len(_EXTRACTION_BUDGETS) == test_capacity

        # Adding a 5th budget should evict exhausted 'k_ex1' rather than active 'k_act1'
        b_next = get_or_create_document_budget("ws_b", "doc_next", "v1", "k_next")
        assert len(_EXTRACTION_BUDGETS) == test_capacity
        assert "k_ex1" not in _EXTRACTION_BUDGETS
        assert "k_act1" in _EXTRACTION_BUDGETS
        assert "k_act2" in _EXTRACTION_BUDGETS


def test_repository_manifest_scope_anchoring_parity(tmp_path):
    """Verify that InMemoryOkfRepository and SqliteOkfRepository have identical scoping behavior.

    When manifest entities or assertions have mismatched document or version attributes,
    both repositories anchor them to the manifest scope (workspace, document_id, version).
    """
    ws_id = "ws_parity"
    now = _now()
    doc_target = "doc_correct"
    ver_target = "v2"

    mismatched_ent = OkfEntityRecord(
        workspace_id=ws_id,
        entity_id="e_mismatch",
        entity_name="Parity Test Corp",
        normalized_name="parity test corp",
        entity_type="organization",
        document_id="doc_wrong",
        document_version="v1",
        created_at=now,
    )
    mismatched_ass = OkfPropertyAssertion(
        assertion_id="ass_mismatch",
        workspace_id=ws_id,
        entity_id="e_mismatch",
        entity_name="Parity Test Corp",
        property_key="revenue",
        property_value="$42M",
        value_type="string",
        source_chunk_id="chk_p1",
        document_id="doc_wrong",
        document_version="v1",
        created_at=now,
    )

    manifest = OkfManifest(
        workspace_id=ws_id,
        document_id=doc_target,
        document_version=ver_target,
        entities=(mismatched_ent,),
        assertions=(mismatched_ass,),
        status="ready",
        idempotency_key="k_parity",
        created_at=now,
    )

    # 1. In-Memory repo
    in_mem_repo = InMemoryOkfRepository()
    in_mem_repo.save_manifest(ws_id, manifest)
    in_mem_ass, _ = in_mem_repo.list_assertions_by_document(ws_id, doc_target, ver_target)
    assert len(in_mem_ass) == 1
    assert in_mem_ass[0].document_id == doc_target
    assert in_mem_ass[0].document_version == ver_target

    # 2. SQLite repo
    db_path = tmp_path / "parity_test.db"
    sqlite_repo = SQLiteOkfRepository(db_path=db_path)
    sqlite_repo.save_manifest(ws_id, manifest)
    sqlite_ass, _ = sqlite_repo.list_assertions_by_document(ws_id, doc_target, ver_target)
    assert len(sqlite_ass) == 1
    assert sqlite_ass[0].document_id == doc_target
    assert sqlite_ass[0].document_version == ver_target


# ==============================================================================
# 9. Grounding Reproducer, Fail Closed Metadata, Complete Budget & Deadline
# ==============================================================================


def test_competing_property_relational_grounding_reproducer():
    """Reproduce grounding counterexample: 'Acme reported revenue of 100 and profit of 20.'

    revenue=100 passes; revenue=20 fails.
    profit=20 passes; profit=100 fails.
    Bind entity, property, and value together; refuse ambiguous relations.
    """
    text = "Acme reported revenue of 100 and profit of 20."

    # 1. revenue=100 is validly bound
    ok_rev100, reason_rev100 = validate_assertion_grounding(
        entity_name="Acme",
        property_key="revenue",
        property_value="100",
        chunk_text=text,
    )
    assert ok_rev100 is True
    assert reason_rev100 == "verified"

    # 2. revenue=20 must fail (20 is bound to profit, not revenue)
    ok_rev20, reason_rev20 = validate_assertion_grounding(
        entity_name="Acme",
        property_key="revenue",
        property_value="20",
        chunk_text=text,
    )
    assert ok_rev20 is False
    assert reason_rev20 == "unsupported_relationship"

    # 3. profit=20 is validly bound
    ok_prof20, reason_prof20 = validate_assertion_grounding(
        entity_name="Acme",
        property_key="profit",
        property_value="20",
        chunk_text=text,
    )
    assert ok_prof20 is True
    assert reason_prof20 == "verified"

    # 4. profit=100 must fail (100 is bound to revenue, not profit)
    ok_prof100, reason_prof100 = validate_assertion_grounding(
        entity_name="Acme",
        property_key="profit",
        property_value="100",
        chunk_text=text,
    )
    assert ok_prof100 is False
    assert reason_prof100 == "unsupported_relationship"

    # 5. Inverted clause order: 'Acme reported profit of 20 and revenue of 100.'
    inverted_text = "Acme reported profit of 20 and revenue of 100."
    assert validate_assertion_grounding("Acme", "revenue", "100", inverted_text)[0] is True
    assert validate_assertion_grounding("Acme", "revenue", "20", inverted_text)[0] is False
    assert validate_assertion_grounding("Acme", "profit", "20", inverted_text)[0] is True
    assert validate_assertion_grounding("Acme", "profit", "100", inverted_text)[0] is False

    # 6. Ambiguous compound attribution: competing property intervenes between target and value
    ambiguous_text = "Acme reported revenue and profit of 100."
    ok_amb_rev, reason_amb_rev = validate_assertion_grounding(
        entity_name="Acme",
        property_key="revenue",
        property_value="100",
        chunk_text=ambiguous_text,
    )
    # Revenue is separated from 100 by intervening competing property profit
    assert ok_amb_rev is False
    assert reason_amb_rev == "unsupported_relationship"


def test_empty_or_missing_snapshots_yields_zero_okf_candidates():
    """Empty or missing snapshots in boundary repo must yield no OKF candidates.

    Manifest-only fallback must not authorize assertions without active snapshot proof.
    """
    repo = InMemoryOkfRepository()
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    retriever = OkfRetriever(repository=repo, boundary_repo=boundary_repo)

    ws_id = "ws_empty_snaps"
    now = _now()
    ent = OkfEntityRecord(
        workspace_id=ws_id,
        entity_id="e_ghost",
        entity_name="Ghost Corp",
        normalized_name="ghost corp",
        entity_type="organization",
        document_id="doc_ghost",
        document_version="v1",
        created_at=now,
    )
    ass = OkfPropertyAssertion(
        assertion_id="ass_ghost",
        workspace_id=ws_id,
        entity_id="e_ghost",
        entity_name="Ghost Corp",
        property_key="revenue",
        property_value="$10M",
        value_type="string",
        source_chunk_id="chk_ghost",
        document_id="doc_ghost",
        document_version="v1",
        created_at=now,
    )
    # Manifest exists in okf_repo with status ready, but NO snapshots exist in boundary_repo
    repo.save_manifest(ws_id, OkfManifest(
        workspace_id=ws_id,
        document_id="doc_ghost",
        document_version="v1",
        entities=(ent,),
        assertions=(ass,),
        status="ready",
        idempotency_key="k_ghost",
        created_at=now,
    ))

    # Query with zero snapshots present
    res = retriever.lookup_sync(
        workspace_id=ws_id,
        query="What is Ghost Corp revenue?",
        target_entities=["Ghost Corp"],
    )

    # Must fail closed: 0 matched assertions, entity_state none, property_state none
    assert res.entity_state == "none"
    assert res.property_state == "none"
    assert len(res.matched_assertions) == 0
    assert len(res.candidate_chunk_ids) == 0

    # Case B: Snapshot exists but okf_status is failed
    boundary_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws_id,
        document_id="doc_ghost",
        source="doc_ghost.pdf",
        version="v1",
        format="pdf",
        created_at=now,
        okf_status="failed",
    ))
    res_failed = retriever.lookup_sync(
        workspace_id=ws_id,
        query="What is Ghost Corp revenue?",
        target_entities=["Ghost Corp"],
    )
    assert res_failed.entity_state == "none"
    assert len(res_failed.matched_assertions) == 0

    # Case C: Snapshot has active version v2, but manifest is for v1
    boundary_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws_id,
        document_id="doc_ghost",
        source="doc_ghost.pdf",
        version="v2",
        format="pdf",
        created_at=now,
        okf_status="ready",
    ))
    res_stale = retriever.lookup_sync(
        workspace_id=ws_id,
        query="What is Ghost Corp revenue?",
        target_entities=["Ghost Corp"],
    )
    # Manifest for v2 does not exist, so v1 manifest is not authorized
    assert res_stale.entity_state == "none"
    assert len(res_stale.matched_assertions) == 0


def test_complete_provider_request_budgeting_including_system_prompt():
    """Budget the complete provider request including system instructions and framing overhead."""
    system_prompt = (
        "You are an Open Knowledge Fact extractor. Extract entities and properties. "
        "Return valid JSON matching the schema."
    )
    user_prompt = "<source_chunk id=\"chk_1\">\nAcme reported revenue of 100.\n</source_chunk>"

    counted = count_request_tokens(system_prompt, user_prompt)
    full_text = f"{system_prompt}\n\n{user_prompt}"

    # Verify conservative bound dominates character ratio
    assert counted >= len(full_text) // 4
    assert counted >= math.ceil(len(full_text) / 3.0) + 16

    # Verify upfront reservation rejects requests when total request exceeds input token budget
    budget = DocumentExtractionBudget(
        workspace_id="ws_b",
        document_id="doc_b",
        document_version="v1",
        max_calls=2,
        max_input_tokens=counted - 1,  # Not enough tokens to cover complete request
        max_output_tokens=600,
        max_elapsed_s=10.0,
    )

    can_reserve = budget.reserve_call(counted, max_output_tokens=600)
    assert can_reserve is False
    assert budget.exhausted is True
    assert "input_token_limit_exceeded" in budget.exhaustion_reason


def test_blocking_provider_call_enforces_remaining_document_deadline():
    """Enforce the remaining document deadline across actual blocking provider calls with zero retries."""
    builder = OkfBuilder()
    mock_client = MagicMock()

    # Simulate a slow client call that takes 0.5s
    def slow_converse(**kwargs):
        time.sleep(0.5)
        return {
            "output": {"message": {"content": [{"text": "{}"}]}},
            "usage": {"inputTokens": 100, "outputTokens": 50},
        }

    mock_client.converse.side_effect = slow_converse

    # Create budget with an elapsed deadline of only 0.05s
    budget = DocumentExtractionBudget(
        workspace_id="ws_dead",
        document_id="doc_dead",
        document_version="v1",
        max_calls=2,
        max_input_tokens=4000,
        max_output_tokens=1200,
        max_elapsed_s=0.05,
    )

    test_chunk = Chunk(
        id="chk_d1",
        document_id="doc_dead",
        source_format="pdf",
        text="Acme revenue is 100.",
        element_type="text",
        metadata={"token_count": 5},
    )

    with patch.object(builder, "_get_bedrock_client", return_value=mock_client):
        raw_json, usage = builder._invoke_bedrock_extraction(
            chunks=[test_chunk],
            workspace_id="ws_dead",
            document_id="doc_dead",
            client=mock_client,
            budget=budget,
        )

    # Verification: Call timed out cleanly, budget exhausted, zero retries
    assert raw_json == ""
    assert usage == {"input_tokens": 0, "output_tokens": 0}
    assert budget.exhausted is True
    assert "document_deadline_exceeded" in budget.exhaustion_reason or "elapsed_time_limit_reached" in budget.exhaustion_reason
    # Because timeout triggers zero retries per AC-3, client.converse was called at most once
    assert mock_client.converse.call_count <= 1


# ==============================================================================
# 10. Provider Concurrency, Token Bound Defensibility, and Attempt Persistence
# ==============================================================================


def test_token_bound_never_underestimates_and_catches_heuristic_failures():
    """Verify defensible token bounds prevent underestimation from character heuristics."""
    system_prompt = "You are an extractor."

    # 1. Single character spaced sequence where len // 4 severely underestimates
    user_prompt_sparse = "a b c d e f g h i j k l m n o p q r s t u v w x y z"
    full_text_sparse = f"{system_prompt}\n\n{user_prompt_sparse}"

    heuristic_div4 = len(full_text_sparse) // 4
    heuristic_div3 = math.ceil(len(full_text_sparse) / 3.0)
    bound = count_request_tokens(system_prompt, user_prompt_sparse)

    # Prove character heuristics underestimate
    assert heuristic_div4 < 26  # Underestimates single character token count
    assert bound >= len(full_text_sparse.encode("utf-8")) + 32
    assert bound > heuristic_div4
    assert bound > heuristic_div3

    # 2. Punctuation dense sequence where each symbol represents a distinct token
    user_prompt_punct = "! , . ? ; : ( ) [ ] { } / @ # $ % ^ & * + = < >"
    full_text_punct = f"{system_prompt}\n\n{user_prompt_punct}"
    punct_div4 = len(full_text_punct) // 4
    bound_punct = count_request_tokens(system_prompt, user_prompt_punct)
    assert bound_punct > punct_div4 * 2

    # 3. Model configuration validation fails closed on unsupported models
    with pytest.raises(ValueError, match="Unsupported model configuration"):
        count_request_tokens(system_prompt, user_prompt_sparse, model_id="unsupported.fake.model")

    # 4. In OkfBuilder, unsupported model configuration fails closed with zero calls
    builder = OkfBuilder()
    mock_bedrock = MagicMock()
    with patch("config.settings.OKF_BEDROCK_MODEL_ID", "unsupported.model.v1"):
        budget = DocumentExtractionBudget(
            workspace_id="ws_unsup",
            document_id="doc_unsup",
            document_version="v1",
            max_calls=2,
            max_input_tokens=4000,
            max_output_tokens=1200,
            max_elapsed_s=10.0,
        )
        test_chunk = Chunk(
            id="chk_u1",
            document_id="doc_unsup",
            source_format="pdf",
            text="Acme revenue is 100.",
            element_type="text",
        )
        raw_out, stats = builder._invoke_bedrock_extraction(
            client=mock_bedrock,
            chunks=[test_chunk],
            workspace_id="ws_unsup",
            document_id="doc_unsup",
            budget=budget,
        )
        assert raw_out == ""
        assert stats == {"input_tokens": 0, "output_tokens": 0}
        assert budget.exhausted is True
        assert "unsupported_model_configuration" in (budget.exhaustion_reason or "")
        assert mock_bedrock.converse.call_count == 0


def test_concurrent_provider_calls_bounded_across_documents():
    """Verify provider call concurrency across documents is strictly bounded by controller."""
    controller = OkfProviderConcurrencyController(max_concurrency=2, max_queue=4)
    active_tracker: list[int] = []
    tracker_lock = threading.Lock()

    def slow_provider_work(*args, **kwargs):
        with tracker_lock:
            active_tracker.append(controller.active_count)
        time.sleep(0.08)
        return {"output": {"message": {"content": [{"text": "[]"}]}}, "usage": {"inputTokens": 50, "outputTokens": 10}}

    mock_client = MagicMock()
    mock_client.converse.side_effect = slow_provider_work

    builder = OkfBuilder(bedrock_client=mock_client, provider_controller=controller)

    def run_doc(doc_id: str):
        test_chunk = Chunk(
            id=f"chk_{doc_id}",
            document_id=doc_id,
            source_format="pdf",
            text="Corporate revenue was 500.",
            element_type="text",
        )
        budget = DocumentExtractionBudget(
            workspace_id="ws_conc",
            document_id=doc_id,
            document_version="v1",
            max_calls=2,
            max_input_tokens=4000,
            max_output_tokens=1200,
            max_elapsed_s=10.0,
        )
        return builder._invoke_bedrock_extraction(
            client=mock_client,
            chunks=[test_chunk],
            workspace_id="ws_conc",
            document_id=doc_id,
            budget=budget,
        )

    threads = []
    results = [None] * 4
    for i in range(4):
        def worker(idx=i):
            results[idx] = run_doc(f"doc_{idx}")
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Verify concurrency was strictly bounded to at most 2 at all times
    assert len(active_tracker) == 4
    assert max(active_tracker) <= 2
    # All 4 documents succeeded through bounded admission
    for res in results:
        assert res is not None
        assert res[0] == "[]"


def test_provider_timeout_capacity_retention_blocks_immediate_new_calls():
    """Verify that when a caller times out, worker capacity is retained until underlying work finishes."""
    controller = OkfProviderConcurrencyController(max_concurrency=1, max_queue=2)

    underlying_completed = threading.Event()

    def very_slow_provider_call(*args, **kwargs):
        time.sleep(0.25)
        underlying_completed.set()
        return {"result": "success"}

    # Caller 1 starts with a tight 0.05s deadline
    caller1_result: list[Any] = []

    def caller1():
        res, status = controller.run_provider_call(very_slow_provider_call, timeout_s=0.05)
        caller1_result.append((res, status))

    t1 = threading.Thread(target=caller1)
    t1.start()
    t1.join()

    # Caller 1 timed out
    assert len(caller1_result) == 1
    assert caller1_result[0][1] == "caller_timeout"
    # But underlying provider work is still executing in the background!
    assert not underlying_completed.is_set()
    assert controller.active_count == 1

    # Caller 2 immediately attempts to acquire a slot with a 0.05s timeout
    caller2_result: list[Any] = []

    def caller2():
        res, status = controller.run_provider_call(lambda: "instant", timeout_s=0.05)
        caller2_result.append((res, status))

    t2 = threading.Thread(target=caller2)
    t2.start()
    t2.join()

    # Caller 2 could NOT acquire the slot because capacity was retained by caller 1's ongoing work
    assert len(caller2_result) == 1
    assert caller2_result[0][1] == "caller_timeout"

    # Wait until underlying provider work actually completes
    finished = underlying_completed.wait(timeout=1.0)
    assert finished is True
    time.sleep(0.02)  # brief settle for lock release

    # Now Caller 3 can acquire the slot immediately and succeed
    res3, status3 = controller.run_provider_call(lambda: "success_c3", timeout_s=0.1)
    assert status3 == "completed"
    assert res3 == "success_c3"


def test_attempt_reservation_survives_crash_and_restart():
    """Verify attempt reservations persist to durable store before calls and survive simulated crashes."""
    repo = InMemoryOkfRepository()
    ws_id = "ws_crash_test"
    doc_id = "doc_crash_test"
    doc_ver = "v1"

    builder = OkfBuilder(repo=repo)
    budget = get_or_create_document_budget(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=doc_ver,
        idempotency_key=f"{ws_id}:{doc_id}:{doc_ver}",
        repo=repo,
    )

    # Mock client that raises an unexpected crash exception during call 1
    mock_crash_client = MagicMock()
    mock_crash_client.converse.side_effect = RuntimeError("Fatal worker process crash simulation")

    test_chunk = Chunk(
        id="chk_c1",
        document_id=doc_id,
        source_format="pdf",
        text="Acme revenue is 100.",
        element_type="text",
    )

    builder._invoke_bedrock_extraction(
        client=mock_crash_client,
        chunks=[test_chunk],
        workspace_id=ws_id,
        document_id=doc_id,
        budget=budget,
    )

    # 1. Verify durable repository persisted the attempt reservation before or during failure
    durable_man = repo.get_manifest(ws_id, doc_id, doc_ver)
    assert durable_man is not None
    assert durable_man.model_calls == 1
    assert durable_man.status == "failed"

    # 2. Simulate total service crash by clearing in memory ledger
    reset_budget_ledger()

    # 3. Create fresh builder and budget after restart
    recovered_budget = get_or_create_document_budget(
        workspace_id=ws_id,
        document_id=doc_id,
        document_version=doc_ver,
        idempotency_key=f"{ws_id}:{doc_id}:{doc_ver}",
        repo=repo,
    )

    # Attempt count was recovered from durable storage and was not silently reset
    assert recovered_budget.calls_made == 1
    assert recovered_budget.exhausted is True
    assert "prior_recorded_attempts" in (recovered_budget.exhaustion_reason or "")
