"""Unit tests for grounded-answer scoring in the canonical benchmark evaluator.

Covers:
  - Preserved answer-text scoring vs grounded-answer scoring
  - Grounded correctness requiring answer correctness plus validated request-retained evidence
  - Answerable responses with missing, invalid, wrong-source, or wrong-version citations failing
  - Structured aggregate evidence validation (asserting computed result_value, not metadata counts)
  - Refusal contracts (clean refusal with 0 citations passes; fabricated citations fail)
  - Matching citation IDs alone not establishing claim support
  - Explicit defect control: contradictory-answer grading defect in text scoring kept explicitly open
"""

import pytest
from types import SimpleNamespace

from services.evaluation.benchmark_scorer import (
    validate_citations,
    evaluate_grounded_answer,
    detect_contradiction_against_evidence,
    content_match,
    compute_answer_relevancy,
    extract_numbers_normalized,
)


def _make_retained_evidence_item(
    evidence_id: str = "chunk_sec_001",
    document_filename: str = "SEC-Form-10Q.pdf",
    document_version: int = 1,
    document_id: str = "doc_sec_01",
    text: str = "Apple Inc. Commission File Number: 001-36743. Form 10-Q.",
) -> SimpleNamespace:
    return SimpleNamespace(
        evidence_id=evidence_id,
        document_filename=document_filename,
        document_version=document_version,
        document_id=document_id,
        locator={"chunk_id": evidence_id, "document_filename": document_filename, "document_version": document_version, "document_id": document_id},
        citation_payload={"chunk_id": evidence_id},
        text=text,
    )


# ===========================================================================
# Positive Controls
# ===========================================================================

def test_supported_text_answer_positive_control():
    """Valid text answer with supported citation, matching source, and context text passes grounded scoring."""
    retained_item = _make_retained_evidence_item()
    citations = [
        {
            "chunk_id": "chunk_sec_001",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 1,
            "document_id": "doc_sec_01",
            "page_number": 1,
            "text": "Commission File Number: 001-36743. Apple Inc. Quarterly Report.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Apple's Commission File Number is 001-36743.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_001"},
    )

    assert result["is_grounded_correct"] is True
    assert result["grounded_correct"] is True
    assert result["reason"] == "grounded_and_verified"
    assert result["citation_validation"]["valid"] is True


def test_clean_refusal_positive_control():
    """A clean refusal with zero citations for a refusal contract passes grounded scoring."""
    result = evaluate_grounded_answer(
        answer="I couldn't find any relevant passages in the workspace documents matching your query.",
        citations=[],
        is_answer_text_correct=True,
        contract_type="refusal",
        contract={"contract_type": "refusal", "is_refusal_required": True},
        ans_is_refusal=True,
    )

    assert result["is_grounded_correct"] is True
    assert result["reason"] == "valid_unsupported_refusal"
    assert result["citation_validation"]["valid"] is True


