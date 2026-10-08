"""Mandatory audit guard tests for trustworthy evaluation & production retrieval.

Tests:
  1. test_manifest_mismatch_rejects_comparison
  2. test_planned_path_differs_from_executed_path
  3. test_full_path_makes_at_most_one_generation_call
  4. test_deterministic_executor_unseen_schemas
  5. test_context_budget_preserves_evidence_and_validates_citations
  6. test_no_benchmark_leakage_in_production_code
  7. test_benchmark_fails_when_provider_mocked
  8. test_corpus_hash_change_invalidates_cache_and_comparison
  9. test_fast_path_zero_remote_calls
 10. test_full_path_single_remote_embedding_call
"""

import ast
from decimal import Decimal
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from services.evaluation.benchmark_comparator import (
    compare_manifests,
    NOT_COMPARABLE_MESSAGE,
    REQUIRED_EQUAL_FIELDS,
)
from services.evaluation.benchmark_scorer import (
    compute_retrieval_metrics,
    grade_premise_correction,
    grade_arithmetic_answer,
    grade_json_schema,
    validate_citations,
)
from schemas.models import Chunk, QueryRequest
from services.retrieval.deterministic_executor import DeterministicExecutor
from services.retrieval.bm25_retriever import get_cached_chunks, invalidate_chunk_cache
from providers.embedding_provider import reset_token_counter, get_token_counter
from providers.llm_provider import reset_llm_counter, get_llm_counter


# --- Test 1: Manifest comparison rejects mismatches ---
def test_manifest_mismatch_rejects_comparison():
    base_manifest = {
        "commit_sha": "commit_base_123",
        "baseline_commit_sha": "commit_root_000",
        "test_suite_sha256": "hash_suite_aaa",
        "corpus_manifest_sha256": "hash_corpus_bbb",
        "workspace_id": "ws_fresh_benchmark",
        "runner_version": "2.1.0",
        "scorer_version": "2.1.0",
        "embedding_model": "amazon.titan-embed-text-v2:0",
        "llm_model": "amazon.nova-lite-v1:0",
        "reranker_policy": "openrouter:cross-encoder/ms-marco-MiniLM-L-6-v2",
        "cache_mode": "bypass_exact_and_semantic",
        "benchmark_type": "cold",
        "metrics": {
            "overall_accuracy": 0.80,
            "refusal_accuracy": 0.85,
            "grounded_recall_at_5": 0.85,
            "precision_at_3": 0.70,
            "mrr_at_5": 0.75,
            "ndcg_at_5": 0.80,
            "llm_activation_rate": 0.50,
        },
    }

    # Candidate with different commit_sha but baseline_commit_sha pointing to baseline commit
    valid_candidate = dict(base_manifest)
    valid_candidate["commit_sha"] = "commit_cand_456"
    valid_candidate["baseline_commit_sha"] = "commit_base_123"
    valid_candidate["metrics"] = dict(base_manifest["metrics"])
    valid_candidate["metrics"]["overall_accuracy"] = 0.85

    res = compare_manifests(valid_candidate, base_manifest)
    assert res["comparable"] is True
    assert res["status"] == "COMPARABLE"
    assert res["deltas"]["overall_accuracy_delta"] == 0.05

    # 1. Mismatch when baseline_commit_sha does not equal baseline commit_sha
    cand_wrong_base = dict(valid_candidate)
    cand_wrong_base["baseline_commit_sha"] = "wrong_commit_999"
    res_wrong_base = compare_manifests(cand_wrong_base, base_manifest)
    assert res_wrong_base["comparable"] is False
    assert res_wrong_base["status"] == NOT_COMPARABLE_MESSAGE

    # 2. Mismatch on any of the required 9 fields must reject comparison
    for field in REQUIRED_EQUAL_FIELDS:
        mismatched_candidate = dict(valid_candidate)
        mismatched_candidate[field] = f"altered_{field}"
        res_mismatch = compare_manifests(mismatched_candidate, base_manifest)
        assert res_mismatch["comparable"] is False
        assert res_mismatch["status"] == NOT_COMPARABLE_MESSAGE
        assert any(field in m for m in res_mismatch["mismatches"])


