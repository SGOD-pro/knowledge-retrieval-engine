"""Unit and regression tests for Bounded Knowledge Graph Enrichment Slice 2.

Covers:
1. Grounding counterexamples (reversed active voice, reversed passive voice, negation, disjoint sentences, malformed JSON, hallucinated entities, unknown chunk IDs, co-occurrence alone).
2. CountTokens failures (missing capability, zero/negative count, exceptions, prompt token ceiling exceeding 2000).
3. Concurrent reservations and lease takeover (15-second lease window, active lease held, monotonic fence tokens).
4. Stale worker fencing (worker timeout, competitor lease takeover, stale worker fenced out, token expenditure reconciled into ledger, manifest commit rejected).
5. Uncertain timeouts & client socket timeouts (recovery with strongly consistent reads, durable token preservation).
6. Restart recovery & retry ceilings (resuming with existing reservations, throttling 429 retry counting against 2-call ceiling, zero retries on non-throttling errors).
7. Reconciliation failures (failing closed, discarding candidate relationships).
8. Ready-publication preservation (new attempt failures do not clobber existing ready manifests, spent budget retained).
9. Atomic storage behavior across independent SQLite connections.
10. Atomic storage behavior across DynamoDB Local with conditional updates.
11. TableStore row evidence hydration loop deadline check (Nit 2).
12. Clean SQLite configuration separation (Nit 1).
"""

from __future__ import annotations

import decimal
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from config import settings
from src.db.knowledge_graph_repo import (
    DynamoDBKnowledgeGraphRepository,
    FencingConflictError,
    InMemoryKnowledgeGraphRepository,
    SQLiteKnowledgeGraphRepository,
    disable_client_retries,
)
from src.ingestion.graph_builder import (
    KnowledgeGraphBuilder,
    extract_pdf_relationships,
    get_kg_provider_controller,
    reset_kg_provider_controller,
    validate_chunk_provenance,
    validate_relationship_grounding,
)
from src.ingestion.parse_service import parse_file
from src.schemas.contracts.knowledge_graph import (
    KnowledgeGraphBudgetLedger,
    KnowledgeGraphEntity,
    KnowledgeGraphManifest,
    KnowledgeGraphRelationship,
    PinnedDocumentVersion,
    PinnedSnapshotContext,
    canonicalize_predicate,
    compute_entity_id,
    compute_relationship_id,
    generate_publication_id,
)
from src.schemas.contracts.location import DocumentLocation, TableLocation
from src.schemas.models import Chunk
from src.services.retrieval.knowledge_graph_retriever import (
    GraphConcurrencyController,
    KnowledgeGraphRetriever,
)


def make_chunk(
    id: str = "chunk_1",
    document_id: str = "doc_1",
    text: str = "Acme acquired Beta",
    page_number: int | None = 1,
    workspace_id: str = "ws_test",
    document_version: str | None = "v1",
    metadata: dict[str, Any] | None = None,
) -> Chunk:
    meta = dict(metadata) if metadata is not None else {}
    if document_version is not None:
        if "document_version" not in meta:
            meta["document_version"] = document_version
        if "version" not in meta:
            meta["version"] = document_version
    return Chunk(
        id=id,
        document_id=document_id,
        source_format="pdf",
        text=text,
        element_type="text",
        page_number=page_number,
        workspace_id=workspace_id,
        metadata=meta if meta else None,
    )


# =============================================================================
# Helper Doubles
# =============================================================================

class MockBedrockClient:
    """Mock Bedrock client with configurable count_tokens and converse behaviors."""

    def __init__(
        self,
        input_tokens: int = 150,
        output_tokens: int = 80,
        response_text: str = "",
        count_tokens_error: Exception | None = None,
        converse_error: Exception | None = None,
        has_count_tokens: bool = True,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.response_text = response_text
        self.count_tokens_error = count_tokens_error
        self.converse_error = converse_error
        self.has_count_tokens = has_count_tokens
        self.converse_calls = 0
        self.count_tokens_calls = 0

    def count_tokens(self, modelId: str, input: dict[str, Any]) -> dict[str, Any]:
        if not self.has_count_tokens:
            raise AttributeError("Client has no count_tokens method")
        self.count_tokens_calls += 1
        if self.count_tokens_error:
            raise self.count_tokens_error
        return {"inputTokens": self.input_tokens}

    def converse(
        self,
        modelId: str,
        messages: list[dict[str, Any]],
        system: list[dict[str, Any]] | None = None,
        inferenceConfig: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.converse_calls += 1
        if self.converse_error:
            raise self.converse_error
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": self.response_text}],
                }
            },
            "usage": {
                "inputTokens": self.input_tokens,
                "outputTokens": self.output_tokens,
            },
        }


# =============================================================================
# 1. Grounding Counterexamples
# =============================================================================

def test_grounding_counterexample_reversed_active_voice():
    """covers: AC-2 Rejection of reversed entity roles in active voice."""
    text = "In 2024, Beta Corp acquired Acme Inc for fifty million dollars."
    valid, reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Acme Inc",
        predicate="acquired",
        target_entity_name="Beta Corp",
    )
    assert valid is False
    assert reason == "reversed_relationship"


def test_grounding_counterexample_reversed_passive_voice():
    """covers: AC-2 Rejection of reversed entity roles in passive voice."""
    text = "During the restructuring, Acme Inc was acquired by Beta Corp."
    # Acme Inc was acquired by Beta Corp means Beta Corp acquired Acme Inc.
    # Claim that Acme Inc acquired Beta Corp must be rejected as reversed!
    valid, reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Acme Inc",
        predicate="acquired",
        target_entity_name="Beta Corp",
    )
    assert valid is False
    assert reason == "reversed_relationship"


def test_grounding_counterexample_negated_relationship():
    """covers: AC-2 Rejection of negated relationships asserting non-existence."""
    text1 = "Acme Inc did not acquire Beta Corp despite recent speculation."
    v1, r1 = validate_relationship_grounding(
        chunk_text=text1,
        source_entity_name="Acme Inc",
        predicate="acquired",
        target_entity_name="Beta Corp",
    )
    assert v1 is False
    assert r1 == "negated_relationship"

    text2 = "Neither Acme Inc nor Beta Corp entered into any partnership agreement."
    v2, r2 = validate_relationship_grounding(
        chunk_text=text2,
        source_entity_name="Acme Inc",
        predicate="partnered_with",
        target_entity_name="Beta Corp",
    )
    assert v2 is False
    assert r2 == "negated_relationship"


def test_grounding_counterexample_unrelated_clauses_disjoint_sentences():
    """covers: AC-2 Rejection when entities appear across disjoint sentences without relational connection."""
    text = (
        "Acme Inc acquired additional assets yesterday. "
        "Beta Corp opened a new semiconductor manufacturing facility in Texas."
    )
    valid, reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Acme Inc",
        predicate="acquired",
        target_entity_name="Beta Corp",
    )
    assert valid is False
    assert reason == "co_occurrence_across_disjoint_sentences"


def test_grounding_counterexample_cooccurrence_alone_without_predicate():
    """covers: AC-2 Rejection of mere co-occurrence without directional predicate."""
    text = "Acme Inc and Beta Corp executives attended the annual technology conference in San Jose."
    valid, reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Acme Inc",
        predicate="acquired",
        target_entity_name="Beta Corp",
    )
    assert valid is False
    assert "predicate_not_grounded" in (reason or "")


def test_extract_pdf_relationships_malformed_json_fails_closed():
    """covers: AC-1, AC-2 Model output returning non-JSON or malformed syntax fails closed."""
    chunk = make_chunk(id="chunk_1", document_id="doc_1", text="Acme acquired Beta")
    entities, rels = extract_pdf_relationships(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        model_output_json="This is not valid JSON at all! ```json {unclosed",
        publication_id="pub_1",
    )
    assert len(entities) == 0
    assert len(rels) == 0


def test_extract_pdf_relationships_unknown_chunk_id_rejected():
    """covers: AC-1, AC-2 Model output citing unauthorized chunk ID not in input is rejected."""
    chunk = make_chunk(id="chunk_authorized", document_id="doc_1", text="Acme acquired Beta")
    output = json.dumps({
        "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
        "relationships": [{
            "source_entity": "Acme",
            "predicate": "acquired",
            "target_entity": "Beta",
            "chunk_id": "chunk_unauthorized_external",
            "verbatim_evidence": "Acme acquired Beta",
        }],
    })
    entities, rels = extract_pdf_relationships(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        model_output_json=output,
        publication_id="pub_1",
    )
    assert len(rels) == 0


