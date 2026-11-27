"""Offline tests for the mixed-corpus 180-question evaluator."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from services.evaluation.mixed_180_evaluator import (
    grade_180_record,
    load_engine_requests,
    resolve_180_locators,
    validate_180_dataset,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
STAGED_DIR = Path("/home/swyra/.gemini/antigravity-cli/brain/338cc016-3147-4604-897e-08680acdf711/scratch/staged_17_docs")


def test_input_isolation_has_no_gold_or_hints():
    reqs = load_engine_requests(ROOT_DIR / "data" / "kre_questions_only_180.json")
    assert len(reqs) == 180
    for r in reqs:
        assert set(r.keys()) == {"id", "question"}


def test_validate_180_dataset_and_staged_hashes():
    summary = validate_180_dataset(
        ROOT_DIR / "data" / "kre_questions_only_180.json",
        ROOT_DIR / "data" / "kre_benchmark_180.json",
        ROOT_DIR / "data" / "kre_shuffled_documents.json",
        staged_dir=STAGED_DIR if STAGED_DIR.exists() else None,
    )
    assert summary["question_count"] == 180
    assert summary["document_count"] == 17


def test_locator_resolution_all_types_and_version_isolation():
    snaps = {
        "paper.pdf": SimpleNamespace(document_id="doc_pdf", version="v_cur"),
        "deck.pptx": SimpleNamespace(document_id="doc_pptx", version="v_cur"),
        "manual.docx": SimpleNamespace(document_id="doc_docx", version="v_cur"),
        "sheet.xls": SimpleNamespace(document_id="doc_xls", version="v_cur", sheet_inventory=SimpleNamespace(complete=True)),
        "table.csv": SimpleNamespace(document_id="doc_csv", version="v_cur"),
    }
    chunks = [
        SimpleNamespace(id="doc_pdf:page:3:element:0", document_id="doc_pdf", document_version="v_cur", page_number=3, location_reference="Page: 3"),
        SimpleNamespace(id="doc_pdf:page:3:stale", document_id="doc_pdf", document_version="v_old", page_number=3, location_reference="Page: 3"),
        SimpleNamespace(id="doc_pptx:s12", document_id="doc_pptx", document_version="v_cur", page_number=12, location_reference="Slide 12"),
        SimpleNamespace(id="doc_docx:p:0", document_id="doc_docx", document_version="v_cur", page_number=1, location_reference="Paragraph: 1"),
        SimpleNamespace(id="doc_xls:xls:Sheet1:row1", document_id="doc_xls", document_version="v_cur", page_number=None, location_reference="Sheet1 Row 1"),
    ]

    r_pdf = resolve_180_locators([{"document": "paper.pdf", "locator": {"page": 3}}], chunks, snaps)
    assert r_pdf["resolved_chunk_ids"] == {"doc_pdf:page:3:element:0"}  # excludes v_old

    r_slide = resolve_180_locators([{"document": "deck.pptx", "locator": {"slide": 12}}], chunks, snaps)
    assert r_slide["resolved_chunk_ids"] == {"doc_pptx:s12"}

    r_docx = resolve_180_locators([{"document": "manual.docx", "locator": {"document_section_page": 1}}], chunks, snaps)
    assert r_docx["resolved_chunk_ids"] == {"doc_docx:p:0"}

    r_cell = resolve_180_locators([{"document": "sheet.xls", "locator": {"sheet": "Sheet1", "cell": "C2"}}], chunks, snaps)
    assert r_cell["resolved_chunk_ids"] == {"doc_xls:xls:Sheet1:row1"}

    r_inv = resolve_180_locators([{"document": "sheet.xls", "locator": {"operation": "sheet_inventory"}}], chunks, snaps)
    assert r_inv["resolved_chunk_ids"] == {"doc_xls:sheet_inventory:v_cur"}

    r_struct = resolve_180_locators([{"document": "table.csv", "locator": {"filter": {"Year": 2025}, "column": "Value"}}], chunks, snaps)
    assert len(r_struct["structured_targets"]) == 1

    r_unsup = resolve_180_locators([{"document": "paper.pdf", "locator": {"unknown_key": 99}}], chunks, snaps)
    assert r_unsup["unscorable_reasons"] and "unsupported_locator_type" in r_unsup["unscorable_reasons"][0]


def test_grade_180_states_and_empty_selection():
    resolved_ok = {"resolved_chunk_ids": {"c1"}, "resolved_documents": {"doc.pdf"}, "structured_targets": [], "unscorable_reasons": []}
    g_comp = grade_180_record(
        {"id": "Q1", "expected_state": "COMPLETE", "preferred_answer": "450 million"},
        {"answer": "Revenue was $450 million.", "citations": [{"chunk_id": "c1", "document_filename": "doc.pdf"}], "executed_path": "full"},
        resolved_ok,
    )
    assert g_comp["grounded_correct"] is True

    g_part = grade_180_record(
        {"id": "Q2", "expected_state": "PARTIAL", "preferred_answer": "2024 is 100"},
        {"answer": "2024 is 100; 2026 is not available.", "citations": [{"chunk_id": "c1", "document_filename": "doc.pdf"}], "executed_path": "full"},
        resolved_ok,
    )
    assert g_part["grounded_correct"] is True

    g_clar = grade_180_record(
        {"id": "Q3", "expected_state": "CLARIFICATION_REQUIRED", "preferred_answer": "Please clarify which year."},
        {"answer": "Could you clarify which year you mean?", "citations": []},
        {"resolved_chunk_ids": set(), "resolved_documents": set(), "structured_targets": [], "unscorable_reasons": []},
    )
    assert g_clar["grounded_correct"] is True

    g_nosup = grade_180_record(
        {"id": "Q4", "expected_state": "NO_SUPPORT", "preferred_answer": "Not found"},
        {"answer": "I couldn't find any relevant passages.", "citations": []},
        {"resolved_chunk_ids": set(), "resolved_documents": set(), "structured_targets": [], "unscorable_reasons": []},
    )
    assert g_nosup["grounded_correct"] is True

    g_empty = grade_180_record(
        {"id": "Q5", "expected_state": "COMPLETE", "preferred_answer": "450"},
        {"answer": "450", "citations": [], "executed_path": "full"},
        resolved_ok,
    )
    assert g_empty["grounded_correct"] is False

    g_unscorable = grade_180_record(
        {"id": "Q6", "expected_state": "COMPLETE", "preferred_answer": "450"},
        {"answer": "450", "citations": []},
        {"resolved_chunk_ids": set(), "resolved_documents": set(), "structured_targets": [], "unscorable_reasons": ["unsupported_locator_type:doc.pdf:['foo']"]},
    )
    assert g_unscorable["scorable"] is False
