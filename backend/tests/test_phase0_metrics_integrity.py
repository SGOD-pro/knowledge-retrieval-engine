import os
import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from schemas.models import Chunk, QueryRequest
from services.retrieval.planner import Plan
from api.routes import query_endpoint, get_benchmarks_endpoint
from modules.query.query_service import query_service
from db.database import CloudRepository


def make_test_chunk(chunk_id: str, text: str, page: int = 1, reranker_score: float = 0.85) -> Chunk:
    return Chunk(
        id=chunk_id,
        document_id="doc_001",
        source_format="pdf",
        text=text,
        element_type="text",
        page_number=page,
        reranker_score=reranker_score,
    )


# ---------------------------------------------------------------------------
# Phase 0.1: Faithfulness Integrity & Dashboard KPIs
# ---------------------------------------------------------------------------


def test_0_1a_faithfulness_on_not_found_with_non_empty_citations():
    """
    Test 0.1A: When top_chunks are legitimately retrieved (so citations are non-empty)
    but the LLM decides to abstain ("NOT_FOUND"), faithfulness must NOT fall back to 99.59.
    It must be None (abstained), preventing fabricated 99.59% faithfulness on abstention.
    """
    dummy_chunk = make_test_chunk(
        chunk_id="chunk_test_001",
        text="The lease agreement commenced on January 1, 2024.",
        page=1,
    )

    mock_response = MagicMock()
    mock_response.answer = "NOT_FOUND"
    mock_response.confidence_score = 0.4
    mock_response.fast_path = False
    mock_response.citations = [dummy_chunk]
    mock_response.stage_timings = {"vector_ms": 12.0, "llm_ms": 150.0}
    # Deliberately remove faithfulness attribute so getattr(response, "faithfulness", 99.59)
    # triggers the buggy 99.59 default when citations are present.
    del mock_response.faithfulness

    with patch.object(
        query_service.repo,
        "get_workspace_documents",
        return_value={"documents": [{"id": "doc_001", "name": "document.pdf"}]},
    ):
        with patch("services.langgraph_pipeline.pipeline.run", return_value=mock_response):
            req = QueryRequest(query="What is the rent amount?", workspace_id="ws_001")
            res = query_endpoint(req)

            # In buggy code: formatted_citations is non-empty (has 1 chunk),
            # but response has no faithfulness attribute, so
            # getattr(response, "faithfulness", 99.59) if formatted_citations else 0.0
            # returns 99.59!
            # In fixed code: res["faithfulness"] must be None.
            assert len(res["citations"]) > 0, "Test fixture must guarantee non-empty citations!"
            assert res.get("faithfulness") != 99.59, (
                f"Faithfulness must not report fabricated 99.59 when citations exist but LLM abstains! Got: {res.get('faithfulness')}"
            )
            assert res.get("faithfulness") is None, (
                f"Faithfulness on NOT_FOUND with citations must be None, but got: {res.get('faithfulness')}"
            )


def test_0_1b_dashboard_kpis_unverified_when_no_benchmark_data(tmp_path):
    """
    Test 0.1B: When benchmark files do not exist or contain unverified data,
    get_benchmarks_endpoint must return status='UNVERIFIED' and zeroed KPIs,
    not fabricated passing constants (99.59, 79.22, 3.47, 50.65).
    """
    with patch("pathlib.Path.exists", return_value=False):
        data = get_benchmarks_endpoint()
        assert data["status"] == "UNVERIFIED", (
            f"Expected status 'UNVERIFIED' when no benchmark file exists, got: {data['status']}"
        )
        kpis = data["kpis"]
        assert kpis["faithfulness"]["value"] == 0.0, (
            f"Expected faithfulness 0.0, got: {kpis['faithfulness']['value']}"
        )
        assert kpis["recall_5"]["value"] == 0.0, (
            f"Expected recall_5 0.0, got: {kpis['recall_5']['value']}"
        )
        assert kpis["p95_latency"]["value"] == 0.0, (
            f"Expected p95_latency 0.0, got: {kpis['p95_latency']['value']}"
        )
        assert kpis["llm_activation"]["value"] == 0.0, (
            f"Expected llm_activation 0.0, got: {kpis['llm_activation']['value']}"
        )


# ---------------------------------------------------------------------------
# Phase 0.2: Citation Utilization Rate (Full-Path & Fast-Path)
# ---------------------------------------------------------------------------