def test_premise_correction_positive_control():
    """A valid premise correction citing the ground truth evidence passes grounded scoring."""
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_eps",
        text="Basic earnings per share was $2.03 for the three months ended June 27, 2026.",
    )
    citations = [
        {
            "chunk_id": "chunk_sec_eps",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 1,
            "page_number": 4,
            "text": "Basic earnings per share was $2.03 for the three months ended June 27, 2026.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="No. Basic EPS is $2.03 per share for the three months ended June 27, 2026.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="premise_correction",
        expected_sources=["SEC-Form-10Q.pdf"],
        retained_evidence_items=[retained_item],
    )

    assert result["is_grounded_correct"] is True
    assert result["reason"] == "grounded_and_verified"


def test_structured_aggregate_positive_control():
    """Structured aggregate answer asserting the computed result_value passes grounded scoring."""
    hash_val = "1234567890abcdef"
    ev = SimpleNamespace(
        evidence_id="ev_agg_1",
        workspace_id="ws_01",
        document_version="1",
        structured_payload={
            "selection_hash": hash_val,
            "table_id": "survay",
            "operator": "sum",
            "target_column": "Total_Value",
            "selection_count": 5,
            "result_value": "157000",
        },
        locator={"selection_hash": hash_val, "table_id": "survay", "selection_count": 5},
    )
    citations = [
        {
            "evidence_type": "structured_aggregate",
            "table_id": "survay",
            "document_version": "1",
            "operator": "sum",
            "target_column": "Total_Value",
            "selection_count": 5,
            "selection_hash": hash_val,
            "result_value": "157000",
        }
    ]

    result = evaluate_grounded_answer(
        answer="The total sum of Total_Value across the selected records is 157,000.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "target_values": [157000]},
        question_type="structured_aggregate",
        retained_evidence_items=[ev],
        retained_structured_hashes={hash_val},
    )

    assert result["is_grounded_correct"] is True
    assert result["reason"] == "grounded_structured_aggregate"


# ===========================================================================
# Negative Controls
# ===========================================================================

def test_missing_citations_fails_grounded_scoring():
    """Answerable responses with missing citations must fail grounded scoring."""
    result = evaluate_grounded_answer(
        answer="Apple's Commission File Number is 001-36743.",
        citations=[],
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=["SEC-Form-10Q.pdf"],
    )

    assert result["is_grounded_correct"] is False
    assert result["reason"] == "missing_citations"


def test_invalid_citation_fields_fails_grounded_scoring():
    """Citations missing required fields must fail grounded scoring."""
    citations = [
        {
            # missing chunk_id
            "document_filename": "SEC-Form-10Q.pdf",
            "page_number": 1,
            "text": "Some text",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Apple's Commission File Number is 001-36743.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
    )

    assert result["is_grounded_correct"] is False
    assert "invalid_citations" in result["reason"]


def test_wrong_source_citation_fails_grounded_scoring():
    """Citations citing the wrong source document must fail grounded scoring."""
    citations = [
        {
            "chunk_id": "chunk_arxiv_01",
            "document_filename": "2212.14776v3.pdf",  # Wrong source!
            "page_number": 1,
            "text": "Attention network interpretability research.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Apple's Commission File Number is 001-36743.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=["SEC-Form-10Q.pdf"],
    )

    assert result["is_grounded_correct"] is False
    assert "not in expected sources" in result["reason"]


def test_wrong_version_citation_fails_grounded_scoring():
    """Citations with a version differing from expected version must fail grounded scoring."""
    citations = [
        {
            "chunk_id": "chunk_sec_001",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 2,  # Expected 1!
            "page_number": 1,
            "text": "Commission File Number: 001-36743.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Apple's Commission File Number is 001-36743.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
    )

    assert result["is_grounded_correct"] is False
    assert "does not match expected version" in result["reason"]


def test_altered_retained_evidence_fails_grounded_scoring():
    """Citations that alter document_filename compared to retained evidence must fail."""
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_001",
        document_filename="SEC-Form-10Q.pdf",
    )
    citations = [
        {
            "chunk_id": "chunk_sec_001",
            "document_filename": "Forged-Document.pdf",  # Altered!
            "page_number": 1,
            "text": "Commission File Number: 001-36743.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Apple's Commission File Number is 001-36743.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        retained_evidence_items=[retained_item],
    )

    assert result["is_grounded_correct"] is False
    assert "altered from retained evidence" in result["reason"]


def test_matching_citation_id_alone_low_faithfulness_fails():
    """Matching citation ID alone must not establish claim support when context does not support answer."""
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_001",
        text="The Commission regulates corporate financial disclosure filings under the Securities Act.",
    )
    citations = [
        {
            "chunk_id": "chunk_sec_001",  # Matches ground truth ID!
            "document_filename": "SEC-Form-10Q.pdf",
            "page_number": 1,
            # Snippet has no mention of Steve Jobs or Los Altos
            "text": "The Commission regulates corporate financial disclosure filings under the Securities Act.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Apple was founded in 1976 by Steve Jobs in a garage in Los Altos California.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=["SEC-Form-10Q.pdf"],
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_001"},
    )

    assert result["is_grounded_correct"] is False
    assert "low citation faithfulness" in result["reason"] or "claim_unsupported" in result["reason"]


def test_matching_citation_id_alone_unsupported_numeric_claim_fails():
    """Matching citation ID alone must not establish claim support when answer invents numbers not in context."""
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_001",
        text="Apple Inc. reports quarterly revenue and expenses in accordance with GAAP.",
    )
    citations = [
        {
            "chunk_id": "chunk_sec_001",
            "document_filename": "SEC-Form-10Q.pdf",
            "page_number": 1,
            "text": "Apple Inc. reports quarterly revenue and expenses in accordance with GAAP.",
        }
    ]

    # The answer shares text terms with the snippet so faithfulness passes, but claims 999,888 which is NOT in the cited snippet
    result = evaluate_grounded_answer(
        answer="Apple Inc. reports quarterly revenue of 999888.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=["SEC-Form-10Q.pdf"],
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_001"},
    )

    assert result["is_grounded_correct"] is False
    assert "numeric claims" in result["reason"]


def test_refusal_with_fabricated_citations_fails():
    """Refusal contract where model answered refusal, but cited fabricated citations fails."""
    citations = [
        {
            "chunk_id": "chunk_invented_01",
            "document_filename": "survay.csv",
            "page_number": 1,
            "text": "Fabricated evidence snippet.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="I couldn't find any relevant passages in the workspace documents matching your query.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="refusal",
        contract={"contract_type": "refusal", "is_refusal_required": True},
        ans_is_refusal=True,
    )

    assert result["is_grounded_correct"] is False
    assert result["reason"] == "refusal_contract_with_fabricated_citations"


def test_structured_aggregate_claiming_selection_count_fails():
    """Structured aggregate claiming selection_count instead of result_value fails grounded scoring."""
    hash_val = "1234567890abcdef"
    ev = SimpleNamespace(
        evidence_id="ev_agg_1",
        workspace_id="ws_01",
        document_version="1",
        structured_payload={
            "selection_hash": hash_val,
            "table_id": "survay",
            "operator": "sum",
            "target_column": "Total_Value",
            "selection_count": 14,
            "result_value": "500000",
        },
        locator={"selection_hash": hash_val, "table_id": "survay", "selection_count": 14},
    )
    citations = [
        {
            "evidence_type": "structured_aggregate",
            "table_id": "survay",
            "document_version": "1",
            "operator": "sum",
            "target_column": "Total_Value",
            "selection_count": 14,
            "selection_hash": hash_val,
            "result_value": "500000",
        }
    ]

    # Model incorrectly claims 14 (the selection count) as the sum
    result = evaluate_grounded_answer(
        answer="The total sum of Total_Value is 14.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "target_values": [500000]},
        question_type="structured_aggregate",
        retained_evidence_items=[ev],
        retained_structured_hashes={hash_val},
    )

    assert result["is_grounded_correct"] is False
    assert "does not assert computed result" in result["reason"] or "amounts" in result["reason"]


def test_infrastructure_error_fails():
    """Infrastructure failure must immediately fail grounded scoring."""
    result = evaluate_grounded_answer(
        answer="Database timeout",
        citations=[],
        is_answer_text_correct=False,
        is_infra_error=True,
    )

    assert result["is_grounded_correct"] is False
    assert result["reason"] == "infrastructure_error"


def test_incorrect_answer_text_fails():
    """If answer text is incorrect, grounded scoring must fail even with valid citations."""
    retained_item = _make_retained_evidence_item()
    citations = [
        {
            "chunk_id": "chunk_sec_001",
            "document_filename": "SEC-Form-10Q.pdf",
            "page_number": 1,
            "text": "Commission File Number: 001-36743.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Apple's Commission File Number is 999-99999.",
        citations=citations,
        is_answer_text_correct=False,  # Text score failed
        contract_type="semantic",
        retained_evidence_items=[retained_item],
    )

    assert result["is_grounded_correct"] is False
    assert result["reason"] == "answer_text_incorrect"


# ===========================================================================
# Defect Control: Contradictory Answer Grading Defect Kept Explicitly Open
# ===========================================================================

def test_contradictory_answer_defect_remains_open_in_text_scoring():
    """Proves the known contradictory-answer grading defect remains explicitly open in text scoring.

    For question Q136: 'What is Apple's Commission File Number?'
    Gold answer: '001-36743'
    Contradictory answer: 'According to the SEC filing, Apple's Commission File Number is definitely NOT 001-36743.'

    The legacy text scorer evaluates token recall and relevancy without negation awareness,
    grading this contradictory answer as is_correct = True.
    This test verifies that this defect is preserved unchanged in text scoring.
    """
    gold_answer = "001-36743"
    contradictory_answer = "According to the SEC filing, Apple's Commission File Number is definitely NOT 001-36743."

    c_match = content_match(contradictory_answer, gold_answer)
    relevancy = compute_answer_relevancy(contradictory_answer, gold_answer)
    is_text_correct = c_match and relevancy >= 0.40

    # Defect must be explicitly open: text scorer marks contradictory answer as correct
    assert is_text_correct is True, (
        "Expected contradictory answer to score True in legacy text scoring due to token overlap defect."
    )


# ===========================================================================
# Controls: Contradictory Claims Rejected in Grounded-Answer Scoring
# ===========================================================================

def test_control_1_supported_assertion_passes_grounded_scoring():
    """Control 1: Supported assertion affirming evidence filing number passes grounded scoring.

    Evidence asserts 001-36743. Answer asserts 001-36743 without contradiction.
    """
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_001",
        text="Apple Inc. Commission File Number: 001-36743. Form 10-Q.",
    )
    citations = [
        {
            "chunk_id": "chunk_sec_001",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 1,
            "document_id": "doc_sec_01",
            "page_number": 1,
            "text": "Commission File Number: 001-36743. Apple Inc. Quarterly Report.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Apple's Commission File Number is 001-36743.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_001"},
    )

    assert result["is_grounded_correct"] is True
    assert result["grounded_correct"] is True
    assert result["reason"] == "grounded_and_verified"
    assert result["citation_validation"]["valid"] is True


def test_control_2_explicit_denial_reproduction_fails_grounded_scoring():
    """Control 2: Explicit denial of evidence filing number fails grounded scoring.

    Reproduces Q136: Retained evidence asserts filing number 001-36743 while
    the answer explicitly denies that number ('definitely NOT 001-36743').
    While legacy text scoring marks this correct due to token overlap,
    grounded scoring must fail due to claim-level contradiction.
    """
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_001",
        text="Apple Inc. Commission File Number: 001-36743. Form 10-Q.",
    )
    citations = [
        {
            "chunk_id": "chunk_sec_001",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 1,
            "document_id": "doc_sec_01",
            "page_number": 1,
            "text": "Commission File Number: 001-36743. Apple Inc. Quarterly Report.",
        }
    ]
    contradictory_answer = (
        "According to the SEC filing, Apple's Commission File Number is definitely NOT 001-36743."
    )

    # Legacy text scoring marks it correct (preserved separately)
    gold_answer = "001-36743"
    c_match = content_match(contradictory_answer, gold_answer)
    relevancy = compute_answer_relevancy(contradictory_answer, gold_answer)
    is_text_correct = c_match and relevancy >= 0.40
    assert is_text_correct is True

    # Grounded scoring must fail
    result = evaluate_grounded_answer(
        answer=contradictory_answer,
        citations=citations,
        is_answer_text_correct=is_text_correct,
        contract_type="semantic",
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_001"},
    )

    assert result["is_grounded_correct"] is False
    assert result["grounded_correct"] is False
    assert "claim_contradicts_evidence" in result["reason"]
    assert "001-36743" in result["reason"]


def test_control_3_conflicting_assertions_fails_grounded_scoring():
    """Control 3: Conflicting assertions denying evidence facts fail grounded scoring.

    Case A: Answer contains conflicting clauses, affirming then denying the evidence fact:
            'Apple's Commission File Number is 001-36743, but it is not 001-36743.'
    Case B: Answer asserts a conflicting value while denying the evidence value:
            'The Commission File Number is 001-99999, not 001-36743.'
    Both cases must fail grounded scoring.
    """
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_001",
        text="Apple Inc. Commission File Number: 001-36743. Form 10-Q.",
    )
    citations = [
        {
            "chunk_id": "chunk_sec_001",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 1,
            "document_id": "doc_sec_01",
            "page_number": 1,
            "text": "Commission File Number: 001-36743. Apple Inc. Quarterly Report.",
        }
    ]

    # Case A: affirms then denies
    ans_a = "Apple's Commission File Number is 001-36743, but it is not 001-36743."
    res_a = evaluate_grounded_answer(
        answer=ans_a,
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_001"},
    )
    assert res_a["is_grounded_correct"] is False
    assert "claim_contradicts_evidence" in res_a["reason"]

    # Case B: denies evidence value while asserting alternative
    ans_b = "The Commission File Number is 001-99999, not 001-36743."
    res_b = evaluate_grounded_answer(
        answer=ans_b,
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_001"},
    )
    assert res_b["is_grounded_correct"] is False
    assert "claim_contradicts_evidence" in res_b["reason"]


def test_control_4_valid_premise_correction_passes_grounded_scoring():
    """Control 4: Valid premise corrections pass grounded scoring without false rejection.

    Blanket rejection of 'not' or 'no' must be avoided so legitimate premise corrections
    that correct false question premises while affirming true evidence facts pass.

    Case A: Answer begins with 'No.' and affirms the reported basic EPS of $2.03.
    Case B: Answer explicitly denies the false premise ('not $5.00') and affirms $2.03.
    """
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_eps",
        text="Basic earnings per share was $2.03 for the three months ended June 27, 2026.",
    )
    citations = [
        {
            "chunk_id": "chunk_sec_eps",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 1,
            "document_id": "doc_sec_01",
            "page_number": 4,
            "text": "Basic earnings per share was $2.03 for the three months ended June 27, 2026.",
        }
    ]

    # Case A: 'No. Basic EPS is $2.03 per share...'
    ans_a = "No. Basic EPS is $2.03 per share for the three months ended June 27, 2026."
    res_a = evaluate_grounded_answer(
        answer=ans_a,
        citations=citations,
        is_answer_text_correct=True,
        contract_type="premise_correction",
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_eps"},
    )
    assert res_a["is_grounded_correct"] is True
    assert res_a["reason"] == "grounded_and_verified"

    # Case B: 'No, basic EPS is not $5.00, it is $2.03 per share.'
    ans_b = "No, basic EPS is not $5.00, it is $2.03 per share."
    res_b = evaluate_grounded_answer(
        answer=ans_b,
        citations=citations,
        is_answer_text_correct=True,
        contract_type="premise_correction",
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_eps"},
    )
    assert res_b["is_grounded_correct"] is True
    assert res_b["reason"] == "grounded_and_verified"


def test_control_5_valid_refusal_passes_grounded_scoring():
    """Control 5: Clean refusal with zero citations passes grounded scoring.

    Refusal contracts requiring refusal must pass when 0 citations are provided,
    unaffected by contradiction logic.
    """
    result = evaluate_grounded_answer(
        answer="I couldn't find any relevant passages in the workspace documents matching your query.",
        citations=[],
        is_answer_text_correct=True,
        contract_type="refusal",
        contract={"contract_type": "refusal", "is_refusal_required": True},
        ans_is_refusal=True,
    )

    assert result["is_grounded_correct"] is True
    assert result["reason"] == "valid_unsupported_refusal"
    assert result["citation_validation"]["valid"] is True


def test_control_6_structured_aggregate_denial_fails_grounded_scoring():
    """Control 6: Explicit denial of retained computed structured result fails grounded scoring."""
    hash_val = "1234567890abcdef"
    ev = SimpleNamespace(
        evidence_id="ev_agg_1",
        workspace_id="ws_01",
        document_version="1",
        structured_payload={
            "selection_hash": hash_val,
            "table_id": "survay",
            "operator": "sum",
            "target_column": "Total_Value",
            "selection_count": 5,
            "result_value": "157000",
        },
        locator={"selection_hash": hash_val, "table_id": "survay", "selection_count": 5},
    )
    citations = [
        {
            "evidence_type": "structured_aggregate",
            "table_id": "survay",
            "document_version": "1",
            "operator": "sum",
            "target_column": "Total_Value",
            "selection_count": 5,
            "selection_hash": hash_val,
            "result_value": "157000",
        }
    ]

    result = evaluate_grounded_answer(
        answer="The total sum is definitely not 157,000.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "target_values": [157000]},
        question_type="structured_aggregate",
        retained_evidence_items=[ev],
        retained_structured_hashes={hash_val},
    )

    assert result["is_grounded_correct"] is False
    assert "claim_contradicts_evidence" in result["reason"]


# ===========================================================================
# Regressions for Three Reproduced False Passes & Evidence Authority Repairs
# ===========================================================================

def test_false_pass_1_fabricated_citation_snippet_fails():
    """False Pass 1 Regression: Text answer with fabricated citation snippet must fail grounded scoring.

    The model cites a retained chunk whose actual text does not contain the answer,
    but fabricates the answer inside the citation dictionary's text snippet.
    Grounded scoring must reject the fabricated snippet and evaluate claims
    strictly against authoritative retained evidence text.
    """
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_001",
        text="The Commission regulates corporate financial disclosure filings under the Securities Act.",
    )
    # Adversarial citation: claims the chunk contains the Commission File Number
    citations = [
        {
            "chunk_id": "chunk_sec_001",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 1,
            "document_id": "doc_sec_01",
            "page_number": 1,
            "text": "Commission File Number: 001-36743. Apple Inc. Quarterly Report.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Apple's Commission File Number is 001-36743.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_001"},
    )

    assert result["is_grounded_correct"] is False
    assert (
        "fabricates supporting content" in result["reason"]
        or "claim_unsupported" in result["reason"]
    )