# --- Test 2: Planned path differs from executed path on escalation ---
def test_planned_path_differs_from_executed_path():
    from services.langgraph_pipeline import pipeline

    # Create dummy chunks that produce low similarity/extraction failure
    dummy_chunk = Chunk(
        id="c1",
        document_id="doc1",
        source_format="pdf",
        text="A totally unrelated sentence about geology and igneous rock formations.",
        element_type="paragraph",
        page_number=1,
        similarity_score=0.30,  # Below fast path threshold
    )

    # Mock retriever and planner to plan fast path initially
    with patch("services.langgraph_pipeline._get_repo") as mock_repo, \
         patch("services.retrieval.planner.planner.route") as mock_route, \
         patch("services.retrieval.vector_retriever.VectorRetriever.search") as mock_vec, \
         patch("services.retrieval.bm25_retriever.BM25Retriever.search") as mock_bm25, \
         patch("providers.llm_provider.generate_completion", return_value=("Escalated LLM answer", {"input_tokens": 10, "output_tokens": 5})):

        from services.retrieval.planner import Plan
        mock_route.return_value = Plan(fast_path=True, use_graph=False, stages=["fast_vector", "verified_extraction"])
        mock_vec.return_value = [(dummy_chunk, 0.30)]
        mock_bm25.return_value = [(dummy_chunk, 0.30)]
        mock_repo.return_value.get_all_chunks.return_value = [dummy_chunk]
        mock_repo.return_value.get_okf_properties.return_value = []

        response = pipeline.run(
            query="What is Apple's revenue in 2026?",
            workspace_id="ws_test",
        )

        assert response.planned_path == "fast"
        # Since extraction failed on unrelated text, pipeline escalated to full path
        assert response.executed_path == "full"


# --- Test 3: Full path makes at most one generation call, math makes 0 ---
def test_full_path_makes_at_most_one_generation_call():
    from services.langgraph_pipeline import pipeline

    dummy_chunk = Chunk(
        id="c1",
        document_id="doc1",
        source_format="pdf",
        text="Apple reported quarterly revenue of 94.9 billion dollars.",
        element_type="paragraph",
        page_number=1,
        similarity_score=0.90,
    )

    reset_llm_counter()
    with patch("services.langgraph_pipeline._get_repo") as mock_repo, \
         patch("services.retrieval.vector_retriever.VectorRetriever.search", return_value=[(dummy_chunk, 0.90)]), \
         patch("services.retrieval.bm25_retriever.BM25Retriever.search", return_value=[(dummy_chunk, 0.90)]), \
         patch("providers.llm_provider.generate_completion", return_value=("Generated answer", {"input_tokens": 50, "output_tokens": 10})):

        mock_repo.return_value.get_all_chunks.return_value = [dummy_chunk]
        mock_repo.return_value.get_okf_properties.return_value = []

        # 1. Full path query
        res_full = pipeline.run(
            query="Summarize Apple quarterly financial results across all segments",
            workspace_id="ws_test",
            force_full_path=True,
        )
        llm_count = get_llm_counter()["generation_calls"]
        assert llm_count <= 1

    # 2. Deterministic math makes exactly 0 generation calls
    reset_llm_counter()
    math_chunk = Chunk(
        id="c2",
        document_id="doc1",
        source_format="csv",
        text="Metric, 2024, 2025\nNet Sales, 100, 150",
        element_type="table",
        page_number=1,
        similarity_score=0.95,
    )

    with patch("services.langgraph_pipeline._get_repo") as mock_repo, \
         patch("services.retrieval.vector_retriever.VectorRetriever.search", return_value=[(math_chunk, 0.95)]), \
         patch("services.retrieval.bm25_retriever.BM25Retriever.search", return_value=[(math_chunk, 0.95)]), \
         patch("services.retrieval.reranker.rerank", return_value=[math_chunk]), \
         patch("services.retrieval.deterministic_executor.DeterministicExecutor.resolve_and_execute") as mock_math:

        from services.retrieval.deterministic_executor import ExecutionResult, ExecutionOperator, Operand
        mock_math.return_value = ExecutionResult(
            result_value=Decimal("50"),
            operator=ExecutionOperator.DIFFERENCE,
            operands=(Operand(raw_value="150", normalized_value=Decimal("150")), Operand(raw_value="100", normalized_value=Decimal("100"))),
            overall_confidence=1.0,
            provenance_citations=({"chunk_id": "c2"},),
        )
        mock_repo.return_value.get_all_chunks.return_value = [math_chunk]
        mock_repo.return_value.get_okf_properties.return_value = []

        res_math = pipeline.run(
            query="What is the difference between Net Sales in 2025 and 2024?",
            workspace_id="ws_test",
        )
        assert res_math.executed_path == "deterministic_math"
        assert get_llm_counter()["generation_calls"] == 0