def test_extract_pdf_relationships_document_scoped_identity_default():
    """covers: AC-1 Entity identity is document-scoped by default unless disambiguation token is present."""
    chunk = make_chunk(id="chunk_1", document_id="doc_a", text="Michael Jordan founded 23XI Racing")
    output = json.dumps({
        "entities": [{"name": "Michael Jordan", "entity_type": "person", "disambiguation_token": None}],
        "relationships": [{
            "source_entity": "Michael Jordan",
            "predicate": "founded_by",
            "target_entity": "23XI Racing",
            "chunk_id": "chunk_1",
            "verbatim_evidence": "Michael Jordan founded 23XI Racing",
        }],
    })
    entities, _ = extract_pdf_relationships(
        workspace_id="ws_test",
        document_id="doc_a",
        document_version="v1",
        chunks=[chunk],
        model_output_json=output,
        publication_id="pub_1",
    )
    # Default identity includes DOC#doc_a
    mj_entity = next(e for e in entities if e.name == "Michael Jordan")
    expected_id = compute_entity_id(
        workspace_id="ws_test",
        document_id="doc_a",
        entity_type="person",
        name="Michael Jordan",
        disambiguation_token=None,
    )
    assert mj_entity.entity_id == expected_id
    assert mj_entity.source_document_id == "doc_a"


def test_extract_pdf_relationships_verified_disambiguation_token_allows_cross_document():
    """covers: AC-1 Verified disambiguation token establishes cross-document identity."""
    chunk = make_chunk(id="chunk_1", document_id="doc_a", text="Apple Inc acquired Beats")
    output = json.dumps({
        "entities": [{"name": "Apple Inc", "entity_type": "organization", "disambiguation_token": "NASDAQ:AAPL"}],
        "relationships": [{
            "source_entity": "Apple Inc",
            "predicate": "acquired",
            "target_entity": "Beats",
            "chunk_id": "chunk_1",
            "verbatim_evidence": "Apple Inc acquired Beats",
        }],
    })
    entities, _ = extract_pdf_relationships(
        workspace_id="ws_test",
        document_id="doc_a",
        document_version="v1",
        chunks=[chunk],
        model_output_json=output,
        publication_id="pub_1",
        authoritative_disambiguation={"apple inc": "NASDAQ:AAPL"},
    )
    apple = next(e for e in entities if e.name == "Apple Inc")
    expected_id = compute_entity_id(
        workspace_id="ws_test",
        document_id="doc_b",  # different doc, same disambiguation token
        entity_type="organization",
        name="Apple Inc",
        disambiguation_token="NASDAQ:AAPL",
    )
    assert apple.entity_id == expected_id
    assert apple.disambiguation_token == "NASDAQ:AAPL"


# =============================================================================
# 2. CountTokens Failures (AC-5)
# =============================================================================

def test_count_tokens_missing_capability_fails_closed():
    """covers: AC-5 Client missing count_tokens method fails closed with 0 converse calls."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)
    client = MagicMock(spec=["converse"])

    chunk = make_chunk(id="chunk_1", document_id="doc_1", text="Acme acquired Beta")
    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        client=client,
        worker_id="worker_test",
    )
    assert manifest.status == "failed"
    assert client.converse.call_count == 0


def test_count_tokens_zero_or_negative_fails_closed():
    """covers: AC-5 count_tokens returning <= 0 tokens fails closed with 0 converse calls."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)
    client = MockBedrockClient(input_tokens=0)

    chunk = make_chunk(id="chunk_1", document_id="doc_1", text="Acme acquired Beta")
    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        client=client,
        worker_id="worker_test",
    )
    assert manifest.status == "failed"
    assert client.converse_calls == 0


def test_count_tokens_exception_fails_closed():
    """covers: AC-5 count_tokens raising an exception fails closed with 0 converse calls."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)
    client = MockBedrockClient(count_tokens_error=RuntimeError("Token service unavailable"))

    chunk = make_chunk(id="chunk_1", document_id="doc_1", text="Acme acquired Beta")
    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        client=client,
        worker_id="worker_test",
    )
    assert manifest.status == "failed"
    assert client.converse_calls == 0


def test_count_tokens_exceeding_per_call_ceiling_fails_closed():
    """covers: AC-5 Request exceeding 2000 input tokens ceiling fails closed before network dispatch."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)
    client = MockBedrockClient(input_tokens=2050)

    chunk = make_chunk(id="chunk_1", document_id="doc_1", text="Acme acquired Beta")
    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        client=client,
        worker_id="worker_test",
    )
    assert manifest.status == "failed"
    assert client.converse_calls == 0


# =============================================================================
# 3. Concurrent Reservations and Lease Takeover (AC-4)
# =============================================================================

def test_concurrent_reservations_active_lease_held():
    """covers: AC-4 While an active lease is held, another worker cannot reserve."""
    repo = InMemoryKnowledgeGraphRepository()
    res1 = repo.reserve_extraction_budget(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        worker_id="worker_a",
        estimated_prompt_tokens=200,
        lease_duration_s=15.0,
    )
    assert res1.granted is True
    assert res1.fence_token == 1

    # Worker B tries to reserve while lease is active
    res2 = repo.reserve_extraction_budget(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        worker_id="worker_b",
        estimated_prompt_tokens=200,
        lease_duration_s=15.0,
    )
    assert res2.granted is False
    assert res2.reason == "active_lease_held"


def test_lease_takeover_after_expiration():
    """covers: AC-4 After 15-second lease expires, competitor worker takes over and advances fence."""
    repo = InMemoryKnowledgeGraphRepository()
    res1 = repo.reserve_extraction_budget(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        worker_id="worker_a",
        estimated_prompt_tokens=200,
        lease_duration_s=0.1,  # short lease for test
    )
    assert res1.granted is True
    assert res1.fence_token == 1

    time.sleep(0.15)  # wait for lease to expire

    # Worker B takes over
    res2 = repo.reserve_extraction_budget(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        worker_id="worker_b",
        estimated_prompt_tokens=200,
        lease_duration_s=15.0,
    )
    assert res2.granted is True
    assert res2.fence_token == 2
    assert res2.call_number == 2
    assert res2.ledger is not None
    assert res2.ledger.lease_owner_id == "worker_b"


# =============================================================================
# 4. Stale Worker Fencing (AC-4)
# =============================================================================

def test_stale_worker_fenced_out_tokens_reconciled():
    """covers: AC-4 Late-returning worker is fenced out; tokens are reconciled, but publication is rejected."""
    repo = InMemoryKnowledgeGraphRepository()
    # 1. Worker A reserves
    res_a = repo.reserve_extraction_budget(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        worker_id="worker_a",
        estimated_prompt_tokens=300,
        lease_duration_s=0.1,
    )
    assert res_a.fence_token == 1

    time.sleep(0.15)

    # 2. Worker B takes over advancing fence to 2
    res_b = repo.reserve_extraction_budget(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        worker_id="worker_b",
        estimated_prompt_tokens=300,
        lease_duration_s=15.0,
    )
    assert res_b.fence_token == 2

    # 3. Worker A completes late with fence 1
    rec_a = repo.reconcile_extraction_budget(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        worker_id="worker_a",
        fence_token=1,
        actual_prompt_tokens=300,
        actual_output_tokens=100,
    )
    assert rec_a.reconciled is False
    assert rec_a.fenced_out is True
    assert rec_a.reason == "fenced_out"
    # Verify Worker A's tokens were reconciled into ledger cumulative total
    assert rec_a.ledger is not None
    assert rec_a.ledger.spent_tokens == 400
    assert rec_a.ledger.spent_prompt_tokens == 300
    assert rec_a.ledger.spent_output_tokens == 100

    # 4. Worker A attempts to commit manifest with stale fence -> FencingConflictError
    manifest_a = KnowledgeGraphManifest(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        publication_id="pub_stale_a",
        status="ready",
        entity_count=1,
        relationship_count=1,
        ingested_at_utc="2026-11-16T00:00:00Z",
        content_hash="hash_a",
    )
    with pytest.raises(FencingConflictError):
        repo.commit_manifest(
            workspace_id="ws_test",
            manifest=manifest_a,
            fence_token=1,
            worker_id="worker_a",
        )


# =============================================================================
# 5. Uncertain Timeouts & Client Socket Timeouts (AC-4)
# =============================================================================

