"""Focused regressions for canonical grading of concise percentage answers.

Proves:
  1. Reproduction of Q149 through the canonical grading path accepting '99.58%' as correct answer text.
  2. Negative controls for wrong value, wrong unit, negated value, and omission of other required claims.
  3. Avoidance of question-ID special cases via generic synthetic single percentage questions.
  4. Citation and evidence checks remain fully intact in grounded evaluation.
  5. Preservation of historical saved benchmark artifacts and offline reproducibility.
"""

import json
from pathlib import Path
from decimal import Decimal

from services.evaluation.benchmark_scorer import (
    SCORER_VERSION,
    content_match,
    compute_answer_relevancy,
    grade_canonical_answer_text,
    evaluate_grounded_answer,
    rescore_query_record_offline,
    is_concise_percentage_match,
)
from services.retrieval.evidence_contract import EvidenceItem


# Ground truth for Q149
Q149_EXPECTED = "99.58% of the population lives in households with electricity."
Q149_SOURCE = "NFHS_5_India_Districts_Factsheet_Data.xls"


# --- 1. Q149 Reproduction Through Canonical Grading Path ---

def test_q149_concise_percentage_answer_passes_canonical_text_grading():
    """Reproduce Q149: accept 99.58% as correct answer text for the single percentage requested."""
    ans_text = "99.58%"

    # 1. Concise percentage helper
    assert is_concise_percentage_match(ans_text, Q149_EXPECTED) is True

    # 2. Canonical content_match
    c_match = content_match(ans_text, Q149_EXPECTED)
    assert c_match is True

    # 3. Canonical compute_answer_relevancy
    relevancy = compute_answer_relevancy(ans_text, Q149_EXPECTED)
    assert relevancy >= 0.40
    assert relevancy == 0.60

    # 4. Canonical grading path (c_match and relevancy >= 0.40)
    is_correct = grade_canonical_answer_text(ans_text, Q149_EXPECTED, contract_type="semantic")
    assert is_correct is True


# --- 2. Control 1: Wrong Value Rejected ---

def test_control_wrong_value_rejected():
    """Verify answers asserting wrong numeric percentage values are strictly rejected."""
    wrong_answers = ["98.50%", "50.00%", "12.34%", "100.00%", "0.42%"]
    for ans in wrong_answers:
        assert is_concise_percentage_match(ans, Q149_EXPECTED) is False, f"Accepted wrong value: {ans}"
        assert content_match(ans, Q149_EXPECTED) is False, f"content_match accepted wrong value: {ans}"
        assert grade_canonical_answer_text(ans, Q149_EXPECTED, contract_type="semantic") is False, (
            f"grade_canonical_answer_text accepted wrong value: {ans}"
        )


# --- 3. Control 2: Wrong Unit Rejected ---

def test_control_wrong_unit_rejected():
    """Verify answers with wrong units (bare numbers, currency, non-percentage symbols) are strictly rejected."""
    wrong_unit_answers = [
        "99.58",            # Bare number without %
        "$99.58",           # Dollar prefix
        "99.58 dollars",    # Dollar suffix
        "99.58 households", # Non-percentage unit
        "99.58 kg",         # Weight unit
    ]
    for ans in wrong_unit_answers:
        assert is_concise_percentage_match(ans, Q149_EXPECTED) is False, f"Accepted wrong unit: {ans}"
        assert content_match(ans, Q149_EXPECTED) is False, f"content_match accepted wrong unit: {ans}"
        assert grade_canonical_answer_text(ans, Q149_EXPECTED, contract_type="semantic") is False, (
            f"grade_canonical_answer_text accepted wrong unit: {ans}"
        )


# --- 4. Control 3: Negated Value Rejected ---

def test_control_negated_value_rejected():
    """Verify answers asserting negated or contradictory values are strictly rejected."""
    negated_answers = [
        "not 99.58%",
        "It is not 99.58%.",
        "Definitely not 99.58%.",
        "The percentage is never 99.58%.",
        "South Andaman does not have 99.58% of households with electricity.",
    ]
    for ans in negated_answers:
        assert is_concise_percentage_match(ans, Q149_EXPECTED) is False, f"Accepted negated answer: {ans}"
        assert content_match(ans, Q149_EXPECTED) is False, f"content_match accepted negated answer: {ans}"
        assert grade_canonical_answer_text(ans, Q149_EXPECTED, contract_type="semantic") is False, (
            f"grade_canonical_answer_text accepted negated answer: {ans}"
        )


