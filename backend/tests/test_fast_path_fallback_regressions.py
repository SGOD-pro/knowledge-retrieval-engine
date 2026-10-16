import pytest
from unittest.mock import patch
from decimal import Decimal

from schemas.models import Chunk, Document
from db.database import CloudRepository
from ingestion.embed_service import embed_fast_local, _deterministic_vector
from services.langgraph_pipeline import end_fast_path, pipeline
from providers.llm_provider import reset_llm_counter, get_llm_counter


def _setup_doc(repo: CloudRepository, workspace_id: str, doc_id: str, text: str, format: str = "pdf"):
    doc = Document(
        id=doc_id,
        filename=f"{doc_id}.{format}",
        source_format=format,
        chunks=(
            Chunk(
                id=f"{doc_id}:c1",
                document_id=doc_id,
                text=text,
                source_format=format,
                page_number=1,
                element_type="paragraph" if format == "pdf" else "table_row",
                section_path=("Main",),
                embedding_fast=embed_fast_local(text),
                embedding_full=_deterministic_vector(text, 1024),
                workspace_id=workspace_id,
                similarity_score=0.85,
            ),
        ),
        workspace_id=workspace_id,
    )
    repo.save(doc)
    return doc


def test_q167_no_longer_crashes_with_unbound_local_error():
    """Reproduce and fix Q167: verified factual extraction must not raise UnboundLocalError."""
    chunk = Chunk(
        id="c_q167",
        document_id="doc_q167",
        text="Selective Dependence Classification (SDC) evaluates interpretability.",
        source_format="pdf",
        page_number=1,
        element_type="paragraph",
        section_path=("Analysis",),
        similarity_score=0.91,
        workspace_id="ws_test",
    )

    state = {
        "query": "What does SDC stand for in Attention Networks?",
        "top_chunks": [chunk],
        "candidate_chunks": [chunk],
        "stage_timings": {"vector_ms": 12.5},
        "verified_answer": "Selective Dependence Classification",
        "verified_chunk": chunk,
        "workspace_id": "ws_test",
    }

    # Must execute cleanly without UnboundLocalError
    result = end_fast_path(state)
    assert result["executed_path"] == "fast"
    assert result["final_answer"] == "Selective Dependence Classification"
    assert result["confidence_score"] == 0.91
    assert len(result["citations"]) == 1
    assert result["citations"][0]["chunk_id"] == "c_q167"
    assert "fast_path_ms" in result["stage_timings"]
    assert result["stage_timings"]["vector_ms"] == 12.5


def test_failed_extraction_cannot_return_unrelated_first_csv_row():
    """When fast path extraction finds no supported answer, it must NOT return
    an arbitrary first chunk or first CSV row with fabricated confidence.
    """
    csv_chunk = Chunk(
        id="c_csv_row1",
        document_id="doc_csv",
        text="Year: 2025. Industry_aggregation_NZSIOC: Level 1. Industry_code_NZSIOC: 99999",
        source_format="csv",
        page_number=1,
        element_type="table_row",
        section_path=("Data",),
        similarity_score=0.45,
        workspace_id="ws_test",
    )

    state = {
        "query": "What is the capital of France?",
        "top_chunks": [csv_chunk],
        "candidate_chunks": [csv_chunk],
        "stage_timings": {"vector_ms": 8.0},
        "workspace_id": "ws_test",
    }

    result = end_fast_path(state)
    # Must NOT return the unrelated CSV row as final_answer
    assert result.get("final_answer") != csv_chunk.text
    # Must NOT fabricate confidence or claim fast path success
    assert result.get("executed_path") != "fast"
    assert result.get("executed_path") == "full"


