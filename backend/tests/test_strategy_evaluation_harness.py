"""Tests for the independent strategy evaluation harness."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.retrieval.evidence_contract import EvidenceItem, RetrievalStrategyResult


def test_harness_does_not_call_answer_generation():
    """Requirement 11: Strategy evaluation harness does not call final answer generation."""
    from scripts.evaluate_retrieval_strategies import evaluate_question_on_strategy

    mock_strategy = MagicMock()
    mock_strategy.name = "mock_strat"

    item = EvidenceItem(
        evidence_id="c1",
        workspace_id="ws_test",
        document_id="d1",
        document_version=None,
        source_type="chunk",
        locator={"chunk_id": "c1"},
        text="Sample",
        structured_payload=None,
        score=0.9,
        strategy="mock_strat",
        provenance={},
        citation_payload={"chunk_id": "c1", "document_filename": "d1.pdf"},
    )

    mock_result = RetrievalStrategyResult(
        strategy_name="mock_strat",
        query="test query",
        items=[item],
        confidence=0.9,
        latency_ms=10.0,
        remote_call_counts={"bedrock_embedding_calls": 0, "generation_calls": 0},
        failure_reason=None,
        debug_trace={},
    )
    mock_strategy.retrieve = AsyncMock(return_value=mock_result)

    with patch("services.llm.llm_service.call") as mock_llm_call:
        res = pytest.importorskip("asyncio").run(
            evaluate_question_on_strategy(
                question_id="Q101",
                question_text="test query",
                strategy=mock_strategy,
                workspace_id="ws_test",
                gt_evidence_ids={"c1"},
                is_no_evidence_question=False,
            )
        )

        # Ensure no answer generation was invoked
        mock_llm_call.assert_not_called()
        assert res["strategy"] == "mock_strat"
        assert res["question_id"] == "Q101"
        assert res["recall_at_5"] == 1.0
        assert res["precision_at_3"] == 1.0
        assert res["mrr_at_5"] == 1.0


def test_harness_excludes_no_evidence_questions_from_recall_denominator():
    """No-evidence questions must be evaluated separately and excluded from recall denominators."""
    from scripts.evaluate_retrieval_strategies import evaluate_question_on_strategy

    mock_strategy = MagicMock()
    mock_strategy.name = "mock_strat"
    mock_result = RetrievalStrategyResult(
        strategy_name="mock_strat",
        query="What is the color of the invisible pink unicorn?",
        items=[],
        confidence=0.0,
        latency_ms=5.0,
        remote_call_counts={},
        failure_reason="no_candidates",
        debug_trace={},
    )
    mock_strategy.retrieve = AsyncMock(return_value=mock_result)

    res = pytest.importorskip("asyncio").run(
        evaluate_question_on_strategy(
            question_id="Q_REFUSAL",
            question_text="What is the color of the invisible pink unicorn?",
            strategy=mock_strategy,
            workspace_id="ws_test",
            gt_evidence_ids=set(),
            is_no_evidence_question=True,
        )
    )

    # Must be None for recall/precision/mrr denominators
    assert res["recall_at_5"] is None
    assert res["precision_at_3"] is None
    assert res["mrr_at_5"] is None
    assert res["refusal_candidate_detected"] is True
    assert res["unsupported_evidence_returned"] is False