# --- 5. Control 4: Answer That Omits Other Required Claims Rejected ---

def test_control_omits_other_required_claims_rejected():
    """Verify concise percentage answers are rejected when expected answer requires other claims."""
    # Case A: Q143 requires both dollar gross margin ($54,770 million) and percentage (50.06%)
    q143_expected = "Gross margin was 54,770 million dollars, or approximately 50.06% of total net sales (54,770 / 109,417 × 100)."
    ans_q143_omitted = "50.06%"
    assert is_concise_percentage_match(ans_q143_omitted, q143_expected) is False
    assert content_match(ans_q143_omitted, q143_expected) is False
    assert grade_canonical_answer_text(ans_q143_omitted, q143_expected, contract_type="semantic") is False

    # Case B: Q147 requires both named district ('North & Middle Andaman') and percentage ('100.00%')
    q147_expected = "North & Middle Andaman, 100.00%."
    ans_q147_omitted = "100.00%"
    assert is_concise_percentage_match(ans_q147_omitted, q147_expected) is False
    assert content_match(ans_q147_omitted, q147_expected) is False
    assert grade_canonical_answer_text(ans_q147_omitted, q147_expected, contract_type="semantic") is False

    # Full answer with both claims passes
    assert grade_canonical_answer_text("North & Middle Andaman, 100.00%.", q147_expected, contract_type="semantic") is True

    # Case C: Multi-percentage claim question
    multi_pct_expected = "Accuracy rises by 2.04 percentage points, from 69.76% to 71.8%."
    ans_multi_omitted = "69.76%"
    assert is_concise_percentage_match(ans_multi_omitted, multi_pct_expected) is False
    assert content_match(ans_multi_omitted, multi_pct_expected) is False
    assert grade_canonical_answer_text(ans_multi_omitted, multi_pct_expected, contract_type="semantic") is False


# --- 6. Generic Synthetic Single Percentage Questions (No Question-ID Special Cases) ---

def test_generic_synthetic_single_percentage_questions_without_question_id():
    """Verify grading rules generalize to arbitrary synthetic percentage questions without question-ID dependencies."""
    synthetic_expected = "73.25% of candidates passed the state examination."

    # Valid concise answer
    assert grade_canonical_answer_text("73.25%", synthetic_expected, contract_type="semantic") is True
    assert grade_canonical_answer_text("Approximately 73.25%.", synthetic_expected, contract_type="semantic") is True

    # Controls
    assert grade_canonical_answer_text("73.00%", synthetic_expected, contract_type="semantic") is False  # Wrong value
    assert grade_canonical_answer_text("73.25", synthetic_expected, contract_type="semantic") is False    # Wrong unit
    assert grade_canonical_answer_text("$73.25", synthetic_expected, contract_type="semantic") is False   # Wrong unit
    assert grade_canonical_answer_text("not 73.25%", synthetic_expected, contract_type="semantic") is False # Negated


# --- 7. Grounded Evaluation: Citation and Evidence Checks Intact ---