def test_uncertain_client_timeout_recovery_strongly_consistent():
    """covers: AC-4 Socket timeout drops connection; strongly consistent read recovers state."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)

    # Simulate timeout on converse call
    client = MockBedrockClient(
        input_tokens=200,
        converse_error=TimeoutError("Client socket read timeout"),
    )
    chunk = make_chunk(id="chunk_1", document_id="doc_1", text="Acme acquired Beta")

    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        client=client,
        worker_id="worker_test",
    )
    assert manifest.status == "failed"

    # Recovery read with strongly consistent read
    recovered_ledger = repo.get_budget_ledger("ws_test", "doc_1", "v1", consistent_read=True)
    assert recovered_ledger is not None
    assert recovered_ledger.reserved_calls == 1
    # Lease was released upon failure
    assert recovered_ledger.lease_owner_id is None
    assert recovered_ledger.spent_prompt_tokens == 200


# =============================================================================
# 6. Restart Recovery & Retry Ceilings (AC-3, AC-4)
# =============================================================================

def test_crash_recovery_preserves_spent_budget():
    """covers: AC-3, AC-4 Restart recovery preserves existing call reservations and spent tokens."""
    repo = InMemoryKnowledgeGraphRepository()
    # Call 1 executed before crash
    repo.reserve_extraction_budget(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        worker_id="worker_crash",
        estimated_prompt_tokens=400,
        lease_duration_s=0.1,
    )
    repo.reconcile_extraction_budget(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        worker_id="worker_crash",
        fence_token=1,
        actual_prompt_tokens=400,
        actual_output_tokens=150,
    )

    # Worker crashes; after 15-second lease expires, recovery worker restarts
    time.sleep(0.15)

    # Worker crashes and restarts. Check budget recovery:
    ledger = repo.get_budget_ledger("ws_test", "doc_1", "v1", consistent_read=True)
    assert ledger is not None
    assert ledger.reserved_calls == 1
    assert ledger.completed_calls == 1
    assert ledger.spent_tokens == 550

    # Next reservation is call 2
    res2 = repo.reserve_extraction_budget(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        worker_id="worker_restarted",
        estimated_prompt_tokens=400,
    )
    assert res2.granted is True
    assert res2.call_number == 2
    assert res2.remaining_calls == 0


def test_throttling_retry_counts_against_call_ceiling():
    """covers: AC-3 Throttling (429) retry counts as second reserved call; third call is denied."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)

    # Client always throttles
    client = MockBedrockClient(
        input_tokens=200,
        converse_error=Exception("ThrottlingException (HTTP 429) Rate exceeded"),
    )
    chunk = make_chunk(id="chunk_1", document_id="doc_1", text="Acme acquired Beta")

    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        client=client,
        worker_id="worker_throttle",
    )
    assert manifest.status == "failed"

    ledger = repo.get_budget_ledger("ws_test", "doc_1", "v1", consistent_read=True)
    assert ledger is not None
    # 1 initial call + 1 retry = 2 calls reserved (ceiling reached)
    assert ledger.reserved_calls == 2

    # Any subsequent attempt must fail closed as calls exhausted
    res3 = repo.reserve_extraction_budget(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        worker_id="worker_subsequent",
        estimated_prompt_tokens=200,
    )
    assert res3.granted is False
    assert res3.reason == "calls_exhausted"


def test_zero_retries_on_non_throttling_errors():
    """covers: AC-3 Non-throttling errors (400, model error) do NOT retry; exactly 1 call recorded."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)

    client = MockBedrockClient(
        input_tokens=200,
        converse_error=Exception("ValidationException: Invalid request parameters"),
    )
    chunk = make_chunk(id="chunk_1", document_id="doc_1", text="Acme acquired Beta")

    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        client=client,
        worker_id="worker_bad_request",
    )
    assert manifest.status == "failed"

    ledger = repo.get_budget_ledger("ws_test", "doc_1", "v1", consistent_read=True)
    assert ledger is not None
    # Exactly 1 call attempted, zero retries
    assert ledger.reserved_calls == 1


# =============================================================================
# 7. Reconciliation Failures (AC-4)
# =============================================================================

def test_reconciliation_failure_fails_closed():
    """covers: AC-4 When reconciliation fails, extraction fails closed and edges are not committed."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)

    chunk = make_chunk(id="chunk_1", document_id="doc_1", text="Acme Corp acquired Beta Technologies in 2024.")
    valid_json = json.dumps({
        "entities": [{"name": "Acme Corp", "entity_type": "organization"}, {"name": "Beta Technologies", "entity_type": "organization"}],
        "relationships": [{
            "source_entity": "Acme Corp",
            "predicate": "acquired",
            "target_entity": "Beta Technologies",
            "chunk_id": "chunk_1",
            "verbatim_evidence": "Acme Corp acquired Beta Technologies",
        }],
    })
    client = MockBedrockClient(input_tokens=200, output_tokens=80, response_text=valid_json)

    # Force reconcile_extraction_budget to fail
    with patch.object(repo, "reconcile_extraction_budget", side_effect=RuntimeError("Storage connection lost")):
        manifest = builder.build_and_persist_pdf_graph(
            workspace_id="ws_test",
            document_id="doc_1",
            document_version="v1",
            chunks=[chunk],
            client=client,
            worker_id="worker_test",
        )
        assert manifest.status == "failed"
        # No ready manifest committed
        active_man = repo.get_manifest("ws_test", "doc_1", "v1")
        assert active_man is None or active_man.status != "ready"


# =============================================================================
# 8. Ready-Publication Preservation (AC-4)
# =============================================================================

def test_preserve_ready_publication_on_new_attempt_failure():
    """covers: AC-4 A failed new extraction attempt must NEVER clobber an existing ready manifest."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)

    # 1. Establish an initial ready manifest
    chunk = make_chunk(id="chunk_1", document_id="doc_1", text="Acme Corp acquired Beta Technologies in 2024.")
    valid_json = json.dumps({
        "entities": [{"name": "Acme Corp", "entity_type": "organization"}, {"name": "Beta Technologies", "entity_type": "organization"}],
        "relationships": [{
            "source_entity": "Acme Corp",
            "predicate": "acquired",
            "target_entity": "Beta Technologies",
            "chunk_id": "chunk_1",
            "verbatim_evidence": "Acme Corp acquired Beta Technologies",
        }],
    })
    client_good = MockBedrockClient(input_tokens=200, output_tokens=80, response_text=valid_json)

    initial_manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        client=client_good,
        worker_id="worker_initial",
    )
    assert initial_manifest.status == "ready"
    assert initial_manifest.relationship_count == 1
    ready_pub_id = initial_manifest.publication_id

    # 2. Simulate re-attempt that fails due to model error or budget exhaustion
    client_fail = MockBedrockClient(
        input_tokens=200,
        converse_error=Exception("InternalServerError: Bedrock service error"),
    )
    second_manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        client=client_fail,
        worker_id="worker_retry",
        base_publication_id=ready_pub_id,
    )
    # Manifest returned is the preserved ready manifest
    assert second_manifest.status == "ready"
    assert second_manifest.publication_id == ready_pub_id

    # Read from storage: ready manifest remains intact
    persisted = repo.get_manifest("ws_test", "doc_1", "v1", consistent_read=True)
    assert persisted is not None
    assert persisted.status == "ready"
    assert persisted.publication_id == ready_pub_id


# =============================================================================
# 9. Atomic Storage Behavior Across Independent SQLite Connections
# =============================================================================

def test_sqlite_budget_ledger_atomic_across_separate_connections():
    """covers: AC-4 SQLite BEGIN IMMEDIATE transactions enforce atomic fencing across separate database connections."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        repo1 = SQLiteKnowledgeGraphRepository(db_path=db_path)
        repo2 = SQLiteKnowledgeGraphRepository(db_path=db_path)

        # Worker 1 reserves on connection 1
        res1 = repo1.reserve_extraction_budget(
            workspace_id="ws_sqlite",
            document_id="doc_sql",
            document_version="v1",
            worker_id="worker_conn1",
            estimated_prompt_tokens=300,
            lease_duration_s=0.1,
        )
        assert res1.granted is True
        assert res1.fence_token == 1

        # Worker 2 on connection 2 tries while lease is active
        res2_active = repo2.reserve_extraction_budget(
            workspace_id="ws_sqlite",
            document_id="doc_sql",
            document_version="v1",
            worker_id="worker_conn2",
            estimated_prompt_tokens=300,
            lease_duration_s=15.0,
        )
        assert res2_active.granted is False
        assert res2_active.reason == "active_lease_held"

        time.sleep(0.15)  # wait for lease to expire

        # Worker 2 takes over on connection 2
        res2 = repo2.reserve_extraction_budget(
            workspace_id="ws_sqlite",
            document_id="doc_sql",
            document_version="v1",
            worker_id="worker_conn2",
            estimated_prompt_tokens=300,
            lease_duration_s=15.0,
        )
        assert res2.granted is True
        assert res2.fence_token == 2

        # Worker 1 tries to reconcile on connection 1 with stale fence
        rec1 = repo1.reconcile_extraction_budget(
            workspace_id="ws_sqlite",
            document_id="doc_sql",
            document_version="v1",
            worker_id="worker_conn1",
            fence_token=1,
            actual_prompt_tokens=300,
            actual_output_tokens=100,
        )
        assert rec1.reconciled is False
        assert rec1.fenced_out is True
        assert rec1.reason == "fenced_out"
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


# =============================================================================
# 10. Atomic Storage Behavior on Live DynamoDB Local (AC-4)
# =============================================================================