def test_false_pass_2_structured_aggregate_ground_truth_target_substitution_fails():
    """False Pass 2 Regression: Structured aggregate must not substitute contract target_values for missing result_value.

    When the retained evidence for a structured query has selection_count but NO result_value,
    the evaluator must not substitute the evaluator contract's target_values to grant a pass.
    """
    hash_val = "1234567890abcdef"
    ev = SimpleNamespace(
        evidence_id="ev_agg_1",
        workspace_id="ws_01",
        document_version="1",
        structured_payload={
            "selection_hash": hash_val,
            "table_id": "survay",
            "operator": "sum",
            "target_column": "Total_Value",
            "selection_count": 14,
            "result_value": None,  # Missing execution result!
        },
        locator={"selection_hash": hash_val, "table_id": "survay", "selection_count": 14},
    )
    citations = [
        {
            "evidence_type": "structured_aggregate",
            "table_id": "survay",
            "document_version": "1",
            "operator": "sum",
            "target_column": "Total_Value",
            "selection_count": 14,
            "selection_hash": hash_val,
            "result_value": None,
        }
    ]

    result = evaluate_grounded_answer(
        answer="The total sum of Total_Value is 500,000.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "target_values": [500000]},  # Ground-truth target
        question_type="structured_aggregate",
        retained_evidence_items=[ev],
        retained_structured_hashes={hash_val},
    )

    assert result["is_grounded_correct"] is False
    assert result["reason"] == "missing_retained_result_value"


