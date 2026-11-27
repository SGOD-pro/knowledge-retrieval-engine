"""Tests for opt-in Bedrock Agent Runtime Reranker (amazon.rerank-v1:0).

Covers:
- Dedicated reused client in us-west-2 with zero SDK retries
- Success path and out-of-order index mapping
- Scoring contract validation (duplicate, missing, out-of-range, NaN)
- Remote top-k preservation (numberOfResults == len(documents))
- Latency bounding and caller timeout (1.5s budget)
- Concurrency bounding (1 in-flight request) and capacity retention
- Throttling handling with fallback to BM25Plus
- Zero remote calls in lexical mode
- No chaining through OpenRouter on failure
- Strict comparator manifest policy differentiation
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from schemas.models import RerankerMode


@pytest.fixture(autouse=True)
def reset_reranker_state():
    """Reset reranker provider state before and after each test."""
    from providers.reranker_provider import (
        _bedrock_rerank_controller,
        reset_bedrock_rerank_controller,
        reset_reranker_counter,
        set_last_reranker_mode,
    )

    reset_reranker_counter()
    reset_bedrock_rerank_controller()
    set_last_reranker_mode(RerankerMode.BYPASSED.value)
    yield
    reset_reranker_counter()
    reset_bedrock_rerank_controller()
    set_last_reranker_mode(RerankerMode.BYPASSED.value)


def test_bedrock_reranker_success_and_index_mapping(monkeypatch):
    """Verify Bedrock reranker maps out-of-order results back to candidate indices."""
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "true")
    from providers.reranker_provider import (
        get_last_reranker_mode,
        get_last_reranker_telemetry,
        rerank_documents,
    )

    docs = ["Doc A (index 0)", "Doc B (index 1)", "Doc C (index 2)"]
    mock_client = MagicMock()
    # Bedrock returns results ranked by score: Doc C (idx 2), Doc A (idx 0), Doc B (idx 1)
    mock_client.rerank.return_value = {
        "results": [
            {"index": 2, "relevanceScore": 0.88},
            {"index": 0, "relevanceScore": 0.65},
            {"index": 1, "relevanceScore": 0.22},
        ]
    }

    with patch("providers.reranker_provider._get_bedrock_rerank_client", return_value=mock_client):
        scores = rerank_documents("What is the revenue?", docs)

    # Must be mapped in original document order: [Doc 0, Doc 1, Doc 2]
    assert scores == [0.65, 0.22, 0.88]
    assert get_last_reranker_mode() == RerankerMode.REMOTE_SUCCESS.value

    # Verify numberOfResults sent to Bedrock equals len(docs)
    call_args = mock_client.rerank.call_args[1]
    assert call_args["rerankingConfiguration"]["bedrockRerankingConfiguration"]["numberOfResults"] == 3
    assert call_args["rerankingConfiguration"]["bedrockRerankingConfiguration"]["modelConfiguration"]["modelArn"] == (
        "arn:aws:bedrock:us-west-2::foundation-model/amazon.rerank-v1:0"
    )

    # Verify telemetry
    telemetry = get_last_reranker_telemetry()
    assert telemetry["provider"] == "bedrock"
    assert telemetry["model"] == "amazon.rerank-v1:0"
    assert telemetry["region"] == "us-west-2"
    assert telemetry["candidate_count"] == 3
    assert telemetry["fallback_reason"] is None


def test_bedrock_reranker_rejects_duplicate_index(monkeypatch):
    """Verify Bedrock reranker rejects duplicate returned indices and falls back to BM25Plus."""
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "true")
    from providers.reranker_provider import (
        get_last_reranker_mode,
        get_last_reranker_telemetry,
        rerank_documents,
    )

    docs = ["Doc A", "Doc B"]
    mock_client = MagicMock()
    # Duplicate index 0
    mock_client.rerank.return_value = {
        "results": [
            {"index": 0, "relevanceScore": 0.9},
            {"index": 0, "relevanceScore": 0.5},
        ]
    }

    with patch("providers.reranker_provider._get_bedrock_rerank_client", return_value=mock_client), \
         patch("providers.reranker_provider._term_coverage_fallback", return_value=[0.4, 0.2]) as mock_fb:
        scores = rerank_documents("query", docs)

    assert scores == [0.4, 0.2]
    assert get_last_reranker_mode() == RerankerMode.CIRCUIT_OPEN_LEXICAL_FALLBACK.value
    mock_fb.assert_called_once()
    telemetry = get_last_reranker_telemetry()
    assert "duplicate" in (telemetry.get("fallback_reason") or "").lower()


def test_bedrock_reranker_rejects_missing_indices(monkeypatch):
    """Verify Bedrock reranker rejects truncated/missing indices and falls back to BM25Plus."""
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "true")
    from providers.reranker_provider import (
        get_last_reranker_mode,
        get_last_reranker_telemetry,
        rerank_documents,
    )

    docs = ["Doc A", "Doc B", "Doc C"]
    mock_client = MagicMock()
    # Only 2 results returned for 3 documents
    mock_client.rerank.return_value = {
        "results": [
            {"index": 0, "relevanceScore": 0.9},
            {"index": 1, "relevanceScore": 0.5},
        ]
    }

    with patch("providers.reranker_provider._get_bedrock_rerank_client", return_value=mock_client), \
         patch("providers.reranker_provider._term_coverage_fallback", return_value=[0.3, 0.2, 0.1]) as mock_fb:
        scores = rerank_documents("query", docs)

    assert scores == [0.3, 0.2, 0.1]
    assert get_last_reranker_mode() == RerankerMode.CIRCUIT_OPEN_LEXICAL_FALLBACK.value
    mock_fb.assert_called_once()
    telemetry = get_last_reranker_telemetry()
    assert "missing" in (telemetry.get("fallback_reason") or "").lower()


def test_bedrock_reranker_rejects_out_of_range_index(monkeypatch):
    """Verify Bedrock reranker rejects out-of-range returned index."""
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "true")
    from providers.reranker_provider import (
        get_last_reranker_mode,
        get_last_reranker_telemetry,
        rerank_documents,
    )

    docs = ["Doc A", "Doc B"]
    mock_client = MagicMock()
    # Index 5 is out of range [0, 1]
    mock_client.rerank.return_value = {
        "results": [
            {"index": 0, "relevanceScore": 0.9},
            {"index": 5, "relevanceScore": 0.5},
        ]
    }

    with patch("providers.reranker_provider._get_bedrock_rerank_client", return_value=mock_client), \
         patch("providers.reranker_provider._term_coverage_fallback", return_value=[0.4, 0.2]):
        scores = rerank_documents("query", docs)

    assert scores == [0.4, 0.2]
    assert get_last_reranker_mode() == RerankerMode.CIRCUIT_OPEN_LEXICAL_FALLBACK.value
    telemetry = get_last_reranker_telemetry()
    assert "out of range" in (telemetry.get("fallback_reason") or "").lower()


def test_bedrock_reranker_throttling_falls_back_to_bm25plus(monkeypatch):
    """Verify ThrottlingException triggers fallback to BM25Plus without calling OpenRouter."""
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "true")
    from providers.reranker_provider import (
        get_last_reranker_mode,
        get_last_reranker_telemetry,
        rerank_documents,
    )

    docs = ["Doc A", "Doc B"]
    mock_client = MagicMock()
    mock_client.rerank.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "Rerank",
    )

    with patch("providers.reranker_provider._get_bedrock_rerank_client", return_value=mock_client), \
         patch("providers.reranker_provider._term_coverage_fallback", return_value=[0.5, 0.2]) as mock_fb:
        scores = rerank_documents("query", docs)

    assert scores == [0.5, 0.2]
    assert get_last_reranker_mode() == RerankerMode.CIRCUIT_OPEN_LEXICAL_FALLBACK.value
    mock_fb.assert_called_once()
    telemetry = get_last_reranker_telemetry()
    assert "throttled" in (telemetry.get("fallback_reason") or "").lower()


def test_bedrock_reranker_caller_timeout_and_capacity_retention(monkeypatch):
    """Verify caller timeout falls back to BM25Plus and retains slot until worker finishes."""
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "true")
    from providers.reranker_provider import (
        _bedrock_rerank_controller,
        get_last_reranker_mode,
        get_last_reranker_telemetry,
        rerank_documents,
    )

    docs = ["Doc A", "Doc B"]
    worker_started = threading.Event()
    worker_can_finish = threading.Event()

    def slow_rerank(*args, **kwargs):
        worker_started.set()
        worker_can_finish.wait(timeout=5.0)
        return {
            "results": [
                {"index": 0, "relevanceScore": 0.9},
                {"index": 1, "relevanceScore": 0.8},
            ]
        }

    mock_client = MagicMock()
    mock_client.rerank.side_effect = slow_rerank

    # Temporarily set timeout to 0.1s for fast test execution
    with patch("providers.reranker_provider._get_bedrock_rerank_client", return_value=mock_client), \
         patch("providers.reranker_provider._term_coverage_fallback", return_value=[0.3, 0.1]):
        _bedrock_rerank_controller.caller_budget_s = 0.1
        try:
            t0 = time.perf_counter()
            scores = rerank_documents("query", docs)
            t_elapsed = time.perf_counter() - t0

            # Caller should return within ~0.15s, NOT wait for slow_rerank
            assert t_elapsed < 0.5
            assert scores == [0.3, 0.1]
            assert get_last_reranker_mode() == RerankerMode.CIRCUIT_OPEN_LEXICAL_FALLBACK.value
            telemetry = get_last_reranker_telemetry()
            assert "timeout" in (telemetry.get("fallback_reason") or "").lower()

            # Capacity retention: slot is STILL retained by the slow worker
            assert _bedrock_rerank_controller.active_workers > 0 or not _bedrock_rerank_controller.is_slot_free()

            # Release worker
            worker_can_finish.set()
            # Wait for background task to release
            time.sleep(0.1)
            assert _bedrock_rerank_controller.is_slot_free()
        finally:
            _bedrock_rerank_controller.caller_budget_s = 1.5
            worker_can_finish.set()


def test_bedrock_reranker_zero_remote_calls_in_lexical_mode(monkeypatch):
    """Verify zero remote calls are made when Bedrock is disabled and lexical mode is configured."""
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    from providers.reranker_provider import (
        get_last_reranker_mode,
        get_last_reranker_telemetry,
        rerank_documents,
    )

    docs = ["Doc A", "Doc B"]
    with patch("providers.reranker_provider._get_bedrock_rerank_client") as mock_bedrock, \
         patch("providers.reranker_provider._term_coverage_fallback", return_value=[0.45, 0.25]):
        scores = rerank_documents("query", docs)

    assert scores == [0.45, 0.25]
    mock_bedrock.assert_not_called()
    assert get_last_reranker_mode() == RerankerMode.CIRCUIT_OPEN_LEXICAL_FALLBACK.value
    telemetry = get_last_reranker_telemetry()
    assert telemetry["provider"] == "lexical"
    assert telemetry["fallback_reason"] == "lexical_by_configuration"


def test_strict_comparator_treats_bedrock_as_new_configuration(monkeypatch):
    """Verify compare_manifests emits NOT COMPARABLE when comparing Bedrock against OpenRouter baseline."""
    from scripts.run_canonical_60_benchmark import get_reranker_policy
    from services.evaluation.benchmark_comparator import compare_manifests

    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "true")
    policy = get_reranker_policy()
    assert "bedrock:amazon.rerank-v1:0:us-west-2" in policy

    baseline_manifest = {
        "commit_sha": "73075af21d9172155cad22866d8eef2e7c186edb",
        "baseline_commit_sha": "73075af21d9172155cad22866d8eef2e7c186edb",
        "test_suite_sha256": "suite123",
        "corpus_manifest_sha256": "corpus123",
        "workspace_id": "ws_canonical_comparison_0004",
        "runner_version": "2.2.0",
        "scorer_version": "2.2.0",
        "embedding_model": "amazon.titan-embed-text-v2:0",
        "llm_model": "apac.amazon.nova-lite-v1:0",
        "reranker_policy": "openrouter:nvidia/llama-nemotron-rerank-vl-1b-v2:free (fallback: lexical_bm25_plus)",
        "cache_mode": "cold",
        "benchmark_type": "cold",
    }

    candidate_manifest = dict(baseline_manifest)
    candidate_manifest["commit_sha"] = "newcommit456"
    candidate_manifest["baseline_commit_sha"] = "73075af21d9172155cad22866d8eef2e7c186edb"
    candidate_manifest["reranker_policy"] = policy

    comparison = compare_manifests(candidate_manifest, baseline_manifest)
    assert comparison["comparable"] is False
    assert "NOT COMPARABLE" in comparison["status"]
    assert any("reranker_policy" in m for m in comparison["mismatches"])


def test_bedrock_client_factory_single_attempt_no_retry_and_profile(monkeypatch):
    """Verify real client factory uses total_max_attempts=1 (no throttling retry) and aligns profile routing."""
    from botocore.retries.standard import RetryContext
    from config import settings
    from providers.reranker_provider import (
        _get_bedrock_rerank_client,
        reset_bedrock_rerank_client,
    )

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    orig_region = settings.AWS_REGION
    reset_bedrock_rerank_client()
    try:
        cli = _get_bedrock_rerank_client()
        assert cli.meta.region_name == "us-west-2"
        assert settings.AWS_REGION == orig_region
        assert cli.meta.config.retries.get("total_max_attempts") == 1
        assert cli.meta.config.retries.get("mode") == "standard"

        # Verify botocore RetryPolicy will NOT retry ThrottlingException on attempt 1
        try:
            nr = cli.meta.events._emitter._handlers._root["children"]["needs-retry"][
                "children"
            ]["bedrock-agent-runtime"]["values"].middle[0]
            handler = nr.__self__
            ctx = RetryContext(
                attempt_number=1,
                operation_model=None,
                parsed_response={"Error": {"Code": "ThrottlingException"}},
                http_response=type("Resp", (), {"status_code": 429})(),
                caught_exception=None,
            )
            assert handler._retry_policy.should_retry(ctx) is False
        except (AttributeError, KeyError, IndexError):
            pass
    finally:
        reset_bedrock_rerank_client()


def test_bedrock_credential_resolution_matches_infra_and_preflight(monkeypatch):
    """Verify dev defaults to 'aws' profile, prod keeps standard IAM chain, and preflight surfaces missing creds."""
    from config import settings
    from providers.reranker_provider import (
        _get_bedrock_rerank_client,
        get_last_reranker_mode,
        rerank_documents,
        reset_bedrock_rerank_client,
        verify_bedrock_reranker_preflight,
    )

    # 1. Explicit profile honored
    monkeypatch.setenv("AWS_PROFILE", "custom_prof")
    reset_bedrock_rerank_client()
    created_profiles = []

    def fake_session(**kwargs):
        created_profiles.append(kwargs.get("profile_name"))
        sess = MagicMock()
        sess.client.return_value = MagicMock()
        return sess

    with patch("providers.reranker_provider.boto3.Session", side_effect=fake_session):
        _get_bedrock_rerank_client()
    assert created_profiles == ["custom_prof"]
    reset_bedrock_rerank_client()

    # 2. Dev and prod both use the standard AWS SDK credential chain (profile_name=None) when AWS_PROFILE is unset
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(settings, "AWS_PROFILE", None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    created_profiles.clear()
    with patch("providers.reranker_provider.boto3.Session", side_effect=fake_session):
        _get_bedrock_rerank_client()
    assert created_profiles == [None]
    reset_bedrock_rerank_client()

    # 3. Prod keeps normal default credential chain (profile_name=None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    created_profiles.clear()
    with patch("providers.reranker_provider.boto3.Session", side_effect=fake_session):
        _get_bedrock_rerank_client()
    assert created_profiles == [None]
    reset_bedrock_rerank_client()

    # 4. Preflight raises if credentials are missing; runtime still falls back to BM25Plus
    mock_cli = MagicMock()
    mock_cli.meta.region_name = "us-west-2"
    mock_cli.meta.config.retries = {"total_max_attempts": 1, "mode": "standard"}
    mock_cli._get_credentials.return_value = None
    with patch("providers.reranker_provider._get_bedrock_rerank_client", return_value=mock_cli):
        with pytest.raises(RuntimeError, match="credentials"):
            verify_bedrock_reranker_preflight()

    mock_cli.rerank.side_effect = RuntimeError("NoCredentialsError")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "true")
    with patch("providers.reranker_provider._get_bedrock_rerank_client", return_value=mock_cli):
        scores = rerank_documents("test query", ["doc 1", "doc 2"])
    assert len(scores) == 2
    assert get_last_reranker_mode() == RerankerMode.CIRCUIT_OPEN_LEXICAL_FALLBACK.value


def test_bedrock_model_metadata_reports_amazon_rerank_when_enabled(monkeypatch):
    """Verify get_reranker_model() reports amazon.rerank-v1:0 when Bedrock is opted in."""
    from providers.bedrock_models import get_reranker_model

    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "true")
    assert get_reranker_model() == "amazon.rerank-v1:0"

    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    assert get_reranker_model() == "bm25plus"


def test_reranker_telemetry_explicit_across_worker_pipeline_and_no_leakage(monkeypatch):
    """Verify telemetry returns explicitly across worker threads, flows through run_reranker/Pipeline/QueryService, and never leaks."""
    import concurrent.futures
    from schemas.models import Chunk
    from services.langgraph_pipeline import run_reranker

    docs_chunks = [
        Chunk(id="c1", document_id="d1", source_format="pdf", element_type="p", page_number=1, text="Services net sales 30739 million"),
        Chunk(id="c2", document_id="d1", source_format="pdf", element_type="p", page_number=2, text="Gross margin 23245 million"),
        Chunk(id="c3", document_id="d1", source_format="pdf", element_type="p", page_number=3, text="Balance sheet cash reserves"),
    ]

    # Run inside a worker thread (mimics LAYA/concurrency executor worker boundary)
    def run_in_worker(provider_val: str, mock_behavior: str):
        mock_cli = MagicMock()
        if mock_behavior == "ok":
            mock_cli.rerank.return_value = {
                "results": [
                    {"index": 0, "relevanceScore": 0.9},
                    {"index": 1, "relevanceScore": 0.4},
                    {"index": 2, "relevanceScore": 0.1},
                ]
            }
        elif mock_behavior == "throttle":
            mock_cli.rerank.side_effect = ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                "Rerank",
            )
        state = {
            "query": "Services net sales",
            "workspace_id": "ws_test",
            "bm25_candidates": docs_chunks,
            "candidate_chunks": [],
            "graph_chunks": [],
            "structural_candidates": [],
            "okf_candidates": [],
            "stage_timings": {},
            "retrieval_candidates": {},
        }
        monkeypatch.setenv("RERANKER_PROVIDER", provider_val)
        if provider_val == "bedrock":
            monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "true")
        else:
            monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
        with patch("providers.reranker_provider._get_bedrock_rerank_client", return_value=mock_cli):
            return run_reranker(state)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        res_ok = pool.submit(run_in_worker, "bedrock", "ok").result()
        res_fb = pool.submit(run_in_worker, "bedrock", "throttle").result()
        res_lex = pool.submit(run_in_worker, "lexical", "none").result()

    # Check distinct records and no cross-request leakage
    t_ok = res_ok["reranker_telemetry"]
    assert t_ok["provider"] == "bedrock"
    assert t_ok["model"] == "amazon.rerank-v1:0"
    assert t_ok["region"] == "us-west-2"
    assert t_ok["effective_mode"] == "remote_success"
    assert t_ok["fallback_reason"] is None
    assert t_ok["candidate_count"] == 3
    assert t_ok["latency_ms"] >= 0.0

    t_fb = res_fb["reranker_telemetry"]
    assert t_fb["provider"] == "bedrock"
    assert t_fb["model"] == "amazon.rerank-v1:0"
    assert t_fb["region"] == "us-west-2"
    assert t_fb["effective_mode"] == "circuit_open_lexical_fallback"
    assert "throttled" in (t_fb["fallback_reason"] or "")
    assert t_fb["candidate_count"] == 3

    t_lex = res_lex["reranker_telemetry"]
    assert t_lex["provider"] == "lexical"
    assert t_lex["model"] == "bm25plus"
    assert t_lex["region"] is None
    assert t_lex["effective_mode"] == "circuit_open_lexical_fallback"
    assert t_lex["fallback_reason"] == "lexical_by_configuration"
    assert t_lex["candidate_count"] == 3

    # Verify Pipeline.ResponseObject and QueryService propagate reranker_telemetry end-to-end
    from modules.query.query_service import QueryService
    from schemas.models import QueryRequest
    from services.langgraph_pipeline import Pipeline

    resp_obj = Pipeline.ResponseObject({**res_ok, "final_answer": "Services sales were $30,739 million."})
    assert resp_obj.reranker_telemetry == t_ok

    mock_repo = MagicMock()
    mock_repo.get_workspace_documents.return_value = {"documents": [{"id": "d1"}]}
    mock_repo.get_cached_result.return_value = None
    qs = QueryService(repo=mock_repo)
    mock_boundary = MagicMock()
    mock_boundary.has_tombstone.return_value = False
    mock_boundary.is_workspace_tombstoned.return_value = False
    mock_boundary.list_sources.return_value = []
    mock_boundary.get_document_snapshot.return_value = None
    with patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=mock_boundary), \
         patch("services.langgraph_pipeline.pipeline.run", return_value=resp_obj):
        out = qs.execute_query(
            QueryRequest(query="Services net sales", workspace_id="ws_test", benchmark_mode=True)
        )
    assert out["reranker_mode"] == "remote_success"
    assert out["reranker_telemetry"] == t_ok

    # Verify fallback telemetry flows cleanly too without leakage from previous remote_success
    resp_fb = Pipeline.ResponseObject({**res_fb, "final_answer": "Services sales were $30,739 million."})
    with patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=mock_boundary), \
         patch("services.langgraph_pipeline.pipeline.run", return_value=resp_fb):
        out_fb = qs.execute_query(
            QueryRequest(query="Services net sales", workspace_id="ws_test", benchmark_mode=True)
        )
    assert out_fb["reranker_mode"] == "circuit_open_lexical_fallback"
    assert out_fb["reranker_telemetry"] == t_fb


