import pytest
from schemas.models import Chunk
from services.retrieval.response_builder import build_citation, Citation

def test_citation_fields_completeness():
    chunk = Chunk(
        id="chunk_test_123:page:1:s0",
        document_id="doc_test_123",
        text="Attention mechanisms allow neural networks to focus on specific parts of input sequences.",
        source_format="pdf",
        page_number=1,
        element_type="paragraph",
        section_path="1. Introduction",
        bounding_box={"x1": 0.1, "y1": 0.2, "x2": 0.8, "y2": 0.5, "page_number": 1},
        location_reference="Page 1",
    )

    cit = build_citation(chunk, document_filename="attention_paper.pdf")
    cit_dict = cit.to_dict()

    # Required fields must exist in citation dict
    assert "text" in cit_dict
    assert cit_dict["text"] == chunk.text[:500]
    assert "document_filename" in cit_dict
    assert cit_dict["document_filename"] == "attention_paper.pdf"
    assert "page_number" in cit_dict
    assert cit_dict["page_number"] == 1
    assert "chunk_id" in cit_dict
    assert "document_id" in cit_dict
    assert "source_format" in cit_dict