def test_false_pass_3_structured_aggregate_missing_result_fallback_fails():
    """False Pass 3 Regression: Missing-result fallback must be removed.

    When a structured aggregate execution has no computed result, it must not pass
    via grounded_structured_aggregate_fallback.
    """
    hash_val = "1234567890abcdef"
    ev = SimpleNamespace(
        evidence_id="ev_agg_1",
        workspace_id="ws_01",
        document_version="1",
        structured_payload={
            "selection_hash": hash_val,
            "table_id": "survay",
            "operator": "sum",
            "target_column": "Total_Value",
            "selection_count": 14,
            "result_value": None,
        },
        locator={"selection_hash": hash_val, "table_id": "survay", "selection_count": 14},
    )
    citations = [
        {
            "evidence_type": "structured_aggregate",
            "table_id": "survay",
            "document_version": "1",
            "operator": "sum",
            "target_column": "Total_Value",
            "selection_count": 14,
            "selection_hash": hash_val,
            "result_value": None,
        }
    ]

    result = evaluate_grounded_answer(
        answer="The total sum is 500,000.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        question_type="structured_aggregate",
        retained_evidence_items=[ev],
        retained_structured_hashes={hash_val},
    )

    assert result["is_grounded_correct"] is False
    assert result["reason"] == "missing_retained_result_value"


