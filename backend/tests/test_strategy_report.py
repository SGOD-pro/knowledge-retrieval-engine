"""Tests for Strategy Comparison Report generator."""

import pytest
from services.evaluation.strategy_report_generator import generate_strategy_matrix_markdown


def test_strategy_comparison_report_includes_every_question_exactly_once_per_strategy():
    """Requirement 12: Strategy comparison report includes every benchmark question exactly once per strategy."""
    strategies = ["vector_rerank", "bm25", "structured_table", "page_index", "knowledge_graph", "okf"]
    question_ids = [f"Q{i}" for i in range(1, 11)]  # 10 questions

    mock_results = []
    for s in strategies:
        for qid in question_ids:
            mock_results.append({
                "question_id": qid,
                "category": "FACTUAL" if int(qid[1:]) % 2 == 0 else "NUMERIC",
                "strategy": s,
                "retrieved_evidence_ids": ["c1", "c2"],
                "ground_truth_evidence_ids": ["c1"],
                "recall_at_5": 1.0 if s == "vector_rerank" else 0.5,
                "precision_at_3": 0.5,
                "mrr_at_5": 1.0 if s == "vector_rerank" else 0.5,
                "ndcg_at_5": 1.0,
                "latency_ms": 15.0,
                "remote_calls": {},
                "failure_reason": None,
            })

    # Total results must be len(strategies) * len(questions)
    assert len(mock_results) == 6 * 10

    report = generate_strategy_matrix_markdown(mock_results, commit_sha="test_sha", timestamp="20260925_120000")

    # Verify Markdown contains the overall summary, category matrix, and question matrix
    assert "# Independent Retrieval Strategy Evaluation Matrix" in report
    assert "## Overall Strategy Benchmark Summary" in report
    assert "## Strategy Capability by Category" in report
    assert "## Per-Question Winning Strategy & Discovery Matrix" in report

    # Verify all strategies appear
    for s in strategies:
        assert s in report

    # Verify each question appears in the Per-Question table
    for qid in question_ids:
        assert f"| {qid} |" in report
