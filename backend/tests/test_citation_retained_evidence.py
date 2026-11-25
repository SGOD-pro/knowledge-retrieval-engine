"""Phase 4: Structured citation validation against retained execution evidence.

These tests verify that validate_citations rejects structured-aggregate citations
whose selection_hash was not produced by the pipeline's actual query execution.

A citation with an invented table_id, fake document_version, arbitrary operator,
or an unrecognised selection_hash must FAIL when retained_structured_hashes is provided.
"""
import pytest

from services.evaluation.benchmark_scorer import validate_citations


def _real_citation(selection_hash: str = "abcdef1234567890") -> dict:
    return {
        "evidence_type": "structured_aggregate",
        "table_id": "survay",
        "document_version": "a1b2c3d4e5f60001",
        "operator": "compare",
        "target_column": "Value_NZD",
        "selection_count": 2,
        "selection_hash": selection_hash,
        "total_rows_in_table": 60255,
    }


# ---------------------------------------------------------------------------
# When retained_structured_hashes is None (backwards compat), pass on valid shape
# ---------------------------------------------------------------------------


def test_structured_citation_no_retained_set_passes_valid_shape():
    """Without retained_structured_hashes, any well-formed structured citation passes."""
    cit = _real_citation("abcdef1234567890")
    result = validate_citations([cit], retained_structured_hashes=None)
    assert result["valid"] is True


# ---------------------------------------------------------------------------
# Fabricated citations must fail when retained set is provided
# ---------------------------------------------------------------------------


def test_fabricated_hash_fails_when_retained_set_provided():
    """A selection_hash not in retained_structured_hashes is a fabricated citation — must fail."""
    cit = _real_citation("0000000000000000")  # arbitrary invented hash
    real_hashes = {"abcdef1234567890", "1111111111111111"}
    result = validate_citations([cit], retained_structured_hashes=real_hashes)
    assert result["valid"] is False
    assert any("not in retained execution evidence" in e for e in result["errors"])


def test_nonexistent_table_with_arbitrary_hash_fails():
    """A citation with a non-existent table_id and random hash must fail when hashes are retained."""
    cit = {
        "evidence_type": "structured_aggregate",
        "table_id": "nonexistent_table",
        "document_version": "fakeverXXXXXXXXXX",
        "operator": "invented_op",
        "target_column": "SomeColumn",
        "selection_count": 999,
        "selection_hash": "abcdef1234567891",  # 16 chars but not in retained set
    }
    retained = {"realexechash0001"}
    result = validate_citations([cit], retained_structured_hashes=retained)
    assert result["valid"] is False


def test_real_hash_in_retained_set_passes():
    """A structured citation whose hash is in retained_structured_hashes must pass."""
    real_hash = "abcdef1234567890"
    cit = _real_citation(real_hash)
    result = validate_citations([cit], retained_structured_hashes={real_hash})
    assert result["valid"] is True
    assert result["valid_count"] == 1


def test_hash_too_short_fails_regardless_of_retained():
    """selection_hash shorter than 16 chars must fail independent of retained_structured_hashes."""
    cit = _real_citation("abc")
    result = validate_citations([cit], retained_structured_hashes={"abc"})
    # Even if the short hash is in the retained set, the format check fails first
    assert result["valid"] is False
    assert any("invalid selection_hash" in e for e in result["errors"])


def test_missing_table_id_fails():
    """Structured citation missing table_id must fail regardless of retained set."""
    cit = {
        "evidence_type": "structured_aggregate",
        "document_version": "v1",
        "operator": "compare",
        "target_column": "Value",
        "selection_count": 2,
        "selection_hash": "abcdef1234567890",
    }
    result = validate_citations([cit], retained_structured_hashes={"abcdef1234567890"})
    assert result["valid"] is False
    assert any("missing table_id" in e for e in result["errors"])