def test_escalation_executes_existing_full_retrieval_path(monkeypatch):
    """When fast path extraction finds no supported answer, the query must
    escalate to the full retrieval path (reranker, compressor, fidelity, LLM).
    """
    monkeypatch.setenv("ENVIRONMENT", "test")
    repo = CloudRepository()
    ws_id = "ws_escalation_test"

    csv_text = "Year: 2025. Industry_aggregation_NZSIOC: Level 1. Industry_code_NZSIOC: 99999"
    doc_csv = Document(
        id="doc_csv_unrelated",
        filename="survay.csv",
        source_format="csv",
        chunks=(
            Chunk(
                id="doc_csv_unrelated:c1",
                document_id="doc_csv_unrelated",
                text=csv_text,
                source_format="csv",
                page_number=1,
                element_type="table_row",
                section_path=("Data",),
                embedding_fast=embed_fast_local("Who was the primary mission commander of the Apollo landing?"),
                embedding_full=_deterministic_vector(csv_text, 1024),
                workspace_id=ws_id,
                similarity_score=0.90,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_csv)

    prose_text = "The primary mission commander was Neil Armstrong. He piloted the lunar module."
    doc_prose = Document(
        id="doc_prose_target",
        filename="apollo.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_prose_target:c1",
                document_id="doc_prose_target",
                text=prose_text,
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("History",),
                embedding_fast=embed_fast_local("Unrelated text for vector search"),
                embedding_full=_deterministic_vector(prose_text, 1024),
                workspace_id=ws_id,
                similarity_score=0.20,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_prose)

    query = "Who was the primary mission commander of the Apollo landing?"
    reset_llm_counter()
    response = pipeline.run(query, workspace_id=ws_id)

    # Must escalate to full path and generate via LLM
    assert response.planned_path == "fast"
    assert response.executed_path == "full"
    assert response.fast_path is False
    assert "Neil Armstrong" in response.answer
    assert "Year: 2025" not in response.answer
    # Stage timings must include both fast path attempt and subsequent full stages
    assert "fast_path_ms" in response.stage_timings
    assert "reranker_ms" in response.stage_timings
    assert "llm_ms" in response.stage_timings

    # Exactly one LLM generation call occurred (not zero, not multiple)
    llm_stats = get_llm_counter()
    assert llm_stats["generation_calls"] == 1


def test_supported_fast_extraction_still_works_without_generation(monkeypatch):
    """Supported fast extraction (e.g. abbreviation expansion) must complete
    on the fast path with zero LLM generation calls and zero token usage.
    """
    monkeypatch.setenv("ENVIRONMENT", "test")
    repo = CloudRepository()
    ws_id = "ws_fast_supported_test"

    text = "The Retrieval Augmented Generation (RAG) architecture combines retrieval with generative models."
    _setup_doc(repo, ws_id, "doc_rag", text)

    reset_llm_counter()
    query = "What does RAG stand for in neural architectures?"
    response = pipeline.run(query, workspace_id=ws_id)

    assert response.executed_path == "fast"
    assert response.fast_path is True
    assert "Retrieval Augmented Generation" in response.answer
    assert response.usage == {"input_tokens": 0, "output_tokens": 0}
    assert len(response.citations) > 0
    # Zero LLM generation calls
    assert get_llm_counter()["generation_calls"] == 0


def test_unsupported_evidence_produces_refusal_with_empty_citations(monkeypatch):
    """When query escalates to the full path but no relevant evidence exists,
    the pipeline returns NOT_FOUND and query_service returns structured refusal
    with empty citations.
    """
    monkeypatch.setenv("ENVIRONMENT", "test")
    repo = CloudRepository()
    ws_id = "ws_refusal_test"

    unrelated_text = "The quick brown fox jumps over the lazy dog in the middle of a sunny afternoon."
    _setup_doc(repo, ws_id, "doc_unrelated", unrelated_text)

    from modules.query.query_service import QueryService
    from schemas.models import QueryRequest

    qs = QueryService()
    req = QueryRequest(
        workspace_id=ws_id,
        query="What is the total revenue for quantum computing in 2045?",
        cache=False,
    )

    res = qs.execute_query(req)

    # Must produce the standard refusal and empty citations
    assert "couldn't find any relevant passages" in res["answer"].lower()
    assert res["citations"] == []
    assert res["confidence"] == 0.0