def test_missing_authoritative_evidence_fails_grounded_scoring():
    """Answerable queries with missing authoritative retained evidence must fail grounded scoring."""
    citations = [
        {
            "chunk_id": "chunk_sec_001",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 1,
            "document_id": "doc_sec_01",
            "page_number": 1,
            "text": "Commission File Number: 001-36743.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Apple's Commission File Number is 001-36743.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=None,  # Missing!
    )

    assert result["is_grounded_correct"] is False
    assert result["reason"] == "missing_authoritative_evidence"


def test_legitimate_count_operation_positive_control():
    """For COUNT operations, selection_count is legitimately preserved as the count result."""
    hash_val = "1234567890abcdef"
    ev = SimpleNamespace(
        evidence_id="ev_agg_1",
        workspace_id="ws_01",
        document_version="1",
        structured_payload={
            "selection_hash": hash_val,
            "table_id": "survay",
            "operator": "count",
            "target_column": "*",
            "selection_count": 25,
            "result_value": None,  # No separate result_value, selection_count is count
        },
        locator={"selection_hash": hash_val, "table_id": "survay", "selection_count": 25},
    )
    citations = [
        {
            "evidence_type": "structured_aggregate",
            "table_id": "survay",
            "document_version": "1",
            "operator": "count",
            "target_column": "*",
            "selection_count": 25,
            "selection_hash": hash_val,
        }
    ]

    result = evaluate_grounded_answer(
        answer="There are 25 records matching the criteria.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "operator": "count"},
        question_type="structured_aggregate",
        retained_evidence_items=[ev],
        retained_structured_hashes={hash_val},
    )

    assert result["is_grounded_correct"] is True
    assert result["reason"] == "grounded_structured_aggregate"


def test_legitimate_zero_result_positive_control():
    """Zero-result answers asserting 0 or zero match 0 computed result and pass."""
    hash_val = "1234567890abcdef"
    ev = SimpleNamespace(
        evidence_id="ev_agg_1",
        workspace_id="ws_01",
        document_version="1",
        structured_payload={
            "selection_hash": hash_val,
            "table_id": "survay",
            "operator": "count",
            "target_column": "*",
            "selection_count": 1,
            "result_value": "0",
        },
        locator={"selection_hash": hash_val, "table_id": "survay", "selection_count": 1},
    )
    citations = [
        {
            "evidence_type": "structured_aggregate",
            "table_id": "survay",
            "document_version": "1",
            "operator": "count",
            "target_column": "*",
            "selection_count": 1,
            "selection_hash": hash_val,
            "result_value": "0",
        }
    ]

    result = evaluate_grounded_answer(
        answer="There are zero records found matching the query.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "operator": "count"},
        question_type="structured_aggregate",
        retained_evidence_items=[ev],
        retained_structured_hashes={hash_val},
    )

    assert result["is_grounded_correct"] is True
    assert result["reason"] == "grounded_structured_aggregate"


def test_legitimate_derived_calculation_positive_control():
    """Text retrieval questions with numeric derived calculations pass when operands appear in context."""
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_sales",
        text="Total net sales were 94930 million in 2026 compared to 81797 million in 2025.",
    )
    citations = [
        {
            "chunk_id": "chunk_sec_sales",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 1,
            "document_id": "doc_sec_01",
            "page_number": 2,
            "text": "Total net sales were 94930 million in 2026 compared to 81797 million in 2025.",
        }
    ]

    # The calculated growth is 13133 (94930 - 81797), which is not literally in the text chunk
    result = evaluate_grounded_answer(
        answer="Net sales increased by 13133 million year over year.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={
            "contract_type": "numeric",
            "target_values": [13133],
            "operands": [94930, 81797],
        },
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_sales"},
    )

    assert result["is_grounded_correct"] is True
    assert result["reason"] == "grounded_and_verified"