def test_grounded_evaluation_citations_and_evidence_checks_intact():
    """Verify grounded answer evaluation retains all strict citation and evidence gates for percentage answers."""
    ans_text = "99.58%"
    chunk_id = "fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0:xls:Sheet1:row3"
    doc_id = "fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0"
    ws_id = "ws_canonical_comparison_0004"

    context_snippet = (
        "District Names: South Andaman. State/UT: Andaman & Nicobar Islands. "
        "Population living in households with electricity (%): 99.58. Number of Households surveyed: 868."
    )

    ev_item = EvidenceItem(
        evidence_id=chunk_id,
        workspace_id=ws_id,
        document_id=doc_id,
        document_version="e482b990d74eb46b5ce5a6e10606b745",
        source_type="chunk",
        locator={"sheet": "Sheet1", "row": 4},
        text=context_snippet,
        structured_payload=None,
        score=0.95,
        strategy="vector_rerank",
        provenance={"source_format": "xls"},
        citation_payload={
            "chunk_id": chunk_id,
            "document_id": doc_id,
            "document_filename": Q149_SOURCE,
            "strategy": "vector_rerank",
        },
    )

    valid_citation = {
        "chunk_id": chunk_id,
        "document_id": doc_id,
        "document_filename": Q149_SOURCE,
        "location_reference": "Sheet: Sheet1, Row: 4",
        "snippet": context_snippet,
    }

    # 1. Valid citations and evidence -> Grounded pass
    eval_pass = evaluate_grounded_answer(
        answer=ans_text,
        citations=[valid_citation],
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=[Q149_SOURCE],
        retained_evidence_items=[ev_item],
    )
    assert eval_pass["is_grounded_correct"] is True
    assert eval_pass["reason"] == "grounded_and_verified"
    assert eval_pass["citation_validation"]["valid"] is True

    # 2. Invalid citation (wrong source filename, e.g. historical Doc-fcc723b5) -> Fails
    invalid_cit = dict(valid_citation)
    invalid_cit["document_filename"] = "Doc-fcc723b5"
    eval_invalid_cit = evaluate_grounded_answer(
        answer=ans_text,
        citations=[invalid_cit],
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=[Q149_SOURCE],
        retained_evidence_items=[ev_item],
    )
    assert eval_invalid_cit["is_grounded_correct"] is False
    assert "invalid_citations" in eval_invalid_cit["reason"]

    # 3. Missing citations -> Fails
    eval_missing_cit = evaluate_grounded_answer(
        answer=ans_text,
        citations=[],
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=[Q149_SOURCE],
        retained_evidence_items=[ev_item],
    )
    assert eval_missing_cit["is_grounded_correct"] is False
    assert eval_missing_cit["reason"] == "missing_citations"

    # 4. Missing authoritative evidence -> Fails
    eval_missing_ev = evaluate_grounded_answer(
        answer=ans_text,
        citations=[valid_citation],
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=[Q149_SOURCE],
        retained_evidence_items=[],
    )
    assert eval_missing_ev["is_grounded_correct"] is False
    assert (
        "retained evidence" in eval_missing_ev["reason"]
        or eval_missing_ev["reason"] == "missing_authoritative_evidence"
    )

    # 5. Contradictory answer against evidence -> Fails
    contra_ans = "South Andaman households definitely do not have 99.58% electricity."
    eval_contra = evaluate_grounded_answer(
        answer=contra_ans,
        citations=[valid_citation],
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=[Q149_SOURCE],
        retained_evidence_items=[ev_item],
    )
    assert eval_contra["is_grounded_correct"] is False
    assert "claim_contradicts_evidence" in eval_contra["reason"]


# --- 8. Historical Artifacts and Offline Rescoring Preservation ---

def test_historical_q149_record_preserved_offline():
    """Verify saved Q149 record from historical run strictly preserves its original verdict."""
    report_path = (
        Path(__file__).resolve().parent
        / ".."
        / "reports"
        / "73075af21d9172155cad22866d8eef2e7c186edb"
        / "20261123_075022"
        / "query_details.json"
    )
    if not report_path.exists():
        return

    with open(report_path, encoding="utf-8") as f:
        records = json.load(f)

    q149 = next((r for r in records if r.get("id") == "Q149"), None)
    assert q149 is not None

    # Replay offline with default recompute_text_correctness=False strictly preserves historical verdict
    eval_hist = rescore_query_record_offline(q149)
    assert eval_hist["is_grounded_correct"] is False
    assert eval_hist["reason"] == "answer_text_incorrect"

    # Replay offline with recompute_text_correctness=True validates text correctness
    # while continuing to enforce Citation 5 source validation ('Doc-fcc723b5' vs expected sources)
    eval_recomputed = rescore_query_record_offline(q149, recompute_text_correctness=True)
    assert eval_recomputed["is_grounded_correct"] is False
    assert "invalid_citations" in eval_recomputed["reason"]
