import pytest
from pathlib import Path
import uuid
from ingestion.parse_service import generate_deterministic_doc_id
from run_benchmark import _parse_page, _expected_pages

def test_deterministic_doc_id():
    p1 = Path("tests/data/Workflow Documentation.docx")
    p2 = Path("other_path/Workflow Documentation.docx")
    p3 = Path("tests/data/hdfc.pdf")
    
    id1 = generate_deterministic_doc_id(p1)
    id2 = generate_deterministic_doc_id(p2)
    id3 = generate_deterministic_doc_id(p3)
    
    # Same filename -> exact same ID
    assert id1 == id2
    assert isinstance(uuid.UUID(id1), uuid.UUID)
    
    # Different filename -> different ID
    assert id1 != id3

def test_citation_page_parser():
    # 1. From bounding_box
    cit1 = {"bounding_box": {"page_number": 6}}
    assert _parse_page(cit1) == 6
    
    # 2. From location_reference
    cit2 = {"location_reference": "Page: 24"}
    assert _parse_page(cit2) == 24
    
    # 3. From chunk_id format
    cit3 = {"chunk_id": "44912ed2-fa9b-4401-91c5-904110f6d81a:page:29:element:194"}
    assert _parse_page(cit3) == 29
    
    # 4. Ground truth with multiple citations
    gt = {
        "citations": [
            {"location_reference": "Page: 2"},
            {"chunk_id": "doc1:page:6:elem:1"}
        ]
    }
    assert _expected_pages(gt) == {2, 6}