def test_unsupported_derived_calculation_operands_missing_fails():
    """Numeric derived calculations fail when operands are missing from authoritative context."""
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_sales",
        text="Total units shipped were 500 in 2026.",
    )
    citations = [
        {
            "chunk_id": "chunk_sec_sales",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 1,
            "document_id": "doc_sec_01",
            "page_number": 2,
            "text": "Total units shipped were 500 in 2026.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Net sales increased by 13133 million year over year.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={
            "contract_type": "numeric",
            "target_values": [13133],
            "operands": [94930, 81797],  # Neither 94930 nor 81797 is in retained text!
        },
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_sales"},
    )

    assert result["is_grounded_correct"] is False
    assert "numeric claims" in result["reason"] or "claim_unsupported" in result["reason"]


def test_derived_calculation_partial_operands_retained_fails():
    """Regression: When only one of two required operands is retained, derived result must fail.

    Contract requires operands [94930, 81797] to derive 13133.
    Retained context contains only 94930 (81797 is missing).
    The derived result 13133 must not be accepted.
    """
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_sales_partial",
        text="Total net sales were 94930 million in 2026.",
    )
    citations = [
        {
            "chunk_id": "chunk_sec_sales_partial",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 1,
            "document_id": "doc_sec_01",
            "page_number": 2,
            "text": "Total net sales were 94930 million in 2026.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Net sales increased by 13133 million year over year.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={
            "contract_type": "numeric",
            "target_values": [13133],
            "operands": [94930, 81797],  # 81797 is missing from context!
        },
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_sales_partial"},
    )

    assert result["is_grounded_correct"] is False
    assert "numeric claims" in result["reason"] or "claim_unsupported" in result["reason"]