# --- Test 4: Deterministic executor handles arbitrary unseen tabular schemas ---
def test_deterministic_executor_unseen_schemas():
    executor = DeterministicExecutor()

    # Unseen schema 1: Medical hospital equipment inventory
    table_text_1 = (
        "| Equipment_ID | Category | Units_In_Stock | Unit_Cost_USD |\n"
        "| E-101 | Ventilator | 45 | 12000 |\n"
        "| E-102 | Defibrillator | 80 | 4500 |\n"
    )
    chunk_1 = Chunk(
        id="med_1",
        document_id="doc_med",
        source_format="csv",
        text=table_text_1,
        element_type="table",
        page_number=1,
    )
    res_1 = executor.resolve_and_execute(
        "What is the difference between Units_In_Stock for Defibrillator and Ventilator?",
        [chunk_1],
    )
    assert res_1 is not None
    assert res_1.result_value == Decimal("35")

    # Unseen schema 2: Logistics shipping containers
    table_text_2 = (
        "Warehouse, Max_Capacity, Current_Load\n"
        "North_Hub, 5000, 3200\n"
        "South_Hub, 4000, 3900\n"
    )
    chunk_2 = Chunk(
        id="log_1",
        document_id="doc_log",
        source_format="csv",
        text=table_text_2,
        element_type="table",
        page_number=1,
    )
    res_2 = executor.resolve_and_execute(
        "What is the sum of Current_Load across North_Hub and South_Hub?",
        [chunk_2],
    )
    assert res_2 is not None
    assert res_2.result_value == Decimal("7100")


# --- Test 5: Context budget preserves evidence & citation validator checks IDs ---
def test_context_budget_preserves_evidence_and_validates_citations():
    from services.retrieval.compressor import Compressor

    # Create large chunks
    chunks = [
        Chunk(
            id=f"chk_{i}",
            document_id="doc_test",
            source_format="pdf",
            text=f"Paragraph {i}: " + ("evidence keyword " if i == 2 else "filler context words ") * 50,
            element_type="paragraph",
            page_number=i,
        )
        for i in range(10)
    ]

    compressor = Compressor()
    compressed = compressor.compress(chunks, query="evidence keyword", max_tokens=200)
    assert "evidence keyword" in compressed
    # Assert compression kept text concise
    assert len(compressed.split()) < 300

    # Test Citation validation
    valid_cites = [
        {
            "chunk_id": "chk_2",
            "document_filename": "test.pdf",
            "page_number": 2,
            "location_reference": "Page 2",
        }
    ]
    val_res = validate_citations(valid_cites, context_chunk_ids={"chk_2", "chk_3"})
    assert val_res["valid"] is True
    assert val_res["valid_count"] == 1

    # Citation pointing to non-existent chunk in context
    invalid_cites = [
        {
            "chunk_id": "chk_999",
            "document_filename": "test.pdf",
            "page_number": 1,
            "location_reference": "Page 1",
        }
    ]
    val_res_bad = validate_citations(invalid_cites, context_chunk_ids={"chk_2", "chk_3"})
    assert val_res_bad["valid"] is False
    assert len(val_res_bad["errors"]) > 0


# --- Test 6: No benchmark data leakage in production code (AST scan) ---
def test_no_benchmark_leakage_in_production_code():
    src_dir = Path(__file__).resolve().parent.parent / "src"
    assert src_dir.exists()

    forbidden_patterns = [
        "test.json",
        "holdout",
        "ws_fresh_benchmark",
        "001-36743",
        "14,594,180,000",
    ]
    # Check questions Q136 to Q195
    for q_idx in range(136, 196):
        forbidden_patterns.append(f"Q{q_idx}")

    found_violations = []
    for py_file in src_dir.rglob("*.py"):
        code_text = py_file.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            if pattern in code_text:
                found_violations.append(f"{py_file.name}: contains '{pattern}'")

    assert not found_violations, f"Benchmark leakage detected in src/: {found_violations}"


# --- Test 7: Benchmark runner rejects mocked providers ---
def test_benchmark_fails_when_provider_mocked():
    from scripts.run_canonical_60_benchmark import verify_live_providers

    # If any provider is replaced by a MagicMock, verify_live_providers must fail
    with patch("providers.llm_provider.generate_completion", MagicMock()):
        with pytest.raises(RuntimeError, match="Mocked provider detected"):
            verify_live_providers()


# --- Test 8: Corpus hash change invalidates cache and comparison ---
def test_corpus_hash_change_invalidates_cache_and_comparison():
    invalidate_chunk_cache()
    call_count = [0]

    def dummy_fetcher():
        call_count[0] += 1
        return [Chunk(id=f"c_{call_count[0]}", document_id="d1", source_format="pdf", text="test", element_type="p")]

    # 1. Fetch with corpus_version="hash_v1"
    chunks_1 = get_cached_chunks("ws_audit", dummy_fetcher, corpus_version="hash_v1")
    assert call_count[0] == 1
    assert chunks_1[0].id == "c_1"

    # Second call with same version uses cache
    chunks_cached = get_cached_chunks("ws_audit", dummy_fetcher, corpus_version="hash_v1")
    assert call_count[0] == 1

    # 2. Fetch with corpus_version="hash_v2" invalidates and calls fetcher
    chunks_2 = get_cached_chunks("ws_audit", dummy_fetcher, corpus_version="hash_v2")
    assert call_count[0] == 2
    assert chunks_2[0].id == "c_2"

    # Invalidate cache again
    invalidate_chunk_cache()


