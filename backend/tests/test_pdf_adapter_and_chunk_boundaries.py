import pytest
from schemas.models import Chunk
from ingestion.adapters.chunk_util import merge_and_split_chunks
from ingestion.adapters.pdf_adapter import _normalize_elements


def test_merge_and_split_never_crosses_page_boundaries():
    """Verify that undersized chunks on different pages are NEVER merged."""
    c1 = Chunk(
        id="chunk-1",
        document_id="doc-1",
        source_format="pdf",
        text="Short page 1 text that has few words.",
        element_type="paragraph",
        page_number=1,
        section_path="sec_1",
    )
    c2 = Chunk(
        id="chunk-2",
        document_id="doc-1",
        source_format="pdf",
        text="Short page 2 text that also has few words.",
        element_type="paragraph",
        page_number=2,
        section_path="sec_1",
    )

    result = merge_and_split_chunks([c1, c2], min_tokens=100)
    assert len(result) == 2, f"Expected 2 separate chunks across page boundaries, got {len(result)}"
    assert result[0].page_number == 1
    assert result[0].text == "Short page 1 text that has few words."
    assert result[1].page_number == 2
    assert result[1].text == "Short page 2 text that also has few words."


def test_merge_and_split_never_crosses_section_boundaries():
    """Verify that undersized chunks with different section paths are NEVER merged."""
    c1 = Chunk(
        id="chunk-1",
        document_id="doc-1",
        source_format="pdf",
        text="Section A introductory remarks.",
        element_type="paragraph",
        page_number=1,
        section_path="section_A",
    )
    c2 = Chunk(
        id="chunk-2",
        document_id="doc-1",
        source_format="pdf",
        text="Section B introductory remarks.",
        element_type="paragraph",
        page_number=1,
        section_path="section_B",
    )

    result = merge_and_split_chunks([c1, c2], min_tokens=100)
    assert len(result) == 2, f"Expected 2 separate chunks across section boundaries, got {len(result)}"
    assert result[0].section_path == "section_A"
    assert result[1].section_path == "section_B"


def test_normalize_elements_with_odl_kids():
    """Verify that ODL parser elements dictionary is accurately converted to Chunks."""
    elements = [
        {
            "type": "heading",
            "page number": 1,
            "bounding box": [10.0, 20.0, 100.0, 40.0],
            "font": "Times-Roman",
            "font size": 20.0,
            "content": "Attention Is All You Need",
        },
        {
            "type": "paragraph",
            "page number": 1,
            "bounding box": [10.0, 50.0, 300.0, 150.0],
            "content": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.",
        },
        {
            "type": "table",
            "page number": 6,
            "bounding box": [100.0, 70.0, 500.0, 110.0],
            "content": "Table 1: Maximum path lengths for layer types. Self-Attention has O(1) sequential operations.",
        },
    ]

    chunks = _normalize_elements(elements, "doc_test_norm", workspace_id="ws_smoke")
    assert len(chunks) >= 3
    # Heading preserved
    assert chunks[0].element_type == "heading"
    assert chunks[0].page_number == 1
    assert "Attention Is All You Need" in chunks[0].text
    # Table preserved on page 6
    p6_chunks = [c for c in chunks if c.page_number == 6]
    assert len(p6_chunks) >= 1
    assert "Table 1" in p6_chunks[0].text
    assert p6_chunks[0].page_number == 6
