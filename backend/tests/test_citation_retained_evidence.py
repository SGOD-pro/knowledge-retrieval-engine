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