# --- Test 9: Accepted fast path makes zero remote calls ---
def test_fast_path_zero_remote_calls():
    from services.langgraph_pipeline import pipeline

    dummy_chunk = Chunk(
        id="fast_c1",
        document_id="doc_fast",
        source_format="pdf",
        text="The Commission File Number is 001-36743.",
        element_type="paragraph",
        page_number=1,
        similarity_score=0.98,
    )

    reset_token_counter()
    reset_llm_counter()

    with patch("services.langgraph_pipeline._get_repo") as mock_repo, \
         patch("services.retrieval.planner.planner.route") as mock_route, \
         patch("services.retrieval.vector_retriever.VectorRetriever.search") as mock_vec, \
         patch("aws.infra.get_client") as mock_bedrock_client:

        from services.retrieval.planner import Plan
        mock_route.return_value = Plan(fast_path=True, use_graph=False, stages=["fast_vector", "verified_extraction"])
        mock_vec.return_value = [(dummy_chunk, 0.98)]
        mock_repo.return_value.get_all_chunks.return_value = [dummy_chunk]
        mock_repo.return_value.get_okf_properties.return_value = []

        # Client would raise if called
        mock_bedrock_client.side_effect = RuntimeError("Bedrock must NOT be called on fast path")

        res = pipeline.run(
            query="What is the Commission File Number?",
            workspace_id="ws_test",
        )

        assert res.executed_path == "fast"
        assert get_token_counter()["embed_calls"] == 0
        assert get_llm_counter()["generation_calls"] == 0


# --- Test 10: Full path makes exactly one remote embedding call and at most one generation call ---
def test_full_path_single_remote_embedding_call():
    from modules.query.query_service import QueryService

    dummy_chunk = Chunk(
        id="full_c1",
        document_id="doc_full",
        source_format="pdf",
        text="In 2026, total international net sales were 45.2 billion dollars.",
        element_type="paragraph",
        page_number=2,
        similarity_score=0.92,
    )

    reset_token_counter()
    reset_llm_counter()

    with patch("modules.query.query_service.QueryRepository") as MockRepo, \
         patch("services.langgraph_pipeline._get_repo") as mock_pipe_repo, \
         patch("services.retrieval.vector_retriever.VectorRetriever.search") as mock_vec, \
         patch("services.retrieval.bm25_retriever.BM25Retriever.search") as mock_bm25, \
         patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024) as mock_embed, \
         patch("providers.llm_provider.generate_completion", return_value=("Sales summary", {"input_tokens": 30, "output_tokens": 15})):

        repo_instance = MockRepo.return_value
        repo_instance.get_workspace_documents.return_value = {"documents": [{"id": "doc_full"}]}
        repo_instance.get_all_chunks.return_value = [dummy_chunk]
        repo_instance.check_semantic_cache.return_value = None  # Cache miss

        mock_pipe_repo.return_value.get_all_chunks.return_value = [dummy_chunk]
        mock_pipe_repo.return_value.get_okf_properties.return_value = []
        mock_vec.return_value = [(dummy_chunk, 0.92)]
        mock_bm25.return_value = [(dummy_chunk, 0.92)]

        service = QueryService(repo=repo_instance)

        # 1. Outside benchmark mode (cache enabled, cache miss)
        req_live = QueryRequest(
            query="Provide a comprehensive summary of international net sales in 2026",
            workspace_id="ws_test",
            cache=True,
            benchmark_mode=False,
            force_full_path=True,
        )
        res_live = service.execute_query(req_live)

        # Assert exactly 1 remote embedding call
        assert mock_embed.call_count == 1
        assert get_llm_counter()["generation_calls"] <= 1

        # 2. Inside benchmark mode (cache disabled)
        mock_embed.reset_mock()
        reset_llm_counter()

        req_bench = QueryRequest(
            query="Provide a comprehensive summary of international net sales in 2026",
            workspace_id="ws_test",
            cache=False,
            benchmark_mode=True,
            force_full_path=True,
        )
        res_bench = service.execute_query(req_bench)

        # Assert exactly 1 remote embedding call in benchmark mode
        assert mock_embed.call_count == 1
        assert get_llm_counter()["generation_calls"] <= 1