def test_dynamodb_local_budget_ledger_atomic_reservation_and_fencing():
    """covers: AC-4 Verify atomic budget reservation, lease enforcement, and fencing on live DynamoDB Local."""
    try:
        import boto3
        client = boto3.client(
            "dynamodb",
            endpoint_url="http://localhost:8000",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        client.list_tables()
    except Exception:
        pytest.skip("DynamoDB Local daemon not reachable on port 8000")

    table_name = "kre_kg_test_slice2_budget"
    # Create test table
    try:
        client.delete_table(TableName=table_name)
    except Exception:
        pass

    try:
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
    except client.exceptions.ResourceInUseException:
        pass

    try:
        repo = DynamoDBKnowledgeGraphRepository(
            table_name=table_name,
            endpoint_url="http://localhost:8000",
        )

        ws = "ws_ddb_slice2"
        doc = "doc_slice2"
        ver = "v1"

        # 1. Initial reservation
        res1 = repo.reserve_extraction_budget(
            workspace_id=ws,
            document_id=doc,
            document_version=ver,
            worker_id="worker_ddb_1",
            estimated_prompt_tokens=400,
            lease_duration_s=1.0,
        )
        assert res1.granted is True
        assert res1.fence_token == 1
        assert res1.call_number == 1

        # 2. Concurrent reservation while active lease held
        res2_denied = repo.reserve_extraction_budget(
            workspace_id=ws,
            document_id=doc,
            document_version=ver,
            worker_id="worker_ddb_2",
            estimated_prompt_tokens=400,
            lease_duration_s=15.0,
        )
        assert res2_denied.granted is False
        assert res2_denied.reason == "active_lease_held"

        time.sleep(1.05)  # lease expires

        # 3. Worker 2 takes over
        res2 = repo.reserve_extraction_budget(
            workspace_id=ws,
            document_id=doc,
            document_version=ver,
            worker_id="worker_ddb_2",
            estimated_prompt_tokens=400,
            lease_duration_s=15.0,
        )
        assert res2.granted is True
        assert res2.fence_token == 2
        assert res2.call_number == 2

        # 4. Worker 1 attempts reconciliation with stale fence 1 -> fenced out
        rec1 = repo.reconcile_extraction_budget(
            workspace_id=ws,
            document_id=doc,
            document_version=ver,
            worker_id="worker_ddb_1",
            fence_token=1,
            actual_prompt_tokens=400,
            actual_output_tokens=120,
        )
        assert rec1.reconciled is False
        assert rec1.fenced_out is True
        assert rec1.reason == "fenced_out"

        # Tokens from stale worker are reconciled into ledger
        ledger = repo.get_budget_ledger(ws, doc, ver, consistent_read=True)
        assert ledger is not None
        assert ledger.spent_tokens == 520

    finally:
        try:
            client.delete_table(TableName=table_name)
        except Exception:
            pass


# =============================================================================
# 11. TableStore Row Evidence Hydration Loop Deadline Check (Nit 2)
# =============================================================================

def test_tablestore_hydration_loop_deadline_exceeded():
    """covers: Nit 2 TableStore evidence hydration loop halts on remaining deadline overrun."""
    repo = InMemoryKnowledgeGraphRepository()
    ws = "ws_test"
    doc_id = "doc_table"
    ver = "v1"
    pub_id = generate_publication_id()

    src_ent = KnowledgeGraphEntity(
        entity_id="ent_beta",
        workspace_id=ws,
        name="Beta Corp",
        canonical_name="Beta Corp",
        entity_type="organization",
        source_document_id=doc_id,
        aliases=("Beta Corp",),
    )
    tgt_ent = KnowledgeGraphEntity(
        entity_id="ent_target",
        workspace_id=ws,
        name="Gamma LLC",
        canonical_name="Gamma LLC",
        entity_type="organization",
        source_document_id=doc_id,
    )
    rel = KnowledgeGraphRelationship(
        relationship_id="rel_table_edge",
        workspace_id=ws,
        source_entity_id=src_ent.entity_id,
        source_entity_name=src_ent.canonical_name,
        predicate="partnered",
        canonical_predicate="PARTNERED",
        target_entity_id=tgt_ent.entity_id,
        target_entity_name=tgt_ent.canonical_name,
        directed=True,
        confidence=1.0,
        source_document_id=doc_id,
        source_document_version=ver,
        publication_id=pub_id,
        source_chunk_ids=(),
        location=TableLocation(table_id="tbl_1", row_id="0", column_id="col1"),
        verbatim_evidence="Beta Corp partnered Gamma LLC",
        relationship_state="unique",
        created_at_utc="2026-11-16T00:00:00Z",
    )
    repo.stage_relationships(ws, doc_id, ver, pub_id, [src_ent, tgt_ent], [rel])
    manifest = KnowledgeGraphManifest(
        workspace_id=ws,
        document_id=doc_id,
        document_version=ver,
        publication_id=pub_id,
        status="ready",
        entity_count=2,
        relationship_count=1,
        ingested_at_utc="2026-11-16T00:00:00Z",
        content_hash="hash_table",
    )
    repo.commit_manifest(ws, manifest)

    mock_table_store = MagicMock()
    mock_table_store.get_row.return_value = {"col1": "val1"}

    boundary_mock = MagicMock()
    boundary_mock.has_tombstone.return_value = False

    retriever = KnowledgeGraphRetriever(
        repository=repo,
        boundary_repo=boundary_mock,
        table_store=mock_table_store,
        documents_repo=MagicMock(),
    )

    context = PinnedSnapshotContext(
        workspace_id=ws,
        captured_at_utc="2026-11-16T00:00:00Z",
        active_documents=(
            PinnedDocumentVersion(
                document_id=doc_id,
                document_version=ver,
                publication_id=pub_id,
            ),
        ),
    )

    t0 = time.perf_counter()
    result = retriever._execute_expansion(
        workspace_id=ws,
        query="Beta Corp",
        snapshot_context=context,
        max_hops=1,
        t_start=t0 - 1.0,  # 1.0s elapsed
        rem_s=0.0001,      # 0.1ms deadline -> already exceeded
    )
    assert result.was_truncated is True
    assert result.truncation_reason == "deadline_exceeded_during_table_hydration"
    mock_table_store.get_row.assert_not_called()


# =============================================================================
# 12. Clean SQLite Configuration Separation (Nit 1)
# =============================================================================

def test_sqlite_config_separation():
    """covers: Nit 1 SQLite repository uses GRAPH_SQLITE_PATH directly without falling back to WORKSPACE_SQLITE_PATH."""
    with patch.object(settings, "GRAPH_SQLITE_PATH", "/tmp/dedicated_graph.db"):
        with patch.object(settings, "WORKSPACE_SQLITE_PATH", "/tmp/unrelated_workspace.db"):
            repo = SQLiteKnowledgeGraphRepository()
            assert str(repo.db_path) == "/tmp/dedicated_graph.db"


# =============================================================================
# 13. Area 1 & 2: DynamoDB Lease Cleanup Races & Atomic Idempotent Reconciliation
# =============================================================================

def _get_dynamodb_local_repo(table_name: str = "kre_kg_test_slice2_budget"):
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
        pytest.skip("DynamoDB Local daemon not reachable on port 8000")

    try:
        client.delete_table(TableName=table_name)
    except Exception:
        pass

    try:
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
    except client.exceptions.ResourceInUseException:
        pass

    return DynamoDBKnowledgeGraphRepository(
        table_name=table_name,
        endpoint_url="http://localhost:8000",
    )


def test_dynamodb_local_publication_cleanup_never_clears_another_workers_lease():
    """covers: Area 1 Publication cleanup must never clear another worker's lease when fence token or owner has changed."""
    dynamodb_local_repo = _get_dynamodb_local_repo("kre_kg_test_slice2_lease_race")
    ws = "ws_lease_race"
    doc_id = "doc_race_1"
    ver = "v1"

    # Worker A acquires initial lease
    res_a = dynamodb_local_repo.reserve_extraction_budget(
        workspace_id=ws,
        document_id=doc_id,
        document_version=ver,
        worker_id="worker_a",
        estimated_prompt_tokens=500,
        lease_duration_s=1.0,
    )
    assert res_a.granted is True
    assert res_a.fence_token == 1

    # Lease expires on Worker A
    time.sleep(1.05)

    # Worker B takes over with new fence token
    res_b = dynamodb_local_repo.reserve_extraction_budget(
        workspace_id=ws,
        document_id=doc_id,
        document_version=ver,
        worker_id="worker_b",
        estimated_prompt_tokens=500,
        lease_duration_s=15.0,
    )
    assert res_b.granted is True
    assert res_b.fence_token == 2

    # Worker A now attempts to commit manifest using its stale fence token 1
    man_a = KnowledgeGraphManifest(
        workspace_id=ws,
        document_id=doc_id,
        document_version=ver,
        publication_id="pub_stale_a",
        status="ready",
        entity_count=1,
        relationship_count=1,
        ingested_at_utc="2026-11-17T00:00:00Z",
        content_hash="h1",
    )
    with pytest.raises(FencingConflictError):
        dynamodb_local_repo.commit_manifest(
            workspace_id=ws,
            manifest=man_a,
            fence_token=res_a.fence_token,
            worker_id="worker_a",
        )

    # Confirm Worker B's lease, owner, and authority remain completely intact
    ledger = dynamodb_local_repo.get_budget_ledger(ws, doc_id, ver, consistent_read=True)
    assert ledger.lease_owner_id == "worker_b"
    assert ledger.fence_token == 2
    assert ledger.lease_expires_at > time.time()


def test_dynamodb_local_failure_cleanup_never_clears_another_workers_lease():
    """covers: Area 1 Failure cleanup must never clear another worker's lease when fenced out."""
    dynamodb_local_repo = _get_dynamodb_local_repo("kre_kg_test_slice2_fail_race")
    ws = "ws_fail_race"
    doc_id = "doc_fail_1"
    ver = "v1"

    res_a = dynamodb_local_repo.reserve_extraction_budget(
        workspace_id=ws,
        document_id=doc_id,
        document_version=ver,
        worker_id="worker_a",
        estimated_prompt_tokens=500,
        lease_duration_s=1.0,
    )
    assert res_a.granted is True

    # Lease expires
    time.sleep(1.05)

    # Worker B takes over
    res_b = dynamodb_local_repo.reserve_extraction_budget(
        workspace_id=ws,
        document_id=doc_id,
        document_version=ver,
        worker_id="worker_b",
        estimated_prompt_tokens=500,
        lease_duration_s=15.0,
    )
    assert res_b.granted is True
    assert res_b.fence_token == 2

    # Worker A fails late and calls fail_extraction_budget
    dynamodb_local_repo.fail_extraction_budget(
        workspace_id=ws,
        document_id=doc_id,
        document_version=ver,
        worker_id="worker_a",
        fence_token=res_a.fence_token,
        error_reason="late_failure",
        prompt_tokens_spent=300,
        output_tokens_spent=50,
    )

    # Worker B's lease authority must remain completely intact!
    ledger = dynamodb_local_repo.get_budget_ledger(ws, doc_id, ver, consistent_read=True)
    assert ledger.lease_owner_id == "worker_b"
    assert ledger.fence_token == 2
    assert ledger.lease_expires_at > time.time()
    # But Worker A's spent tokens and fence marker must be recorded!
    assert ledger.spent_tokens == 350
    assert 1 in ledger.reconciled_fences


def test_dynamodb_local_atomic_idempotent_reconciliation():
    """covers: Area 2 Duplicate reconciliation is idempotent and concurrent markers are preserved."""
    dynamodb_local_repo = _get_dynamodb_local_repo("kre_kg_test_slice2_rec_idem")
    ws = "ws_rec_idem"
    doc_id = "doc_rec_1"
    ver = "v1"

    res = dynamodb_local_repo.reserve_extraction_budget(
        workspace_id=ws,
        document_id=doc_id,
        document_version=ver,
        worker_id="worker_1",
        estimated_prompt_tokens=500,
        lease_duration_s=15.0,
    )
    assert res.granted is True

    # First reconciliation
    r1 = dynamodb_local_repo.reconcile_extraction_budget(
        workspace_id=ws,
        document_id=doc_id,
        document_version=ver,
        worker_id="worker_1",
        fence_token=res.fence_token,
        actual_prompt_tokens=400,
        actual_output_tokens=100,
    )
    assert r1.reconciled is True
    assert r1.reason == "reconciled"

    # Duplicate reconciliation of the same attempt must be idempotent
    r2 = dynamodb_local_repo.reconcile_extraction_budget(
        workspace_id=ws,
        document_id=doc_id,
        document_version=ver,
        worker_id="worker_1",
        fence_token=res.fence_token,
        actual_prompt_tokens=400,
        actual_output_tokens=100,
    )
    assert r2.reconciled is True
    assert r2.reason == "already_reconciled"

    # Verify cumulative spend was not double-counted
    ledger = dynamodb_local_repo.get_budget_ledger(ws, doc_id, ver, consistent_read=True)
    assert ledger.spent_tokens == 500
    assert ledger.reconciled_fences == (1,)


# =============================================================================
# 14. Area 3: Verify PDF Evidence, Provenance, and Identity
# =============================================================================

def test_extract_pdf_relationships_rejects_empty_or_fabricated_evidence():
    """covers: Area 3 Require non-empty verbatim evidence present in cited source chunk."""
    chunk = make_chunk(id="chunk_1", document_id="doc_1", workspace_id="ws_1", text="Acme Corp acquired Beta Tech in 2024.")

    # 1. Empty verbatim evidence
    out_empty = json.dumps({
        "entities": [{"name": "Acme Corp", "entity_type": "organization"}, {"name": "Beta Tech", "entity_type": "organization"}],
        "relationships": [{
            "source_entity": "Acme Corp",
            "predicate": "acquired",
            "target_entity": "Beta Tech",
            "chunk_id": "chunk_1",
            "verbatim_evidence": "",
        }],
    })
    _, rels_empty = extract_pdf_relationships(
        workspace_id="ws_1",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        model_output_json=out_empty,
        publication_id="pub_1",
    )
    assert len(rels_empty) == 0

    # 2. Fabricated verbatim evidence not in chunk text
    out_fab = json.dumps({
        "entities": [{"name": "Acme Corp", "entity_type": "organization"}, {"name": "Beta Tech", "entity_type": "organization"}],
        "relationships": [{
            "source_entity": "Acme Corp",
            "predicate": "acquired",
            "target_entity": "Beta Tech",
            "chunk_id": "chunk_1",
            "verbatim_evidence": "Acme Corp completely took over Beta Tech",
        }],
    })
    _, rels_fab = extract_pdf_relationships(
        workspace_id="ws_1",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        model_output_json=out_fab,
        publication_id="pub_1",
    )
    assert len(rels_fab) == 0


def test_extract_pdf_relationships_rejects_wrong_source_bindings():
    """covers: Area 3 Reject chunks whose document/version/workspace bindings conflict with request."""
    # Chunk with conflicting document_id
    bad_doc_chunk = make_chunk(id="chunk_1", document_id="doc_WRONG", text="Acme acquired Beta")
    out = json.dumps({
        "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
        "relationships": [{
            "source_entity": "Acme",
            "predicate": "acquired",
            "target_entity": "Beta",
            "chunk_id": "chunk_1",
            "verbatim_evidence": "Acme acquired Beta",
        }],
    })
    _, rels = extract_pdf_relationships(
        workspace_id="ws_1",
        document_id="doc_1",
        document_version="v1",
        chunks=[bad_doc_chunk],
        model_output_json=out,
        publication_id="pub_1",
    )
    assert len(rels) == 0


def test_extract_pdf_relationships_preserves_actual_page_number():
    """covers: Area 3 Preserves actual page_number metadata from chunk."""
    chunk = make_chunk(
        id="chunk_page_test",
        document_id="doc_1",
        workspace_id="ws_1",
        page_number=42,
        text="Acme acquired Beta in 2024.",
    )
    out = json.dumps({
        "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
        "relationships": [{
            "source_entity": "Acme",
            "predicate": "acquired",
            "target_entity": "Beta",
            "chunk_id": "chunk_page_test",
            "verbatim_evidence": "Acme acquired Beta",
        }],
    })
    _, rels = extract_pdf_relationships(
        workspace_id="ws_1",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        model_output_json=out,
        publication_id="pub_1",
    )
    assert len(rels) == 1
    assert rels[0].location.page == 42


def test_extract_pdf_relationships_rejects_unverified_model_disambiguation_token():
    """covers: Area 3 Model-supplied disambiguation tokens are never trusted without authoritative verification."""
    chunk = make_chunk(id="chunk_1", document_id="doc_1", workspace_id="ws_1", text="Acme Corp acquired Beta Tech in 2024.")
    out = json.dumps({
        "entities": [
            {"name": "Acme Corp", "entity_type": "organization", "disambiguation_token": "NASDAQ:ACME_FABRICATED"},
            {"name": "Beta Tech", "entity_type": "organization", "disambiguation_token": None},
        ],
        "relationships": [{
            "source_entity": "Acme Corp",
            "predicate": "acquired",
            "target_entity": "Beta Tech",
            "chunk_id": "chunk_1",
            "verbatim_evidence": "Acme Corp acquired Beta Tech",
        }],
    })

    # Without authoritative verification, model token is stripped
    entities, _ = extract_pdf_relationships(
        workspace_id="ws_1",
        document_id="doc_1",
        document_version="v1",
        chunks=[chunk],
        model_output_json=out,
        publication_id="pub_1",
        authoritative_disambiguation=None,
    )
    acme = next(e for e in entities if e.name == "Acme Corp")
    assert acme.disambiguation_token is None
    expected_doc_scoped_id = compute_entity_id(
        workspace_id="ws_1",
        document_id="doc_1",
        entity_type="organization",
        name="Acme Corp",
        disambiguation_token=None,
    )
    assert acme.entity_id == expected_doc_scoped_id


# =============================================================================
# 15. Area 4: Reject Unsupported Relationships Within One Sentence
# =============================================================================

def test_grounding_rejects_unrelated_clauses_in_one_sentence():
    """covers: Area 4 Reject 'Acme acquired Beta' when text says 'Acme acquired Gamma, while Beta opened an office'."""
    text1 = "Acme Corp acquired Gamma LLC, while Beta Tech opened a branch office in London."
    valid1, reason1 = validate_relationship_grounding(
        chunk_text=text1,
        source_entity_name="Acme Corp",
        predicate="acquired",
        target_entity_name="Beta Tech",
    )
    assert valid1 is False
    assert reason1 == "unrelated_clauses"

    text2 = "Acme Corp acquired Gamma LLC, and Beta Tech opened a branch office."
    valid2, reason2 = validate_relationship_grounding(
        chunk_text=text2,
        source_entity_name="Acme Corp",
        predicate="acquired",
        target_entity_name="Beta Tech",
    )
    assert valid2 is False
    assert reason2 == "unrelated_clauses"


def test_grounding_rejects_hypothetical_and_reported_claims():
    """covers: Area 4 Reject hypothetical statements and unverified reported claims."""
    text_hyp = "Acme Corp might acquire Beta Tech if regulatory approval is granted."
    v_hyp, r_hyp = validate_relationship_grounding(
        chunk_text=text_hyp,
        source_entity_name="Acme Corp",
        predicate="acquired",
        target_entity_name="Beta Tech",
    )
    assert v_hyp is False
    assert r_hyp == "hypothetical_or_unsupported_claim"

    text_rep = "Market analysts reported that Acme Corp acquired Beta Tech yesterday."
    v_rep, r_rep = validate_relationship_grounding(
        chunk_text=text_rep,
        source_entity_name="Acme Corp",
        predicate="acquired",
        target_entity_name="Beta Tech",
    )
    assert v_rep is False
    assert r_rep == "hypothetical_or_unsupported_claim"


# =============================================================================
# 16. Area 5: Fail Closed on Missing or Invalid Provider Usage
# =============================================================================

def test_build_pdf_graph_fails_closed_on_invalid_usage():
    """covers: Area 5 Require authoritative inputTokens and outputTokens; reject missing, boolean, or malformed usage."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)
    chunk = make_chunk(id="chunk_1", workspace_id="ws_u", document_id="doc_u", text="Acme acquired Beta")

    # 1. Missing usage
    client_missing = MagicMock()
    client_missing.count_tokens.return_value = {"inputTokens": 100}
    client_missing.converse.return_value = {
        "output": {"message": {"content": [{"text": json.dumps({
            "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
            "relationships": [{"source_entity": "Acme", "predicate": "acquired", "target_entity": "Beta", "chunk_id": "chunk_1", "verbatim_evidence": "Acme acquired Beta"}],
        })}]}},
        # usage omitted!
    }
    m1 = builder.build_and_persist_pdf_graph(
        workspace_id="ws_u",
        document_id="doc_u",
        document_version="v1",
        chunks=[chunk],
        client=client_missing,
    )
    assert m1.status == "failed"
    assert m1.relationship_count == 0

    # 2. Boolean usage
    client_bool = MagicMock()
    client_bool.count_tokens.return_value = {"inputTokens": 100}
    client_bool.converse.return_value = {
        "output": {"message": {"content": [{"text": "{}"}]}},
        "usage": {"inputTokens": True, "outputTokens": False},
    }
    m2 = builder.build_and_persist_pdf_graph(
        workspace_id="ws_u",
        document_id="doc_u2",
        document_version="v1",
        chunks=[make_chunk(id="chunk_1", workspace_id="ws_u", document_id="doc_u2", text="Acme acquired Beta")],
        client=client_bool,
    )
    assert m2.status == "failed"

    # 3. Negative usage
    client_neg = MagicMock()
    client_neg.count_tokens.return_value = {"inputTokens": 100}
    client_neg.converse.return_value = {
        "output": {"message": {"content": [{"text": "{}"}]}},
        "usage": {"inputTokens": -10, "outputTokens": 50},
    }
    m3 = builder.build_and_persist_pdf_graph(
        workspace_id="ws_u",
        document_id="doc_u3",
        document_version="v1",
        chunks=[make_chunk(id="chunk_1", workspace_id="ws_u", document_id="doc_u3", text="Acme acquired Beta")],
        client=client_neg,
    )
    assert m3.status == "failed"


# =============================================================================
# 17. Area 6: Provider Deadline, Retries Disabled, and Capacity Retention
# =============================================================================

def test_provider_call_deadline_and_capacity_retention():
    """covers: Area 6 Call deadline enforcement, caller timeout fail-closed, and capacity recovery."""
    reset_kg_provider_controller()
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)
    chunk = make_chunk(id="chunk_1", workspace_id="ws_dl", document_id="doc_dl", text="Acme acquired Beta")

    def slow_converse(**kwargs):
        time.sleep(0.3)
        return {
            "output": {"message": {"content": [{"text": "{}"}]}},
            "usage": {"inputTokens": 100, "outputTokens": 20},
        }

    client = MagicMock()
    client.count_tokens.return_value = {"inputTokens": 100}
    client.converse.side_effect = slow_converse

    t0 = time.perf_counter()
    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_dl",
        document_id="doc_dl",
        document_version="v1",
        chunks=[chunk],
        client=client,
        call_deadline_s=0.08,  # Caller timeout at 80ms
    )
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.25  # Caller returned promptly on deadline!
    assert manifest.status == "failed"

    # Check budget ledger recorded failure
    ledger = repo.get_budget_ledger("ws_dl", "doc_dl", "v1")
    assert ledger.spent_tokens == 100

    # Wait for the background worker to finish and recover capacity
    controller = get_kg_provider_controller()
    time.sleep(0.35)
    assert controller.active_count == 0


def test_disable_client_retries_unregisters_boto3_handlers():
    """covers: Area 6 Verify disable_client_retries unregisters handlers from client."""
    mock_events = MagicMock()
    mock_events._emitter._unique_id_handlers = {"retry-config-bedrock-runtime": MagicMock(), "other-handler": MagicMock()}
    mock_client = MagicMock()
    mock_client.meta.events = mock_events
    mock_client.meta.service_model.service_name = "bedrock-runtime"

    disable_client_retries(mock_client)
    mock_events.unregister.assert_called()


# =============================================================================
# 18. Regressions: Four Slice 2 Correctness Gaps
# =============================================================================


def test_chunk_provenance_rejects_stale_metadata_version():
    """covers: Gap 1 Reject stale metadata versions not matching the requested document version."""
    c1 = make_chunk(
        id="chunk_stale_1",
        workspace_id="ws_prov",
        document_id="doc_prov",
        document_version="v0",
        metadata={"version": "v0", "document_version": "v0"},
    )
    ok, reason = validate_chunk_provenance(c1, "ws_prov", "doc_prov", "v1")
    assert ok is False
    assert "stale_document_version" in str(reason)


def test_chunk_provenance_rejects_conflicting_version_or_identity_fields():
    """covers: Gap 1 Reject conflicting version, workspace, and document fields across metadata and attributes."""
    # Conflicting metadata version vs document_version
    c_meta_ver = make_chunk(
        id="c1",
        workspace_id="ws_prov",
        document_id="doc_prov",
        document_version=None,
        metadata={"version": "v1", "document_version": "v2"},
    )
    ok, reason = validate_chunk_provenance(c_meta_ver, "ws_prov", "doc_prov", "v1")
    assert ok is False
    assert "conflicting_metadata_versions" in str(reason)

    # Conflicting attribute version vs metadata version
    class _CustomChunk:
        id = "c2"
        workspace_id = "ws_prov"
        document_id = "doc_prov"
        document_version = "v1"
        metadata = {"version": "v2", "document_version": "v2"}

    c_attr_ver = _CustomChunk()
    ok2, reason2 = validate_chunk_provenance(c_attr_ver, "ws_prov", "doc_prov", "v1")
    assert ok2 is False
    assert "conflicting_version_bindings" in str(reason2)

    # Conflicting document_id attribute vs metadata
    c_doc_conflict = make_chunk(
        id="c3",
        workspace_id="ws_prov",
        document_id="doc_a",
        document_version="v1",
        metadata={"document_id": "doc_b", "version": "v1", "document_version": "v1"},
    )
    ok3, reason3 = validate_chunk_provenance(c_doc_conflict, "ws_prov", "doc_a", "v1")
    assert ok3 is False
    assert "conflicting_document_id" in str(reason3)

    # Conflicting workspace_id attribute vs metadata
    c_ws_conflict = make_chunk(
        id="c4",
        workspace_id="ws_a",
        document_id="doc_prov",
        document_version="v1",
        metadata={"workspace_id": "ws_b", "version": "v1", "document_version": "v1"},
    )
    ok4, reason4 = validate_chunk_provenance(c_ws_conflict, "ws_a", "doc_prov", "v1")
    assert ok4 is False
    assert "conflicting_workspace_id" in str(reason4)


def test_chunk_provenance_rejects_missing_bindings_or_unversioned_chunks():
    """covers: Gap 1 Reject chunks lacking chunk_id, document_id, workspace_id, or version."""
    # Missing chunk ID
    c_no_id = make_chunk(id="", workspace_id="ws_prov", document_id="doc_prov", document_version="v1", metadata={"version": "v1"})
    ok1, reason1 = validate_chunk_provenance(c_no_id, "ws_prov", "doc_prov", "v1")
    assert ok1 is False
    assert "missing_chunk_id" in str(reason1)

    # Missing document ID
    c_no_doc = make_chunk(id="c1", workspace_id="ws_prov", document_id="", document_version="v1", metadata={"version": "v1"})
    ok2, reason2 = validate_chunk_provenance(c_no_doc, "ws_prov", "doc_prov", "v1")
    assert ok2 is False
    assert "missing_document_id" in str(reason2)

    # Missing workspace ID
    c_no_ws = make_chunk(id="c2", workspace_id="", document_id="doc_prov", document_version="v1", metadata={"version": "v1"})
    ok3, reason3 = validate_chunk_provenance(c_no_ws, "ws_prov", "doc_prov", "v1")
    assert ok3 is False
    assert "missing_workspace_id" in str(reason3)

    # Unversioned chunk (no version in attribute or metadata)
    c_unversioned = make_chunk(id="c3", workspace_id="ws_prov", document_id="doc_prov", document_version=None, metadata={})
    ok4, reason4 = validate_chunk_provenance(c_unversioned, "ws_prov", "doc_prov", "v1")
    assert ok4 is False
    assert "unverifiable_version_binding" in str(reason4)

    # Mismatched workspace
    c_match = make_chunk(id="c4", workspace_id="ws_prov", document_id="doc_prov", document_version="v1", metadata={"version": "v1"})
    ok5, reason5 = validate_chunk_provenance(c_match, "ws_other", "doc_prov", "v1")
    assert ok5 is False
    assert "mismatched_workspace_id" in str(reason5)

    # Mismatched document
    ok6, reason6 = validate_chunk_provenance(c_match, "ws_prov", "doc_other", "v1")
    assert ok6 is False
    assert "mismatched_document_id" in str(reason6)


def test_chunk_provenance_accepts_valid_exact_version():
    """covers: Gap 1 Accept valid chunks with exact workspace, document, and version bindings."""
    c = make_chunk(
        id="chunk_valid_1",
        workspace_id="ws_exact",
        document_id="doc_exact",
        document_version="v1",
        metadata={"version": "v1", "document_version": "v1"},
    )
    ok, reason = validate_chunk_provenance(c, "ws_exact", "doc_exact", "v1")
    assert ok is True
    assert reason is None


def test_chunk_provenance_with_real_chunk_from_parse_file(tmp_path):
    """covers: Gap 1 Test real Chunk objects produced through parse_service parse_file."""
    csv_file = tmp_path / "test_data.csv"
    csv_file.write_text("id,name,role\n1,Acme,Corp\n2,Beta,Subsidiary\n", encoding="utf-8")

    doc = parse_file(csv_file, workspace_id="ws_real")
    assert len(doc.chunks) > 0
    real_chunk = doc.chunks[0]

    chunk_ver = real_chunk.metadata.get("version")
    assert chunk_ver is not None

    # Valid exact version passes
    ok, reason = validate_chunk_provenance(real_chunk, "ws_real", doc.id, chunk_ver)
    assert ok is True
    assert reason is None

    # Stale version fails
    ok_stale, reason_stale = validate_chunk_provenance(real_chunk, "ws_real", doc.id, "v_stale")
    assert ok_stale is False
    assert "stale_document_version" in str(reason_stale)


def test_grounding_rejects_evidence_stripping_conditional_context():
    """covers: Gap 2 Reject evidence that strips conditional surrounding context."""
    chunk = make_chunk(
        id="c_cond",
        workspace_id="ws_cond",
        document_id="doc_cond",
        text="If Acme acquired Beta, costs would rise.",
    )
    payload = json.dumps({
        "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
        "relationships": [{
            "source_entity": "Acme",
            "predicate": "acquired",
            "target_entity": "Beta",
            "chunk_id": "c_cond",
            "verbatim_evidence": "Acme acquired Beta",
        }],
    })
    entities, rels = extract_pdf_relationships(
        workspace_id="ws_cond",
        document_id="doc_cond",
        document_version="v1",
        chunks=[chunk],
        model_output_json=payload,
        publication_id="pub_test",
    )
    assert len(rels) == 0


def test_grounding_rejects_evidence_stripping_negation_or_hypothetical():
    """covers: Gap 2 Reject evidence that strips negated or hypothetical surrounding context."""
    # Negated context
    chunk_neg = make_chunk(
        id="c_neg",
        workspace_id="ws_g",
        document_id="doc_g",
        text="Beta was never acquired by Acme during the restructuring.",
    )
    payload_neg = json.dumps({
        "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
        "relationships": [{
            "source_entity": "Acme",
            "predicate": "acquired",
            "target_entity": "Beta",
            "chunk_id": "c_neg",
            "verbatim_evidence": "Acme",
        }],
    })
    _, rels_neg = extract_pdf_relationships(
        workspace_id="ws_g",
        document_id="doc_g",
        document_version="v1",
        chunks=[chunk_neg],
        model_output_json=payload_neg,
        publication_id="pub_test",
    )
    assert len(rels_neg) == 0

    # Hypothetical context
    chunk_hypo = make_chunk(
        id="c_hypo",
        workspace_id="ws_g",
        document_id="doc_g",
        text="Acme might acquire Beta next quarter.",
    )
    payload_hypo = json.dumps({
        "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
        "relationships": [{
            "source_entity": "Acme",
            "predicate": "acquire",
            "target_entity": "Beta",
            "chunk_id": "c_hypo",
            "verbatim_evidence": "Acme might acquire Beta",
        }],
    })
    _, rels_hypo = extract_pdf_relationships(
        workspace_id="ws_g",
        document_id="doc_g",
        document_version="v1",
        chunks=[chunk_hypo],
        model_output_json=payload_hypo,
        publication_id="pub_test",
    )
    assert len(rels_hypo) == 0


def test_grounding_accepts_repeated_evidence_when_one_context_is_factual():
    """covers: Gap 2 Accept repeated evidence string when at least one occurrence is factual."""
    chunk = make_chunk(
        id="c_repeat",
        workspace_id="ws_g",
        document_id="doc_g",
        page_number=3,
        text="If Acme acquired Beta, costs would rise. However, in 2023 Acme acquired Beta in an all cash transaction.",
    )
    payload = json.dumps({
        "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
        "relationships": [{
            "source_entity": "Acme",
            "predicate": "acquired",
            "target_entity": "Beta",
            "chunk_id": "c_repeat",
            "verbatim_evidence": "Acme acquired Beta",
        }],
    })
    _, rels = extract_pdf_relationships(
        workspace_id="ws_g",
        document_id="doc_g",
        document_version="v1",
        chunks=[chunk],
        model_output_json=payload,
        publication_id="pub_test",
    )
    assert len(rels) == 1
    assert rels[0].source_entity_name == "Acme"
    assert rels[0].target_entity_name == "Beta"
    assert rels[0].verbatim_evidence == "Acme acquired Beta"
    assert rels[0].location.page == 3


def test_grounding_rejects_intervening_actor_and_reporting_verb():
    """covers: Gap 3 Bind actor to predicate and reject intervening actors or reporting verbs."""
    text = "Acme said Gamma acquired Beta."
    ok, reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Acme",
        predicate="acquired",
        target_entity_name="Beta",
    )
    assert ok is False

    chunk = make_chunk(id="c_actor", workspace_id="ws_a", document_id="doc_a", text=text)
    payload = json.dumps({
        "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
        "relationships": [{
            "source_entity": "Acme",
            "predicate": "acquired",
            "target_entity": "Beta",
            "chunk_id": "c_actor",
            "verbatim_evidence": "Acme said Gamma acquired Beta",
        }],
    })
    _, rels = extract_pdf_relationships(
        workspace_id="ws_a",
        document_id="doc_a",
        document_version="v1",
        chunks=[chunk],
        model_output_json=payload,
        publication_id="pub_test",
    )
    assert len(rels) == 0


def test_grounding_rejects_nested_clause_reported_speech():
    """covers: Gap 3 Reject nested clause reported speech."""
    text = "Acme reported that Gamma acquired Beta yesterday."
    ok, reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Acme",
        predicate="acquired",
        target_entity_name="Beta",
    )
    assert ok is False


def test_grounding_rejects_multiple_actors_or_unrelated_clauses():
    """covers: Gap 3 Reject entity pairs across unrelated clauses."""
    text = "Acme acquired Gamma, while Beta opened an office."
    ok, reason = validate_relationship_grounding(
        chunk_text=text,
        source_entity_name="Acme",
        predicate="acquired",
        target_entity_name="Beta",
    )
    assert ok is False
    assert reason == "unrelated_clauses"


def test_grounding_preserves_valid_active_and_passive_attachment():
    """covers: Gap 3 Preserve direct active and passive relationships."""
    active_text = "Acme officially acquired Beta yesterday."
    ok_act, _ = validate_relationship_grounding(
        chunk_text=active_text,
        source_entity_name="Acme",
        predicate="acquired",
        target_entity_name="Beta",
    )
    assert ok_act is True

    passive_text = "Beta was officially acquired by Acme yesterday."
    ok_pass, _ = validate_relationship_grounding(
        chunk_text=passive_text,
        source_entity_name="Acme",
        predicate="acquired",
        target_entity_name="Beta",
    )
    assert ok_pass is True


def test_build_pdf_graph_rejects_output_tokens_exceeding_call_limit():
    """covers: Gap 4 Reject output tokens exceeding call limit (801 > 800) and retain spent budget."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)
    chunk = make_chunk(id="c_lim1", workspace_id="ws_lim", document_id="doc_lim1", text="Acme acquired Beta")

    client = MagicMock()
    client.count_tokens.return_value = {"inputTokens": 200}
    client.converse.return_value = {
        "output": {"message": {"content": [{"text": json.dumps({
            "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
            "relationships": [{"source_entity": "Acme", "predicate": "acquired", "target_entity": "Beta", "chunk_id": "c_lim1", "verbatim_evidence": "Acme acquired Beta"}],
        })}]}},
        "usage": {"inputTokens": 200, "outputTokens": 801},
    }

    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_lim",
        document_id="doc_lim1",
        document_version="v1",
        chunks=[chunk],
        client=client,
    )
    assert manifest.status == "failed"
    assert manifest.relationship_count == 0

    ledger = repo.get_budget_ledger("ws_lim", "doc_lim1", "v1")
    assert ledger.spent_prompt_tokens == 200
    assert ledger.spent_output_tokens == 801
    assert ledger.spent_tokens == 1001


