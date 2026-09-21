"""Tests for generic verified factual extractor (zero corpus-specific rules)."""

from schemas.models import Chunk
from services.retrieval.extractor import extract_verified_fact, parse_kv_row


def test_parse_kv_row():
    text = "Department: Radiology. Room: 402. Equipment: MRI. Status: Operational."
    kv = parse_kv_row(text)
    assert kv["department"] == "Radiology"
    assert kv["room"] == "402"
    assert kv["equipment"] == "MRI"
    assert kv["status"] == "Operational"


def test_generic_tabular_lookup():
    chunk = Chunk(
        id="c1",
        document_id="doc1",
        source_format="csv",
        text="Employee_ID: E991. Department: Engineering. Base_Salary_USD: 145000. Start_Year: 2021.",
        element_type="row",
    )
    # Target: Base_Salary_USD; Filters: E991, Engineering
    ans, matched_chunk = extract_verified_fact("What is the Base_Salary_USD for employee E991 in Engineering?", [chunk])
    assert ans == "145000"
    assert matched_chunk is chunk


def test_generic_tabular_lookup_escalates_on_ambiguity():
    chunk1 = Chunk(
        id="c1",
        document_id="doc1",
        source_format="csv",
        text="Product: Widget. Color: Blue. Price: 15. Stock: 40.",
        element_type="row",
    )
    chunk2 = Chunk(
        id="c2",
        document_id="doc1",
        source_format="csv",
        text="Product: Widget. Color: Red. Price: 20. Stock: 25.",
        element_type="row",
    )
    # Ambiguous: Color not specified
    ans, matched_chunk = extract_verified_fact("What is the price of Widget?", [chunk1, chunk2])
    assert ans is None
    assert matched_chunk is None


def test_arxiv_metadata_extraction():
    chunk = Chunk(
        id="c_arxiv",
        document_id="d1",
        source_format="pdf",
        text="Paper title. arXiv: 2304.12345v1 [cs.AI]",
        element_type="paragraph",
    )
    ans, matched_chunk = extract_verified_fact("In what month and year was arXiv identifier 2304.12345 published?", [chunk])
    assert ans == "April 2023"
    assert matched_chunk is not None


def test_abbreviation_expansion_extraction():
    chunk = Chunk(
        id="c_abbr",
        document_id="d1",
        source_format="pdf",
        text="We propose Context Guided Attention (CGA) to enhance model robustness.",
        element_type="paragraph",
    )
    ans, matched_chunk = extract_verified_fact("What does CGA stand for?", [chunk])
    assert ans == "Context Guided Attention"
    assert matched_chunk is chunk
