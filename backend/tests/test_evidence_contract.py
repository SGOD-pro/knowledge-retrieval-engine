"""Tests for EvidenceItem, RetrievalStrategyResult, and retained-evidence citation validation."""

import pytest
from services.retrieval.evidence_contract import (
    EvidenceItem,
    RetrievalStrategyResult,
    RetrievalLimits,
    evidence_item_from_chunk,
)
from services.evaluation.benchmark_scorer import validate_citations
from schemas.models import Chunk


def test_evidence_item_creation():
    item = EvidenceItem(
        evidence_id="chk_123",
        workspace_id="ws_test",
        document_id="doc_1",
        document_version="v1",
        source_type="chunk",
        locator={"chunk_id": "chk_123", "page_number": 3},
        text="Sample text content",
        structured_payload=None,
        score=0.88,
        strategy="vector_rerank",
        provenance={"source_file": "doc1.pdf", "page": 3},
        citation_payload={
            "chunk_id": "chk_123",
            "document_filename": "doc1.pdf",
            "page_number": 3,
            "strategy": "vector_rerank",
        },
    )
    assert item.evidence_id == "chk_123"
    assert item.source_type == "chunk"
    assert item.strategy == "vector_rerank"
    assert item.score == 0.88
    d = item.to_dict()
    assert d["evidence_id"] == "chk_123"
    assert d["strategy"] == "vector_rerank"


def test_retrieval_strategy_result_creation():
    item = EvidenceItem(
        evidence_id="chk_1",
        workspace_id="ws_1",
        document_id="doc_1",
        document_version=None,
        source_type="chunk",
        locator={"chunk_id": "chk_1"},
        text="text",
        structured_payload=None,
        score=1.0,
        strategy="bm25",
        provenance={},
        citation_payload={"chunk_id": "chk_1", "document_filename": "doc.pdf", "page_number": 1},
    )
    res = RetrievalStrategyResult(
        strategy_name="bm25",
        query="test query",
        items=[item],
        confidence=0.9,
        latency_ms=12.5,
        remote_call_counts={"bedrock_embedding_calls": 0, "reranker_remote_calls": 0},
        failure_reason=None,
        debug_trace={"matched_terms": ["test"]},
    )
    assert res.strategy_name == "bm25"
    assert len(res.items) == 1
    assert res.items[0].evidence_id == "chk_1"
    assert res.latency_ms == 12.5
    assert res.remote_call_counts["bedrock_embedding_calls"] == 0


def test_citation_validator_rejects_citation_not_in_retained_evidence():
    retained_item = EvidenceItem(
        evidence_id="chk_valid",
        workspace_id="ws_1",
        document_id="doc_1",
        document_version="v1",
        source_type="chunk",
        locator={"chunk_id": "chk_valid", "page_number": 1},
        text="real context",
        structured_payload=None,
        score=0.9,
        strategy="vector_rerank",
        provenance={"source_file": "doc.pdf"},
        citation_payload={"chunk_id": "chk_valid", "document_filename": "doc.pdf", "page_number": 1},
    )

    valid_citation = {
        "chunk_id": "chk_valid",
        "document_filename": "doc.pdf",
        "page_number": 1,
        "strategy": "vector_rerank",
    }
    unretained_citation = {
        "chunk_id": "chk_fabricated",
        "document_filename": "doc.pdf",
        "page_number": 1,
        "strategy": "vector_rerank",
    }

    # Valid citation matches retained item -> PASS
    res1 = validate_citations([valid_citation], retained_evidence_items=[retained_item])
    assert res1["valid"] is True
    assert res1["valid_count"] == 1

    # Unretained citation must FAIL even if properly formatted
    res2 = validate_citations([unretained_citation], retained_evidence_items=[retained_item])
    assert res2["valid"] is False
    assert any("not in retained evidence" in err.lower() for err in res2["errors"])


def test_citation_validator_rejects_fabricated_structured_citation_not_in_retained():
    retained_struct_item = EvidenceItem(
        evidence_id="struct_real",
        workspace_id="ws_1",
        document_id="doc_table",
        document_version="v1",
        source_type="table_row_set",
        locator={"table_id": "sales", "selection_hash": "1234567890abcdef"},
        text="Total sales: 500",
        structured_payload={"selection_hash": "1234567890abcdef", "table_id": "sales", "operator": "sum"},
        score=1.0,
        strategy="structured_table",
        provenance={"table_id": "sales"},
        citation_payload={
            "evidence_type": "structured_aggregate",
            "table_id": "sales",
            "document_version": "v1",
            "operator": "sum",
            "target_column": "sales_amount",
            "selection_hash": "1234567890abcdef",
            "selection_count": 10,
        },
    )

    valid_struct_citation = {
        "evidence_type": "structured_aggregate",
        "table_id": "sales",
        "document_version": "v1",
        "operator": "sum",
        "target_column": "sales_amount",
        "selection_hash": "1234567890abcdef",
        "selection_count": 10,
    }

    fake_struct_citation = {
        "evidence_type": "structured_aggregate",
        "table_id": "sales",
        "document_version": "v1",
        "operator": "sum",
        "target_column": "sales_amount",
        "selection_hash": "fedcba0987654321",  # 16 valid hex chars, but not retained
        "selection_count": 10,
    }

    # Retained structured item matches -> PASS
    res1 = validate_citations([valid_struct_citation], retained_evidence_items=[retained_struct_item])
    assert res1["valid"] is True

    # Fabricated structured item fails -> FAIL
    res2 = validate_citations([fake_struct_citation], retained_evidence_items=[retained_struct_item])
    assert res2["valid"] is False
    assert any("retained" in err.lower() for err in res2["errors"])
