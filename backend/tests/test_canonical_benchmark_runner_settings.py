"""Offline regression tests for canonical 60 benchmark runner settings lifecycle.

Verifies:
1. settings is imported at module scope in scripts.run_canonical_60_benchmark.
2. run_benchmark overrides PAGEINDEX_ENABLED and OKF_ENABLED when flags are provided.
3. settings are restored to original values after successful execution.
4. settings are restored to original values after failure / exception during execution.
5. settings remain unmodified when page_index and okf flags are None.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from config import settings
from scripts.run_canonical_60_benchmark import (
    get_reranker_policy,
    run_benchmark,
    settings as runner_settings,
)


def test_settings_imported_at_module_level():
    """Verify settings is available at module level in runner and matches global config."""
    assert runner_settings is not None
    assert runner_settings is settings
    # Ensure get_reranker_policy executes without NameError
    policy = get_reranker_policy()
    assert isinstance(policy, str)
    assert len(policy) > 0


def test_run_benchmark_restores_settings_on_failure():
    """Verify settings are restored to original values if run_benchmark encounters an exception."""
    # Setup initial state
    settings.PAGEINDEX_ENABLED = True
    settings.OKF_ENABLED = True

    captured_during_run = {}

    def mock_verify_live_providers():
        captured_during_run["page_index"] = settings.PAGEINDEX_ENABLED
        captured_during_run["okf"] = settings.OKF_ENABLED
        raise RuntimeError("Simulated failure during live provider check")

    with patch("scripts.run_canonical_60_benchmark.verify_live_providers", side_effect=mock_verify_live_providers):
        with pytest.raises(RuntimeError, match="Simulated failure during live provider check"):
            run_benchmark(
                workspace_id="ws_test_failure",
                allow_dirty=True,
                page_index=False,
                okf=False,
            )

    # Verify that inside the execution, settings were overridden
    assert captured_during_run.get("page_index") is False
    assert captured_during_run.get("okf") is False

    # Verify that after failure, settings were restored to original values
    assert settings.PAGEINDEX_ENABLED is True
    assert settings.OKF_ENABLED is True


def test_run_benchmark_restores_settings_on_success(tmp_path: Path):
    """Verify settings are restored to original values after successful run_benchmark execution."""
    # Setup initial state
    settings.PAGEINDEX_ENABLED = True
    settings.OKF_ENABLED = True

    # Create dummy test suite file
    dummy_suite = {
        "test_suite_name": "dummy_offline_test",
        "documents": [{"doc_id": "doc1", "filename": "doc1.pdf"}],
        "questions": [
            {
                "id": "q1",
                "category": "factual",
                "question": "What is the result?",
                "expected_answer": "42",
            }
        ],
    }
    suite_file = tmp_path / "test_suite.json"
    suite_file.write_text(json.dumps(dummy_suite), encoding="utf-8")

    # Create dummy ground truth file
    dummy_gt = {
        "q1": {
            "question_type": "retrieval",
            "relevant_evidence": [],
            "answer_contract": {"contract_type": "semantic"},
        }
    }
    gt_file = tmp_path / "ground_truth.json"
    gt_file.write_text(json.dumps(dummy_gt), encoding="utf-8")

    captured_during_query = {}

    mock_service_instance = MagicMock()

    def mock_execute_query(req):
        captured_during_query["page_index"] = settings.PAGEINDEX_ENABLED
        captured_during_query["okf"] = settings.OKF_ENABLED
        return {
            "answer": "42",
            "cached": False,
            "status": "success",
            "citations": [],
            "retrieval_candidates": {"reranked": [], "compressed_context": [], "llm_citations": []},
            "reranked_chunk_ids": [],
            "compressed_context_chunk_ids": [],
            "llm_cited_chunk_ids": [],
            "final_citation_chunk_ids": [],
            "executed_path": "fast",
            "planned_path": "fast",
            "reranker_mode": "remote_success",
            "bedrock_embedding_calls": 0,
            "bge_lambda_calls": 1,
            "reranker_remote_calls": 0,
            "generation_calls": 0,
        }

    mock_service_instance.execute_query.side_effect = mock_execute_query

    mock_repo_instance = MagicMock()
    mock_repo_instance.get_all_chunks.return_value = [
        MagicMock(chunk_id="c1", doc_id="doc1", text="sample text")
    ]

    with patch("scripts.run_canonical_60_benchmark.verify_live_providers"), \
         patch("db.table_store.get_shared_table_store", return_value=MagicMock()), \
         patch("scripts.run_canonical_60_benchmark.compute_corpus_manifest_hash", return_value=("hash123", {"doc1.pdf": "h1"})), \
         patch("scripts.run_canonical_60_benchmark.QueryRepository", return_value=mock_repo_instance), \
         patch("scripts.run_canonical_60_benchmark.QueryService", return_value=mock_service_instance), \
         patch("scripts.run_canonical_60_benchmark.GROUND_TRUTH_FILE", gt_file), \
         patch("scripts.run_canonical_60_benchmark.ROOT_DIR", tmp_path), \
         patch("scripts.run_canonical_60_benchmark.BACKEND_DIR", tmp_path):

        manifest = run_benchmark(
            workspace_id="ws_test_success",
            allow_dirty=True,
            test_path="test_suite.json",
            page_index=False,
            okf=False,
        )

    # Verify settings during query execution were overridden
    assert captured_during_query.get("page_index") is False
    assert captured_during_query.get("okf") is False

    # Verify manifest recorded the overridden strategies
    assert manifest["strategies"]["page_index_enabled"] is False
    assert manifest["strategies"]["okf_enabled"] is False

    # Verify that after return, settings were restored to original values
    assert settings.PAGEINDEX_ENABLED is True
    assert settings.OKF_ENABLED is True


def test_run_benchmark_unmodified_when_flags_none(tmp_path: Path):
    """Verify settings remain unmodified when page_index and okf flags are None."""
    settings.PAGEINDEX_ENABLED = True
    settings.OKF_ENABLED = True

    captured = {}

    def mock_verify_live_providers():
        captured["page_index"] = settings.PAGEINDEX_ENABLED
        captured["okf"] = settings.OKF_ENABLED
        raise RuntimeError("Stop early after checking settings")

    with patch("scripts.run_canonical_60_benchmark.verify_live_providers", side_effect=mock_verify_live_providers):
        with pytest.raises(RuntimeError, match="Stop early after checking settings"):
            run_benchmark(
                workspace_id="ws_test_none",
                allow_dirty=True,
                page_index=None,
                okf=None,
            )

    assert captured.get("page_index") is True
    assert captured.get("okf") is True
    assert settings.PAGEINDEX_ENABLED is True
    assert settings.OKF_ENABLED is True