def test_arbitrary_context_numbers_do_not_unlock_contract_targets():
    """Arbitrary numbers in context must not unlock contract target values when operands are absent.

    When contract has no operands specified, arbitrary numbers like 50 or 1200
    must not unlock target 13133.
    """
    retained_item = _make_retained_evidence_item(
        evidence_id="chunk_sec_misc",
        text="In 2026, the company operated in 50 countries with 1200 partners.",
    )
    citations = [
        {
            "chunk_id": "chunk_sec_misc",
            "document_filename": "SEC-Form-10Q.pdf",
            "document_version": 1,
            "document_id": "doc_sec_01",
            "page_number": 2,
            "text": "In 2026, the company operated in 50 countries with 1200 partners.",
        }
    ]

    result = evaluate_grounded_answer(
        answer="Net sales increased by 13133 million year over year.",
        citations=citations,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={
            "contract_type": "numeric",
            "target_values": [13133],
            "operands": [],  # No operands!
        },
        expected_sources=["SEC-Form-10Q.pdf"],
        expected_source_version=1,
        retained_evidence_items=[retained_item],
        ground_truth_chunk_ids={"chunk_sec_misc"},
    )

    assert result["is_grounded_correct"] is False
    assert "numeric claims" in result["reason"] or "claim_unsupported" in result["reason"]


