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
)
from src.ingestion.graph_builder import (
    KnowledgeGraphBuilder,
    extract_pdf_relationships,
    validate_relationship_grounding,
)
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
) -> Chunk:
    return Chunk(
        id=id,
        document_id=document_id,
        source_format="pdf",
        text=text,
        element_type="text",
        page_number=page_number,
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
