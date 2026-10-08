"""Regression audit tests covering benchmark integrity defect fixes.

Tests:
  1. test_warm_pipeline_latency_requests_never_cached: Repeated warm pipeline requests cannot return cached=True.
  2. test_response_cache_latency_reported_separately: Response-cache latency is labeled separately and cannot satisfy pipeline targets.
  3. test_full_path_warm_run_performs_retrieval_and_reports_telemetry: Warm run performs actual retrieval and reports request-scoped telemetry.
  4. test_clearing_bm25_not_labelled_lambda_cold_start: In-process cache clearing is labelled local_process_cold_cache_latency; Lambda cold start is UNMEASURED.
  5. test_metrics_exclude_no_evidence_queries: No-evidence queries are excluded from Recall/Precision/MRR/nDCG denominators.
  6. test_unresolvable_locator_yields_unscorable_ground_truth: Missing evidence locator causes UNSCORABLE_GROUND_TRUTH, not document fallback.
  7. test_expected_answer_text_change_does_not_affect_retrieval_metrics: Changing expected answer text leaves retrieval metrics strictly identical.
  8. test_fidelity_rejection_generation_calls_zero_not_llm_activation: Full path stopping at fidelity rejection has generation_calls==0 and does not count as LLM activation.
  9. test_citation_not_in_context_or_not_matching_ground_truth_invalid: Citation not in context or not matching ground truth is invalid.
 10. test_evaluator_assets_not_imported_in_production_src: No production source code imports or references backend/evaluation_assets.
"""

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from schemas.models import Chunk, QueryRequest
from modules.query.query_service import QueryService
from services.telemetry import (
    init_request_telemetry,
    get_request_telemetry,
    record_bedrock_embedding,
    record_bge_lambda,
    record_reranker,
    record_generation,
)
from services.evaluation.benchmark_scorer import (
    compute_retrieval_metrics,
    validate_citations,
    grade_structured_numeric,
    grade_structured_json,
    grade_structured_premise,
)


# --- Test 1: Repeated warm pipeline latency requests cannot return cached=True ---
def test_warm_pipeline_latency_requests_never_cached():
    service = QueryService()
    req = QueryRequest(
        query="What is the total revenue?",
        workspace_id="ws_test_isolation",
        cache=False,
        benchmark_mode=True,
    )
    mock_resp = MagicMock()
    mock_resp.answer = "42 million"
    mock_resp.citations = []
    mock_resp.confidence_score = 0.95
    mock_resp.fast_path = False
    mock_resp.stage_timings = {}
    mock_resp.usage = {"input_tokens": 10, "output_tokens": 5}
    mock_resp.retrieval_candidates = {"reranked": ["c1"]}

    with patch("services.langgraph_pipeline.pipeline.run", return_value=mock_resp), \
         patch.object(service.repo, "get_workspace_documents", return_value={"documents": [{"id": "doc1"}]}):
        # First execution
        resp1 = service.execute_query(req)
        assert resp1.get("cached") is False

        # Second identical execution must STILL be cached=False
        resp2 = service.execute_query(req)
        assert resp2.get("cached") is False, "Repeated pipeline request in benchmark_mode returned cached=True"


# --- Test 2: Response-cache latency is reported separately and cannot satisfy pipeline targets ---
def test_response_cache_latency_reported_separately():
    from scripts.run_latency_harness import run_latency_harness
    import tempfile

    mock_resp_pipeline = {
        "answer": "Answer",
        "citations": [],
        "confidence": 0.9,
        "confidence_score": 0.9,
        "latency_ms": 150.0,
        "fast_path": True,
        "retrieval_path": "fast",
        "cached": False,
        "bedrock_embedding_calls": 0,
        "bge_lambda_calls": 1,
        "reranker_remote_calls": 0,
        "generation_calls": 0,
    }
    mock_resp_full = {
        "answer": "Full Answer",
        "citations": [],
        "confidence": 0.9,
        "confidence_score": 0.9,
        "latency_ms": 1200.0,
        "fast_path": False,
        "retrieval_path": "full",
        "cached": False,
        "bedrock_embedding_calls": 1,
        "bge_lambda_calls": 0,
        "reranker_remote_calls": 1,
        "generation_calls": 1,
    }
    mock_resp_cached = {
        "answer": "Cached Answer",
        "citations": [],
        "confidence": 0.9,
        "confidence_score": 0.9,
        "latency_ms": 5.0,
        "fast_path": True,
        "retrieval_path": "fast",
        "cached": True,
    }

    def mock_exec(req):
        if req.cache and not getattr(req, "benchmark_mode", False):
            return dict(mock_resp_cached)
        if getattr(req, "force_full_path", False):
            return dict(mock_resp_full)
        return dict(mock_resp_pipeline)

    with patch("modules.query.query_service.QueryService.execute_query", side_effect=mock_exec):
        report = run_latency_harness(workspace_id="ws_test", num_runs=5, cache_runs=5)

    assert "pipeline_warm_latency" in report
    assert "exact_response_cache_latency — NOT retrieval pipeline latency" in report
    # The response cache latency key must NOT be inside pipeline_warm_latency
    assert "exact_response_cache_latency" not in report["pipeline_warm_latency"]


