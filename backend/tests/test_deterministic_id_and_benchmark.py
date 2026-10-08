import uuid
from pathlib import Path

from ingestion.parse_service import generate_deterministic_doc_id
from run_benchmark import _expected_pages, _parse_page


def test_deterministic_doc_id():
    p1 = Path("tests/data/Workflow Documentation.docx")
    p2 = Path("other_path/Workflow Documentation.docx")   # different path, same filename
    p3 = Path("tests/data/hdfc.pdf")

    WS_A = "ws_aaa"
    WS_B = "ws_bbb"

    # ── Same filename + same workspace → identical ID (idempotent re-ingestion) ──
    id_a1 = generate_deterministic_doc_id(p1, workspace_id=WS_A)
    id_a2 = generate_deterministic_doc_id(p2, workspace_id=WS_A)
    assert id_a1 == id_a2, "same filename + same workspace must produce the same ID"
    assert isinstance(uuid.UUID(id_a1), uuid.UUID)

    # ── Same filename + DIFFERENT workspace → different ID (cross-workspace isolation) ──
    id_b1 = generate_deterministic_doc_id(p1, workspace_id=WS_B)
    assert id_a1 != id_b1, "same filename in a different workspace must produce a different ID"
    assert isinstance(uuid.UUID(id_b1), uuid.UUID)

    # ── Different filename → always different ID regardless of workspace ──
    id_pdf_a = generate_deterministic_doc_id(p3, workspace_id=WS_A)
    assert id_a1 != id_pdf_a, "different filename must produce a different ID"

    # ── Legacy (no workspace_id) → bare-filename seed — backward compat ──
    id_legacy = generate_deterministic_doc_id(p1)
    assert id_legacy != id_a1, "no-workspace call must differ from workspace-scoped call"
    assert isinstance(uuid.UUID(id_legacy), uuid.UUID)

    # ── Determinism: calling twice with the same args must return the same value ──
    assert id_a1 == generate_deterministic_doc_id(p1, workspace_id=WS_A)
    assert id_legacy == generate_deterministic_doc_id(p2)


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
            {"chunk_id": "doc1:page:6:elem:1"},
        ]
    }
    assert _expected_pages(gt) == {2, 6}