def test_0_2a_full_path_citation_utilization_tag_matching():
    """
    Test 0.2A: Full-path precision must check presence of chunk tag f'[{c.id}]'
    in compressed_text, surviving paragraph filtering.
    If 1 of 2 top chunks is kept in compressed_text, utilization must be 0.5.
    """
    from services.langgraph_pipeline import run_llm

    c1 = make_test_chunk("chunk_alpha", "Paragraph 1 matching query.\nParagraph 2 extra.")
    c2 = make_test_chunk("chunk_beta", "Irrelevant paragraph entirely.")

    compressed_text = "[chunk_alpha] Paragraph 1 matching query."
    top_chunks = [c1, c2]

    with patch("services.langgraph_pipeline.call_llm") as mock_call:
        mock_call.return_value = {
            "answer": "Paragraph 1 matching query.",
            "citations": [],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        state = {
            "query": "query matching",
            "compressed_text": compressed_text,
            "top_chunks": top_chunks,
            "stage_timings": {},
            "plan": Plan(fast_path=False, use_graph=False, stages=["vector", "compressor", "llm"]),
        }
        res = run_llm(state)
        assert "citation_utilization_rate" in res, "run_llm must output citation_utilization_rate"
        assert res["citation_utilization_rate"] == 0.5, (
            f"Expected citation_utilization_rate 0.5 (1 of 2 chunks used), got {res.get('citation_utilization_rate')}"
        )


def test_0_2b_fast_path_citation_utilization_provenance():
    """
    Test 0.2B: Fast-path bypasses run_compressor, so end_fast_path must compute
    citation_utilization_rate from sentence-to-chunk provenance.
    If 1 of 3 chunks provided the selected sentences, utilization must be 1/3 (0.3333).
    """
    from services.langgraph_pipeline import end_fast_path

    c1 = make_test_chunk("c1", "Revenue grew by 20% year over year with steady revenue growth.")
    c2 = make_test_chunk("c2", "Unrelated paragraph about trees and plants.")
    c3 = make_test_chunk("c3", "Another passage describing mechanical tools.")

    state = {
        "query": "revenue growth",
        "top_chunks": [c1, c2, c3],
        "stage_timings": {},
        "plan": Plan(fast_path=True, use_graph=False, stages=["vector", "end_fast_path"]),
    }

    res = end_fast_path(state)
    assert "citation_utilization_rate" in res, "end_fast_path must output citation_utilization_rate"
    assert res["citation_utilization_rate"] is not None, "citation_utilization_rate must not be None on valid fast-path answer"
    assert round(res["citation_utilization_rate"], 2) == 0.33, (
        f"Expected 0.33 (1 of 3 chunks contributed), got {res.get('citation_utilization_rate')}"
    )


# ---------------------------------------------------------------------------
# Phase 0.3: Unified Benchmark Scorer & Faithfulness Abstention
# ---------------------------------------------------------------------------


def test_0_3_unified_benchmark_scorer():
    """
    Test 0.3: Benchmark scorer module must provide canonical content_match and compute_faithfulness.
    compute_faithfulness must return None on NOT_FOUND / empty answer (not 1.0, not 0.0).
    """
    from services.evaluation.benchmark_scorer import content_match, compute_faithfulness

    # Abstention returns None
    assert compute_faithfulness("NOT_FOUND", "Some valid context text") is None
    assert compute_faithfulness("", "Some valid context text") is None

    # Real answer returns grounded score
    context = "Acme Corp reported $150 million revenue in 2023 with 12% operating margin."
    ans = "Acme Corp reported $150 million revenue."
    score = compute_faithfulness(ans, context)
    assert score is not None
    assert score >= 0.80, f"Expected high faithfulness for grounded answer, got {score}"

    # Content match verification (token overlap and numeric overlap)
    chunk_text = "The quick brown fox jumps over the lazy dog."
    assert content_match(chunk_text, "brown fox jumps") is True
    assert content_match(chunk_text, "quantum mechanics entanglement") is False


# ---------------------------------------------------------------------------
# Phase 0.4: Live Token and Cost Tracking
# ---------------------------------------------------------------------------


def test_0_4_live_token_usage_tracking():
    """
    Test 0.4: Token usage returned by call_llm must propagate through run_llm,
    ResponseObject, and the query_endpoint response payload.
    """
    from services.langgraph_pipeline import run_llm

    dummy_chunk = make_test_chunk("c1", "Some context text")
    with patch("services.langgraph_pipeline.call_llm") as mock_call:
        mock_call.return_value = {
            "answer": "Grounded answer text.",
            "citations": [],
            "usage": {"input_tokens": 250, "output_tokens": 45},
        }
        state = {
            "query": "query",
            "compressed_text": "[c1] Some context text",
            "top_chunks": [dummy_chunk],
            "stage_timings": {},
            "plan": Plan(fast_path=False, use_graph=False, stages=["vector", "compressor", "llm"]),
        }
        res = run_llm(state)
        assert "usage" in res, "run_llm must output usage dict"
        assert res["usage"] == {"input_tokens": 250, "output_tokens": 45}, (
            f"Expected usage with 250 in, 45 out, got {res.get('usage')}"
        )
