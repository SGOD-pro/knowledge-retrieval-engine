"""Reproduction tests for Mixed-180 benchmark integrity, grader defects, and generic dataset routing."""

import pytest
from schemas.models import Chunk
from services.evaluation.mixed_180_evaluator import (
    grade_180_record,
    resolve_180_locators,
)
from db.workspace_boundary_repo import DocumentSnapshot


def test_grader_rejects_wrong_sign_and_negated_values():
    """Grader must reject wrong signs and negated values."""
    bench_item = {
        "id": "KRE180-Q150",
        "expected_state": "COMPLETE",
        "preferred_answer": "-4,191 (-0.43%).",
        "expected_sources": [{"document": "data.csv", "locator": {"operation": "delta"}}],
    }
    resolved = {
        "resolved_chunk_ids": set(),
        "resolved_documents": {"data.csv"},
        "structured_targets": [{"document": "data.csv"}],
        "structured_documents": {"data.csv"},
        "unscorable_reasons": [],
    }

    # Wrong sign: positive increase instead of negative drop
    resp_positive = {
        "answer": "The total changed by 4,191, an increase of 0.43%.",
        "executed_path": "structured_aggregate",
        "citations": [{"chunk_id": "c1", "document_filename": "data.csv"}],
    }
    res = grade_180_record(bench_item, resp_positive, resolved)
    assert not res["answer_correct"], "Grader must reject positive value when negative value was expected"
    assert not res["grounded_correct"]

    # Positive control: correct negative sign passes
    resp_correct = {
        "answer": "-4,191 (-0.43%).",
        "executed_path": "structured_aggregate",
        "citations": [{"chunk_id": "c1", "document_filename": "data.csv"}],
    }
    res_ok = grade_180_record(bench_item, resp_correct, resolved)
    assert res_ok["answer_correct"]
    assert res_ok["grounded_correct"]


def test_grader_rejects_missing_structured_lineage():
    """Structured aggregate execution must provide structured lineage citations; empty citations must fail."""
    bench_item = {
        "id": "KRE180-Q150",
        "expected_state": "COMPLETE",
        "preferred_answer": "-4,191 (-0.43%).",
        "expected_sources": [{"document": "data.csv", "locator": {"operation": "delta"}}],
    }
    resolved = {
        "resolved_chunk_ids": set(),
        "resolved_documents": {"data.csv"},
        "structured_targets": [{"document": "data.csv"}],
        "structured_documents": {"data.csv"},
        "unscorable_reasons": [],
    }
    resp_no_citations = {
        "answer": "-4,191 (-0.43%).",
        "executed_path": "structured_aggregate",
        "citations": [],  # Missing lineage
    }
    res = grade_180_record(bench_item, resp_no_citations, resolved)
    assert not res["grounded_correct"], "Structured aggregate with empty citations must fail lineage verification"


def test_grader_rejects_slide_12_for_slide_1():
    """Resolving locator for slide 1 must never match slide 12."""
    snap = DocumentSnapshot(
        workspace_id="ws_test",
        document_id="doc_pptx",
        source="deck.pptx",
        version="v1",
        format="pptx",
        created_at="2026-01-01T00:00:00Z",
    )
    # Chunk is from slide 12
    chunk_slide_12 = Chunk(
        id="doc_pptx:slide:12",
        document_id="doc_pptx",
        source_format="pptx",
        text="Content on slide twelve",
        element_type="text",
        page_number=12,
        document_version="v1",
        workspace_id="ws_test",
    )

    resolved = resolve_180_locators(
        expected_sources=[{"document": "deck.pptx", "locator": {"slide": 1}}],
        chunks=[chunk_slide_12],
        snapshot_by_filename={"deck.pptx": snap},
    )
    assert "doc_pptx:slide:12" not in resolved["resolved_chunk_ids"], (
        "Locator for slide 1 must not resolve slide 12"
    )


def test_grader_requires_real_clarification_not_refusal():
    """CLARIFICATION_REQUIRED must reject simple refusal and require genuine clarification request."""
    bench_item = {
        "id": "KRE180-Q099",
        "expected_state": "CLARIFICATION_REQUIRED",
        "preferred_answer": "Please clarify which department or year you need.",
        "expected_sources": [],
    }
    resolved = {
        "resolved_chunk_ids": set(),
        "resolved_documents": set(),
        "structured_targets": [],
        "structured_documents": set(),
        "unscorable_reasons": [],
    }

    # Plain refusal should NOT pass as clarification
    resp_refusal = {
        "answer": "Could not find any relevant information in the documents.",
        "executed_path": "fast",
        "citations": [],
    }
    res_ref = grade_180_record(bench_item, resp_refusal, resolved)
    assert not res_ref["answer_correct"], "Refusal must not satisfy CLARIFICATION_REQUIRED"

    # Real clarification passes
    resp_clarification = {
        "answer": "Please specify whether you are asking about 2024 or 2025 data.",
        "executed_path": "structured_aggregate_ambiguous",
        "citations": [],
    }
    res_clar = grade_180_record(bench_item, resp_clarification, resolved)
    assert res_clar["answer_correct"]
    assert res_clar["grounded_correct"]