def test_retained_sum_100_fabricated_secondary_value_999_control():
    """Control: Retained execution has SUM=100. Citation fabricates secondary_value=999.

    Citation result_value, secondary_value, and nested payloads must not introduce
    additional accepted results. The answer claiming 999 must fail grounded scoring.
    """
    hash_val = "1234567890abcdef"
    ev = SimpleNamespace(
        evidence_id="ev_agg_1",
        workspace_id="ws_01",
        document_version="1",
        structured_payload={
            "selection_hash": hash_val,
            "table_id": "survay",
            "operator": "sum",
            "target_column": "Total_Value",
            "selection_count": 5,
            "result_value": "100",  # Retained execution has 100
            "secondary_value": None,
        },
        locator={"selection_hash": hash_val, "table_id": "survay", "selection_count": 5},
    )
    # Citation fabricates secondary_value = 999
    fabricated_citation = {
        "evidence_type": "structured_aggregate",
        "table_id": "survay",
        "document_version": "1",
        "operator": "sum",
        "target_column": "Total_Value",
        "selection_count": 5,
        "selection_hash": hash_val,
        "result_value": "100",
        "secondary_value": "999",
    }

    # Direct citation validation must flag fabricated secondary_value
    cit_res = validate_citations(
        [fabricated_citation],
        retained_evidence_items=[ev],
        retained_structured_hashes={hash_val},
    )
    assert cit_res["valid"] is False
    assert any("secondary_value '999' fabricated" in e for e in cit_res["errors"])

    # Grounded answer evaluation with answer asserting the fabricated 999 must fail
    result = evaluate_grounded_answer(
        answer="The sum is 999.",
        citations=[fabricated_citation],
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "target_values": [100]},
        question_type="structured_aggregate",
        retained_evidence_items=[ev],
        retained_structured_hashes={hash_val},
    )

    assert result["is_grounded_correct"] is False
    assert (
        "secondary_value '999' fabricated" in result["reason"]
        or "does not assert computed result" in result["reason"]
        or "amounts" in result["reason"]
    )