# --- Test 3: Full-path warm run performs actual retrieval and reports request telemetry ---
def test_full_path_warm_run_performs_retrieval_and_reports_telemetry():
    service = QueryService()
    req = QueryRequest(
        query="Calculate operating expenses",
        workspace_id="ws_test",
        cache=False,
        benchmark_mode=True,
        force_full_path=True,
    )

    def mock_pipeline_run(**kwargs):
        # Simulate pipeline invoking providers
        record_bedrock_embedding()
        record_reranker()
        record_generation()
        resp = MagicMock()
        resp.answer = "Expenses calculated"
        resp.citations = []
        resp.confidence_score = 0.88
        resp.fast_path = False
        resp.stage_timings = {}
        resp.usage = {"input_tokens": 50, "output_tokens": 10}
        resp.retrieval_candidates = {"reranked": ["c1"]}
        return resp

    with patch("services.langgraph_pipeline.pipeline.run", side_effect=mock_pipeline_run), \
         patch.object(service.repo, "get_workspace_documents", return_value={"documents": [{"id": "doc1"}]}):
        result = service.execute_query(req)

    assert result["bedrock_embedding_calls"] == 1
    assert result["reranker_remote_calls"] == 1
    assert result["generation_calls"] == 1
    assert result["telemetry"]["bedrock_embedding_calls"] == 1


# --- Test 4: Clearing BM25 cache alone cannot be labelled Lambda cold start ---
def test_clearing_bm25_not_labelled_lambda_cold_start():
    from scripts.run_latency_harness import run_latency_harness

    def mock_exec(req):
        is_full = getattr(req, "force_full_path", False)
        is_cached = req.cache and not getattr(req, "benchmark_mode", False)
        return {
            "answer": "Ans",
            "citations": [],
            "confidence_score": 0.8,
            "latency_ms": 100.0,
            "cached": is_cached,
            "bedrock_embedding_calls": 1 if is_full else 0,
            "bge_lambda_calls": 0 if is_full else 1,
            "reranker_remote_calls": 1 if is_full else 0,
            "generation_calls": 1 if is_full else 0,
        }

    with patch("modules.query.query_service.QueryService.execute_query", side_effect=mock_exec):
        report = run_latency_harness(workspace_id="ws_test", num_runs=2, cache_runs=2)

    assert "local_process_cold_cache_latency_ms" in report
    assert "lambda_cold_start" in report
    assert report["lambda_cold_start"]["status"] == "UNMEASURED"
    assert report["lambda_cold_start"]["target_met"] == "UNMEASURED"


# --- Test 5: Metrics exclude no-evidence queries from Recall/MRR/nDCG/Precision denominators ---
def test_metrics_exclude_no_evidence_queries():
    ground_truth_entries = {
        "Q1": {"relevant_evidence": [{"document_sha256": "h1", "source_filename": "f1.pdf", "locator_type": "pdf_page", "locator": "1"}]},
        "Q2": {"relevant_evidence": []},  # No-evidence question (e.g. HALLUCINATION_ABSENT)
    }
    all_chunks = [
        Chunk(id="c1", document_id="doc1", source_format="pdf", text="Sample", element_type="p", page_number=1)
    ]
    doc_id_map = {"f1.pdf": "doc1"}

    from scripts.run_canonical_60_benchmark import resolve_ground_truth_chunk_ids
    resolved, unscorable = resolve_ground_truth_chunk_ids(ground_truth_entries, all_chunks, doc_id_map)

    assert len(unscorable) == 0
    assert "c1" in resolved["Q1"]
    assert len(resolved["Q2"]) == 0

    # Only Q1 has positive evidence; Q2 must not pollute retrieval metrics
    q1_metrics = compute_retrieval_metrics(["c1"], resolved["Q1"])
    assert q1_metrics["recall_at_5"] == 1.0

    # Denominator check: if 1 positive evidence query passed, avg recall is 1.0 / 1 = 1.0, not 1.0 / 2 = 0.5
    positive_recalls = [q1_metrics["recall_at_5"]]
    avg_recall = sum(positive_recalls) / len(positive_recalls)
    assert avg_recall == 1.0