def test_grader_requires_explicit_limitation_for_partial():
    """PARTIAL expected state requires an explicit limitation or gap statement."""
    bench_item = {
        "id": "KRE180-Q100",
        "expected_state": "PARTIAL",
        "preferred_answer": "Data is available for 2020-2022 only; 2023 is missing.",
        "expected_sources": [{"document": "data.pdf", "locator": {"page": 2}}],
    }
    resolved = {
        "resolved_chunk_ids": {"c2"},
        "resolved_documents": {"data.pdf"},
        "per_doc_chunk_ids": {"data.pdf": {"c2"}},
        "structured_targets": [],
        "structured_documents": set(),
        "unscorable_reasons": [],
    }

    # Answer without limitation fails
    resp_unlimited = {
        "answer": "The reported value is 42.",
        "executed_path": "fast",
        "citations": [{"chunk_id": "c2", "document_filename": "data.pdf"}],
    }
    res_fail = grade_180_record(bench_item, resp_unlimited, resolved)
    assert not res_fail["grounded_correct"], "PARTIAL state requires explicit limitation statement"

    # Answer with explicit limitation passes
    resp_limited = {
        "answer": "The value is 42, but data is only available for 2020-2022 and 2023 is missing.",
        "executed_path": "fast",
        "citations": [{"chunk_id": "c2", "document_filename": "data.pdf"}],
    }
    res_ok = grade_180_record(bench_item, resp_limited, resolved)
    assert res_ok["grounded_correct"]


def test_grader_rejects_wrong_rows_or_fields():
    """Grader must reject citations that cite the wrong column/field or wrong row."""
    bench_item = {
        "id": "KRE180-Q120",
        "expected_state": "COMPLETE",
        "preferred_answer": "42",
        "expected_sources": [
            {
                "document": "survay.csv",
                "locator": {"column": "Value", "filter": {"Year": 2025}},
            }
        ],
    }
    resolved = {
        "resolved_chunk_ids": set(),
        "resolved_documents": {"survay.csv"},
        "structured_targets": [
            {
                "document": "survay.csv",
                "document_id": "doc_survay",
                "locator": {"column": "Value", "filter": {"Year": 2025}},
            }
        ],
        "structured_documents": {"survay.csv"},
        "unscorable_reasons": [],
    }

    # Wrong column cited
    resp_wrong_field = {
        "answer": "42",
        "executed_path": "structured_aggregate",
        "citations": [
            {
                "chunk_id": "c_wrong",
                "document_filename": "survay.csv",
                "column": "WrongColumn",
            }
        ],
    }
    res_field = grade_180_record(bench_item, resp_wrong_field, resolved)
    assert not res_field["grounded_correct"], "Must reject citation with wrong column"

    # Correct column passes
    resp_correct = {
        "answer": "42",
        "executed_path": "structured_aggregate",
        "citations": [
            {
                "chunk_id": "c_ok",
                "document_filename": "survay.csv",
                "column": "Value",
            }
        ],
    }
    res_ok = grade_180_record(bench_item, resp_correct, resolved)
    assert res_ok["grounded_correct"], "Must pass citation with matching column"


def test_grader_slide_1_positive_control():
    """Slide 1 locator correctly resolves slide 1 chunks and rejects other slides."""
    snap = DocumentSnapshot(
        workspace_id="ws_test",
        document_id="doc_pptx",
        source="deck.pptx",
        version="v1",
        format="pptx",
        created_at="2026-01-01T00:00:00Z",
    )
    chunk_slide_1 = Chunk(
        id="doc_pptx:slide:1",
        document_id="doc_pptx",
        source_format="pptx",
        text="Content on slide one",
        element_type="text",
        page_number=1,
        document_version="v1",
        workspace_id="ws_test",
    )
    chunk_slide_12 = Chunk(
        id="doc_pptx:slide:12",
        document_id="doc_pptx",
        source_format="pptx",
        text="Content on slide twelve",
        element_type="text",
        page_number=12,
        document_version="v1",
        workspace_id="ws_test",
    )

    resolved = resolve_180_locators(
        expected_sources=[{"document": "deck.pptx", "locator": {"slide": 1}}],
        chunks=[chunk_slide_1, chunk_slide_12],
        snapshot_by_filename={"deck.pptx": snap},
    )
    assert "doc_pptx:slide:1" in resolved["resolved_chunk_ids"]
    assert "doc_pptx:slide:12" not in resolved["resolved_chunk_ids"]

