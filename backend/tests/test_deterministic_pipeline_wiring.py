from decimal import Decimal
from unittest.mock import patch

import pytest

from db.database import CloudRepository
from ingestion.embed_service import _deterministic_vector, embed_fast_local
from schemas.models import Chunk, Document
from services.langgraph_pipeline import pipeline
from services.retrieval.fidelity_check import CoverageError
# Resume with -c (or command below):
# agy --conversation=57d4b898-2446-4ca6-bb44-03007a8c7994

def _setup_test_doc(repo: CloudRepository, workspace_id: str, doc_id: str, text: str):
    doc = Document(
        id=doc_id,
        filename=f"{doc_id}.txt",
        source_format="pdf",
        chunks=(
            Chunk(
                id=f"{doc_id}:c1",
                document_id=doc_id,
                text=text,
                source_format="pdf",
                page_number=1,
                element_type="table",
                section_path=("Balance Sheet",),
                embedding_fast=embed_fast_local(text),
                embedding_full=_deterministic_vector(text, 1024),
                workspace_id=workspace_id,
            ),
        ),
        workspace_id=workspace_id,
    )
    repo.save(doc)


def test_deterministic_arithmetic_execution_in_pipeline(monkeypatch):
    """Verify that arithmetic queries deterministically execute exact Decimal math
    with 0 LLM tokens, full operand provenance, and exact results."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("ENABLE_DETERMINISTIC_MATH", "1")

    repo = CloudRepository()
    ws_id = "ws_arithmetic_test"
    balance_sheet_text = (
        "Consolidated Balance Sheets as of June 27, 2026:\n"
        "Total current assets: $149,818\n"
        "Total current liabilities: $149,326\n"
        "Total liabilities: $250,000"
    )
    _setup_test_doc(repo, ws_id, "doc_balance_sheet", balance_sheet_text)

    query = "Using the June 27, 2026 balance sheet, calculate current assets minus current liabilities."
    response = pipeline.run(query, workspace_id=ws_id)

    # Deterministic math must execute: 149818 - 149326 = 492
    assert response.execution_result is not None, "execution_result should be populated"
    assert response.execution_result.result_value == Decimal("492")
    assert "492" in response.answer
    assert "149818 minus 149326" in response.answer

    # Zero LLM token consumption
    assert response.usage == {"input_tokens": 0, "output_tokens": 0}
    assert response.confidence_score >= 0.8
    assert response.faithfulness == 1.0

    # Provenance citation check
    assert len(response.citations) > 0
    assert response.citations[0]["chunk_id"] == "doc_balance_sheet:c1"
    assert "deterministic_math_ms" in response.stage_timings


def test_deterministic_arithmetic_decoupled_from_fidelity_rejection(monkeypatch):
    """Critical decoupling test: deterministic math runs immediately after reranker.
    Even if fidelity gate would raise CoverageError (simulating semantic drift / over-filtering),
    deterministic math completes successfully without invoking run_fidelity or failing.
    """
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("ENABLE_DETERMINISTIC_MATH", "1")

    repo = CloudRepository()
    ws_id = "ws_fidelity_decouple_test"
    stocks_text = (
        "Variable_name: Closing stocks: 109848\n"
        "Variable_name: Opening stocks: 108327"
    )
    _setup_test_doc(repo, ws_id, "doc_stocks", stocks_text)

    query = "In the 2025 All industries rows, calculate closing stocks minus opening stocks."

    # If run_fidelity were called, it would raise CoverageError
    with patch(
        "services.retrieval.fidelity_check.check_fidelity",
        side_effect=CoverageError("Simulated fidelity gate rejection on semantic drift"),
    ) as mock_fidelity:
        response = pipeline.run(query, workspace_id=ws_id)

        # check_fidelity was NOT called because deterministic math bypassed compressor/fidelity
        mock_fidelity.assert_not_called()

        assert response.execution_result is not None
        assert response.execution_result.result_value == Decimal("1521")
        assert "1521" in response.answer
        assert response.usage == {"input_tokens": 0, "output_tokens": 0}


def test_fallback_to_llm_when_non_mathematical(monkeypatch):
    """Verify that non-mathematical queries fall back cleanly through compressor,
    fidelity, and LLM generation."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("ENABLE_DETERMINISTIC_MATH", "1")

    repo = CloudRepository()
    ws_id = "ws_fallback_test"
    text = "The company reported steady revenue growth across all geographic sectors."
    _setup_test_doc(repo, ws_id, "doc_narrative", text)

    query = "What did the company report regarding revenue growth?"

    mock_llm_response = {
        "answer": "Steady revenue growth across all geographic sectors.",
        "citations": ["doc_narrative:c1"],
        "usage": {"input_tokens": 42, "output_tokens": 12},
    }

    with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response) as mock_call:
        response = pipeline.run(query, workspace_id=ws_id)

        mock_call.assert_called_once()
        assert response.execution_result is None
        assert response.answer == "Steady revenue growth across all geographic sectors."
        assert response.usage["input_tokens"] == 42


def test_gating_disables_deterministic_math(monkeypatch):
    """Verify that ENABLE_DETERMINISTIC_MATH=0 disables deterministic execution
    and routes directly to compressor -> fidelity -> LLM."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("ENABLE_DETERMINISTIC_MATH", "0")

    repo = CloudRepository()
    ws_id = "ws_gating_test"
    text = "Total current assets: $149,818\nTotal current liabilities: $149,326"
    _setup_test_doc(repo, ws_id, "doc_gate", text)

    query = "Calculate current assets minus current liabilities"

    mock_llm_response = {
        "answer": "492 from LLM fallback",
        "citations": ["doc_gate:c1"],
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }

    with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response) as mock_call:
        response = pipeline.run(query, workspace_id=ws_id)

        mock_call.assert_called_once()
        assert response.execution_result is None
        assert response.answer == "492 from LLM fallback"