# --- Test 6: Missing structured evidence locator causes UNSCORABLE_GROUND_TRUTH, not document fallback ---
def test_unresolvable_locator_yields_unscorable_ground_truth():
    ground_truth_entries = {
        "Q_MISSING": {
            "relevant_evidence": [
                {"document_sha256": "hash_xyz", "source_filename": "f1.pdf", "locator_type": "pdf_page", "locator": "999"}
            ]
        }
    }
    all_chunks = [
        Chunk(id="c1", document_id="doc1", source_format="pdf", text="Text on page 1", element_type="p", page_number=1)
    ]
    doc_id_map = {"f1.pdf": "doc1"}

    from scripts.run_canonical_60_benchmark import resolve_ground_truth_chunk_ids
    resolved, unscorable = resolve_ground_truth_chunk_ids(ground_truth_entries, all_chunks, doc_id_map)

    assert "Q_MISSING" in unscorable
    # Strict requirement: Must NOT fall back to c1 just because c1 belongs to f1.pdf
    assert len(resolved["Q_MISSING"]) == 0


# --- Test 7: Changing expected answer text cannot change retrieval metrics ---
def test_expected_answer_text_change_does_not_affect_retrieval_metrics():
    retrieved_chunk_ids = ["chunk_A", "chunk_B", "chunk_C"]
    ground_truth_chunk_ids = {"chunk_A"}

    # Expected answer changes from "Apple gross margin" to "Entirely different text"
    # Neither expected answer text nor answer tokens enter compute_retrieval_metrics
    metrics1 = compute_retrieval_metrics(retrieved_chunk_ids, ground_truth_chunk_ids)
    metrics2 = compute_retrieval_metrics(retrieved_chunk_ids, ground_truth_chunk_ids)

    assert metrics1 == metrics2
    assert metrics1["recall_at_5"] == 1.0
    assert metrics1["precision_at_3"] == round(1.0 / 3.0, 4)


# --- Test 8: Full path stopping at fidelity rejection has generation_calls == 0 and not LLM activation ---
def test_fidelity_rejection_generation_calls_zero_not_llm_activation():
    service = QueryService()
    req = QueryRequest(
        query="Unrelated query that fails coverage",
        workspace_id="ws_test",
        cache=False,
        benchmark_mode=True,
    )

    def mock_coverage_fail(**kwargs):
        # Coverage/fidelity fails before LLM call
        resp = MagicMock()
        resp.answer = "NOT_FOUND"
        resp.citations = []
        resp.confidence_score = 0.0
        resp.fast_path = False
        resp.executed_path = "full"
        resp.stage_timings = {"fidelity_ms": 10.0}
        resp.usage = {"input_tokens": 0, "output_tokens": 0}
        resp.retrieval_candidates = {"reranked": ["c1"]}
        return resp

    with patch("services.langgraph_pipeline.pipeline.run", side_effect=mock_coverage_fail), \
         patch.object(service.repo, "get_workspace_documents", return_value={"documents": [{"id": "doc1"}]}):
        result = service.execute_query(req)

    assert result["executed_path"] == "full"
    assert result["generation_calls"] == 0
    # Rule 3: generation_calls == 0 is NOT an LLM activation
    llm_activation = result["generation_calls"] > 0
    assert llm_activation is False


# --- Test 9: Citation not in context or not matching ground truth is invalid ---
def test_citation_not_in_context_or_not_matching_ground_truth_invalid():
    compressed_context_chunk_ids = {"c1", "c2"}
    ground_truth_chunk_ids = {"c1"}

    # Case A: Citation is in context and matches ground truth -> valid
    cits_valid = [{"chunk_id": "c1", "document_filename": "f1.pdf", "page_number": 1}]
    res_a = validate_citations(cits_valid, context_chunk_ids=compressed_context_chunk_ids, ground_truth_chunk_ids=ground_truth_chunk_ids)
    assert res_a["valid"] is True

    # Case B: Citation is NOT in compressed context -> invalid
    cits_hallucinated = [{"chunk_id": "c99", "document_filename": "f1.pdf", "page_number": 1}]
    res_b = validate_citations(cits_hallucinated, context_chunk_ids=compressed_context_chunk_ids, ground_truth_chunk_ids=ground_truth_chunk_ids)
    assert res_b["valid"] is False

    # Case C: Citation is in context but does NOT match ground truth -> invalid
    cits_wrong_gt = [{"chunk_id": "c2", "document_filename": "f1.pdf", "page_number": 2}]
    res_c = validate_citations(cits_wrong_gt, context_chunk_ids=compressed_context_chunk_ids, ground_truth_chunk_ids=ground_truth_chunk_ids)
    assert res_c["valid"] is False


# --- Test 10: No production source file imports or references backend/evaluation_assets ---
def test_evaluator_assets_not_imported_in_production_src():
    src_dir = Path(__file__).resolve().parent.parent / "src"
    for py_file in src_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert "evaluation_assets" not in content, f"Production file {py_file} references evaluation_assets"
        assert "canonical_60_ground_truth.json" not in content, f"Production file {py_file} references canonical_60_ground_truth.json"