def test_selection_count_zero_fails():
    """selection_count=0 must fail even if hash matches retained set."""
    cit = {
        "evidence_type": "structured_aggregate",
        "table_id": "survay",
        "document_version": "a1b2c3d4e5f60001",
        "operator": "compare",
        "target_column": "Value_NZD",
        "selection_count": 0,
        "selection_hash": "abcdef1234567890",
    }
    result = validate_citations([cit], retained_structured_hashes={"abcdef1234567890"})
    assert result["valid"] is False
    assert any("selection_count must be >= 1" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Mixed citations: one regular chunk citation, one structured
# ---------------------------------------------------------------------------


def test_regular_citation_unaffected_by_retained_hashes():
    """Regular chunk citations must still validate normally when retained_structured_hashes is supplied."""
    regular = {
        "chunk_id": "chunk_abc",
        "document_filename": "report.pdf",
        "page_number": 3,
    }
    retained = {"abcdef1234567890"}
    result = validate_citations([regular], retained_structured_hashes=retained)
    # chunk_id not in context_set is fine when context_chunk_ids is None
    assert result["valid"] is True


def test_mixed_valid_regular_and_valid_structured():
    """Mixed citations: regular chunk + valid structured citation both pass."""
    regular = {
        "chunk_id": "chunk_abc",
        "document_filename": "report.pdf",
        "page_number": 3,
    }
    structured = _real_citation("abcdef1234567890")
    result = validate_citations(
        [regular, structured],
        retained_structured_hashes={"abcdef1234567890"},
    )
    assert result["valid"] is True
    assert result["valid_count"] == 2


def test_mixed_valid_regular_and_fabricated_structured():
    """Mixed citations: valid regular + fabricated structured → overall invalid."""
    regular = {
        "chunk_id": "chunk_abc",
        "document_filename": "report.pdf",
        "page_number": 3,
    }
    fabricated = _real_citation("0000000000000000")  # not in retained set
    result = validate_citations(
        [regular, fabricated],
        retained_structured_hashes={"abcdef1234567890"},
    )
    assert result["valid"] is False


def test_grounded_deterministic_arithmetic_concise_and_derived_with_controls():
    """Verify concise calculations and derived values from cited retained operands, while rejecting invalid controls."""
    from services.evaluation.benchmark_scorer import evaluate_grounded_answer

    filler = " ".join(f"Line item {i} value {1000 + i}" for i in range(45))

    # 1. Correct concise subtraction (Q139 pattern: low bag-of-words overlap against verbose table)
    cid_bs = "doc-fin:page:6:element:5"
    ev_bs = [
        {
            "evidence_id": cid_bs,
            "document_id": "doc-fin",
            "document_filename": "report.pdf",
            "document_version": "v1",
            "text": f"CONDENSED BALANCE SHEETS Net assets 149,818; Prior 149,326. {filler}",
            "locator": {"page_number": 6},
        }
    ]
    cit_bs = [{"chunk_id": cid_bs, "document_id": "doc-fin", "document_filename": "report.pdf", "document_version": "v1", "page_number": 6}]
    res_sub = evaluate_grounded_answer(
        answer="492 million dollars: 149818 minus 149326",
        citations=cit_bs,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "target_values": [492.0], "operands": [149818.0, 149326.0]},
        expected_sources=["report.pdf"],
        expected_source_version="v1",
        retained_evidence_items=ev_bs,
        context_chunk_ids=[cid_bs],
        ground_truth_chunk_ids=[cid_bs],
        outcome={"executed_path": "deterministic_math"},
    )
    assert res_sub["is_grounded_correct"] is True, res_sub.get("reason")

    # 2. Correct concise percentage ratio (Q138 pattern)
    res_pct = evaluate_grounded_answer(
        answer="28.09%, calculated as 30739 / 109417 × 100",
        citations=[{"chunk_id": "doc-fin:page:4:element:3", "document_id": "doc-fin", "document_filename": "report.pdf", "document_version": "v1", "page_number": 4}],
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "target_values": [28.09], "operands": [30739.0, 109417.0]},
        expected_sources=["report.pdf"],
        expected_source_version="v1",
        retained_evidence_items=[{
            "evidence_id": "doc-fin:page:4:element:3",
            "document_id": "doc-fin",
            "document_filename": "report.pdf",
            "document_version": "v1",
            "text": f"STATEMENTS OF OPERATIONS Services 30,739; Total 109,417. {filler}",
            "locator": {"page_number": 4},
        }],
        context_chunk_ids=["doc-fin:page:4:element:3"],
        ground_truth_chunk_ids=["doc-fin:page:4:element:3"],
        outcome={"executed_path": "deterministic_math"},
    )
    assert res_pct["is_grounded_correct"] is True, res_pct.get("reason")

    # 3. Valid derived sum absent verbatim from cited sources (Q146 semantic contract pattern)
    ev_sum = [
        {"evidence_id": "xls:row1", "document_id": "doc-xls", "document_filename": "survey.xls", "document_version": "v1", "text": "District A Households surveyed: 874.", "locator": {"sheet_name": "Sheet1", "row_index": 1}},
        {"evidence_id": "xls:row2", "document_id": "doc-xls", "document_filename": "survey.xls", "document_version": "v1", "text": "District B Households surveyed: 882.", "locator": {"sheet_name": "Sheet1", "row_index": 2}},
        {"evidence_id": "xls:row3", "document_id": "doc-xls", "document_filename": "survey.xls", "document_version": "v1", "text": "District C Households surveyed: 868.", "locator": {"sheet_name": "Sheet1", "row_index": 3}},
    ]
    cit_sum = [{"chunk_id": f"xls:row{i}", "document_id": "doc-xls", "document_filename": "survey.xls", "document_version": "v1", "location_reference": f"Sheet1 row {i}"} for i in (1, 2, 3)]
    res_sum = evaluate_grounded_answer(
        answer="Total households surveyed is 2624 by adding District A (874), District B (882), and District C (868).",
        citations=cit_sum,
        is_answer_text_correct=True,
        contract_type="semantic",
        contract={"contract_type": "semantic"},
        expected_sources=["survey.xls"],
        expected_source_version="v1",
        retained_evidence_items=ev_sum,
        context_chunk_ids=["xls:row1", "xls:row2", "xls:row3"],
        ground_truth_chunk_ids=["xls:row1", "xls:row2", "xls:row3"],
    )
    assert res_sum["is_grounded_correct"] is True, res_sum.get("reason")

    # 4. Valid date-difference duration in days (Q161 pattern)
    cid_dt = "doc-csv:row2"
    ev_dt = [{
        "evidence_id": cid_dt,
        "document_id": "doc-csv",
        "document_filename": "bills.csv",
        "document_version": "v1",
        "text": "Bill XVII. Passed RS: 16/03/2016. Assent Date: 5/5/2016. Status: Assented.",
        "locator": {"row_index": 2},
    }]
    cit_dt = [{"chunk_id": cid_dt, "document_id": "doc-csv", "document_filename": "bills.csv", "document_version": "v1", "location_reference": "row 2"}]
    res_dt = evaluate_grounded_answer(
        answer="50 days, from 16 March 2016 to 5 May 2016",
        citations=cit_dt,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "target_values": [50.0], "operands": []},
        expected_sources=["bills.csv"],
        expected_source_version="v1",
        retained_evidence_items=ev_dt,
        context_chunk_ids=[cid_dt],
        ground_truth_chunk_ids=[cid_dt],
        outcome={"executed_path": "deterministic_math"},
    )
    assert res_dt["is_grounded_correct"] is True, res_dt.get("reason")

    # --- Negative Controls (all must remain REJECTED) ---
    # Control A: Wrong arithmetic result (even with executed_path=deterministic_math)
    res_wrong = evaluate_grounded_answer(
        answer="500 million dollars: 149818 minus 149326",
        citations=cit_bs,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "target_values": [492.0], "operands": [149818.0, 149326.0]},
        expected_sources=["report.pdf"],
        expected_source_version="v1",
        retained_evidence_items=ev_bs,
        context_chunk_ids=[cid_bs],
        ground_truth_chunk_ids=[cid_bs],
        outcome={"executed_path": "deterministic_math"},
    )
    assert res_wrong["is_grounded_correct"] is False

    # Control B: Missing / uncited operand (operand 149326 absent from cited evidence)
    ev_missing_op = [{
        "evidence_id": cid_bs,
        "document_id": "doc-fin",
        "document_filename": "report.pdf",
        "document_version": "v1",
        "text": f"CONDENSED BALANCE SHEETS Net assets 149,818 only. {filler}",
        "locator": {"page_number": 6},
    }]
    res_missing = evaluate_grounded_answer(
        answer="492 million dollars: 149818 minus 149326",
        citations=cit_bs,
        is_answer_text_correct=True,
        contract_type="numeric",
        expected_sources=["report.pdf"],
        expected_source_version="v1",
        retained_evidence_items=ev_missing_op,
        context_chunk_ids=[cid_bs],
        ground_truth_chunk_ids=[cid_bs],
        outcome={"executed_path": "deterministic_math"},
    )
    assert res_missing["is_grounded_correct"] is False

    # Control C: Wrong-source evidence
    res_wrong_src = evaluate_grounded_answer(
        answer="492 million dollars: 149818 minus 149326",
        citations=[{"chunk_id": cid_bs, "document_id": "doc-fin", "document_filename": "other.pdf", "document_version": "v1", "page_number": 6}],
        is_answer_text_correct=True,
        contract_type="numeric",
        expected_sources=["report.pdf"],
        expected_source_version="v1",
        retained_evidence_items=ev_bs,
        context_chunk_ids=[cid_bs],
        ground_truth_chunk_ids=[cid_bs],
        outcome={"executed_path": "deterministic_math"},
    )
    assert res_wrong_src["is_grounded_correct"] is False

    # Control D: Wrong-version evidence
    res_wrong_ver = evaluate_grounded_answer(
        answer="492 million dollars: 149818 minus 149326",
        citations=[{"chunk_id": cid_bs, "document_id": "doc-fin", "document_filename": "report.pdf", "document_version": "v2", "page_number": 6}],
        is_answer_text_correct=True,
        contract_type="numeric",
        expected_sources=["report.pdf"],
        expected_source_version="v1",
        retained_evidence_items=ev_bs,
        context_chunk_ids=[cid_bs],
        ground_truth_chunk_ids=[cid_bs],
        outcome={"executed_path": "deterministic_math"},
    )
    assert res_wrong_ver["is_grounded_correct"] is False

    # Control E: Wrong date-difference duration
    res_wrong_dt = evaluate_grounded_answer(
        answer="45 days, from 16 March 2016 to 5 May 2016",
        citations=cit_dt,
        is_answer_text_correct=True,
        contract_type="numeric",
        expected_sources=["bills.csv"],
        expected_source_version="v1",
        retained_evidence_items=ev_dt,
        context_chunk_ids=[cid_dt],
        ground_truth_chunk_ids=[cid_dt],
        outcome={"executed_path": "deterministic_math"},
    )
    assert res_wrong_dt["is_grounded_correct"] is False

    # Control F: Mathematically valid calculation using wrong row/metric operand in the same cited table
    ev_multi_metric = [{
        "evidence_id": "doc-fin:page:4:element:3",
        "document_id": "doc-fin",
        "document_filename": "report.pdf",
        "document_version": "v1",
        "text": f"STATEMENTS OF OPERATIONS Products 78,678; Services 30,739; Total 109,417. {filler}",
        "locator": {"page_number": 4},
    }]
    res_wrong_metric_op = evaluate_grounded_answer(
        answer="71.91%, calculated as 78678 / 109417 × 100",
        citations=[{"chunk_id": "doc-fin:page:4:element:3", "document_id": "doc-fin", "document_filename": "report.pdf", "document_version": "v1", "page_number": 4}],
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "target_values": [28.09], "operands": [30739.0, 109417.0]},
        expected_sources=["report.pdf"],
        expected_source_version="v1",
        retained_evidence_items=ev_multi_metric,
        context_chunk_ids=["doc-fin:page:4:element:3"],
        ground_truth_chunk_ids=["doc-fin:page:4:element:3"],
        outcome={"executed_path": "deterministic_math"},
    )
    assert res_wrong_metric_op["is_grounded_correct"] is False

    # Control G: Correct calculation accompanied by an unsupported numeric claim
    res_extra_ungrounded = evaluate_grounded_answer(
        answer="492 million dollars: 149818 minus 149326, plus 999999 bonus",
        citations=cit_bs,
        is_answer_text_correct=True,
        contract_type="numeric",
        contract={"contract_type": "numeric", "target_values": [492.0], "operands": [149818.0, 149326.0]},
        expected_sources=["report.pdf"],
        expected_source_version="v1",
        retained_evidence_items=ev_bs,
        context_chunk_ids=[cid_bs],
        ground_truth_chunk_ids=[cid_bs],
        outcome={"executed_path": "deterministic_math"},
    )
    assert res_extra_ungrounded["is_grounded_correct"] is False