def test_build_pdf_graph_rejects_input_tokens_exceeding_reservation_or_cap():
    """covers: Gap 4 Reject input tokens exceeding reservation or per-call 2000 cap."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)
    chunk = make_chunk(id="c_lim2", workspace_id="ws_lim", document_id="doc_lim2", text="Acme acquired Beta")

    client = MagicMock()
    client.count_tokens.return_value = {"inputTokens": 200}
    client.converse.return_value = {
        "output": {"message": {"content": [{"text": json.dumps({
            "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
            "relationships": [{"source_entity": "Acme", "predicate": "acquired", "target_entity": "Beta", "chunk_id": "c_lim2", "verbatim_evidence": "Acme acquired Beta"}],
        })}]}},
        "usage": {"inputTokens": 2050, "outputTokens": 50},
    }

    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_lim",
        document_id="doc_lim2",
        document_version="v1",
        chunks=[chunk],
        client=client,
    )
    assert manifest.status == "failed"
    assert manifest.relationship_count == 0

    ledger = repo.get_budget_ledger("ws_lim", "doc_lim2", "v1")
    assert ledger.spent_prompt_tokens == 2050
    assert ledger.spent_output_tokens == 50


def test_build_pdf_graph_rejects_aggregate_tokens_overrun_across_retries():
    """covers: Gap 4 Reject cumulative token overrun across attempts (prior 3500 + new 600 > 4000)."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)

    # Seed prior attempt spend into ledger
    prior_ledger = KnowledgeGraphBudgetLedger(
        workspace_id="ws_lim",
        document_id="doc_lim3",
        document_version="v1",
        spent_prompt_tokens=3500,
        spent_output_tokens=500,
        spent_tokens=4000,
        completed_calls=1,
        updated_at="2026-11-17T00:00:00Z",
    )
    repo.save_budget_ledger(prior_ledger)

    chunk = make_chunk(id="c_lim3", workspace_id="ws_lim", document_id="doc_lim3", text="Acme acquired Beta")
    client = MagicMock()
    # Count tokens estimates 300 (3500 + 300 = 3800 <= 4000, reservation granted)
    client.count_tokens.return_value = {"inputTokens": 300}
    # Converse returns 600 (3500 + 600 = 4100 > 4000, aggregate limit overrun during call)
    client.converse.return_value = {
        "output": {"message": {"content": [{"text": json.dumps({
            "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
            "relationships": [{"source_entity": "Acme", "predicate": "acquired", "target_entity": "Beta", "chunk_id": "c_lim3", "verbatim_evidence": "Acme acquired Beta"}],
        })}]}},
        "usage": {"inputTokens": 600, "outputTokens": 50},
    }

    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_lim",
        document_id="doc_lim3",
        document_version="v1",
        chunks=[chunk],
        client=client,
    )
    assert manifest.status == "failed"
    assert manifest.relationship_count == 0

    ledger = repo.get_budget_ledger("ws_lim", "doc_lim3", "v1")
    assert ledger.spent_prompt_tokens == 4100
    assert ledger.spent_output_tokens == 550
    assert ledger.spent_tokens == 4650


def test_build_pdf_graph_valid_boundary_counts():
    """covers: Gap 4 Accept valid boundary usage (exact 2000 input, exact 800 output)."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)
    chunk = make_chunk(id="c_bound", workspace_id="ws_bnd", document_id="doc_bnd", text="Acme acquired Beta")

    client = MagicMock()
    client.count_tokens.return_value = {"inputTokens": 2000}
    client.converse.return_value = {
        "output": {"message": {"content": [{"text": json.dumps({
            "entities": [{"name": "Acme", "entity_type": "organization"}, {"name": "Beta", "entity_type": "organization"}],
            "relationships": [{"source_entity": "Acme", "predicate": "acquired", "target_entity": "Beta", "chunk_id": "c_bound", "verbatim_evidence": "Acme acquired Beta"}],
        })}]}},
        "usage": {"inputTokens": 2000, "outputTokens": 800},
    }

    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_bnd",
        document_id="doc_bnd",
        document_version="v1",
        chunks=[chunk],
        client=client,
    )
    assert manifest.status == "ready"
    assert manifest.relationship_count == 1


def test_build_pdf_graph_preserves_ready_publication_on_usage_overrun():
    """covers: Gap 4 Preserve existing ready publication when a new attempt encounters usage overrun."""
    repo = InMemoryKnowledgeGraphRepository()
    builder = KnowledgeGraphBuilder(repository=repo)

    existing_manifest = KnowledgeGraphManifest(
        workspace_id="ws_pres",
        document_id="doc_pres",
        document_version="v1",
        publication_id="pub_existing_ready",
        status="ready",
        entity_count=2,
        relationship_count=1,
        ingested_at_utc="2026-11-17T00:00:00Z",
        content_hash="older_hash",
    )
    repo.commit_manifest("ws_pres", existing_manifest)

    chunk = make_chunk(id="c_pres", workspace_id="ws_pres", document_id="doc_pres", text="Acme acquired Beta")
    client = MagicMock()
    client.count_tokens.return_value = {"inputTokens": 200}
    client.converse.return_value = {
        "output": {"message": {"content": [{"text": "{}"}]}},
        "usage": {"inputTokens": 200, "outputTokens": 801},  # overrun
    }

    manifest = builder.build_and_persist_pdf_graph(
        workspace_id="ws_pres",
        document_id="doc_pres",
        document_version="v1",
        chunks=[chunk],
        client=client,
    )
    assert manifest.status == "ready"
    assert manifest.publication_id == "pub_existing_ready"

    active = repo.get_manifest("ws_pres", "doc_pres", "v1")
    assert active is not None
    assert active.status == "ready"
    assert active.publication_id == "pub_existing_ready"


def test_durable_token_limit_accounting_across_sqlite_and_dynamodb_local():
    """covers: Gap 4 Verify durable cumulative token limit accounting across SQLite and DynamoDB Local."""
    # 1. SQLite repository
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        sqlite_path = tf.name
    try:
        sqlite_repo = SQLiteKnowledgeGraphRepository(db_path=sqlite_path)
        ledger_sqlite = KnowledgeGraphBudgetLedger(
            workspace_id="ws_dur",
            document_id="doc_dur",
            document_version="v1",
            spent_prompt_tokens=4001,
            spent_output_tokens=100,
            spent_tokens=4101,
            completed_calls=1,
            updated_at="2026-11-17T00:00:00Z",
        )
        sqlite_repo.save_budget_ledger(ledger_sqlite)

        res_sqlite = sqlite_repo.reserve_extraction_budget(
            workspace_id="ws_dur",
            document_id="doc_dur",
            document_version="v1",
            worker_id="worker_sqlite",
            estimated_prompt_tokens=100,
        )
        assert res_sqlite.granted is False
        assert res_sqlite.reason == "tokens_exhausted"
    finally:
        if os.path.exists(sqlite_path):
            os.remove(sqlite_path)

    # 2. DynamoDB Local repository
    dynamodb_repo = _get_dynamodb_local_repo("kre_kg_test_slice2_token_limits")
    ledger_ddb = KnowledgeGraphBudgetLedger(
        workspace_id="ws_dur_ddb",
        document_id="doc_dur_ddb",
        document_version="v1",
        spent_prompt_tokens=4001,
        spent_output_tokens=100,
        spent_tokens=4101,
        completed_calls=1,
        updated_at="2026-11-17T00:00:00Z",
    )
    dynamodb_repo.save_budget_ledger(ledger_ddb)

    res_ddb = dynamodb_repo.reserve_extraction_budget(
        workspace_id="ws_dur_ddb",
        document_id="doc_dur_ddb",
        document_version="v1",
        worker_id="worker_ddb",
        estimated_prompt_tokens=100,
    )
    assert res_ddb.granted is False
    assert res_ddb.reason == "tokens_exhausted"
