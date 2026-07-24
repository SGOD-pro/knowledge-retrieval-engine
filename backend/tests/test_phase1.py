from pathlib import Path

from docx import Document as DocxDocument
from openpyxl import Workbook
from pptx import Presentation

from kre.ingestion.format_router import route
from kre.ingestion.adapters.docx_adapter import parse as parse_docx
from kre.ingestion.adapters.pptx_adapter import parse as parse_pptx
from kre.ingestion.adapters.xlsx_adapter import parse as parse_xlsx
from kre.ingestion.page_index_service import rank, score
from kre.models import Chunk


def test_format_router_rejects_unsupported(tmp_path: Path):
    try:
        route(tmp_path / "file.csv")
    except ValueError as error:
        assert "Unsupported format" in str(error)
    else:
        raise AssertionError("unsupported format was accepted")


def test_docx_heading_and_paragraph(tmp_path: Path):
    path = tmp_path / "sample.docx"
    document = DocxDocument()
    document.add_heading("Policy", level=1)
    document.add_paragraph("Refunds are available within thirty days.")
    document.save(path)
    chunks = parse_docx(path, "doc")
    assert [chunk.element_type for chunk in chunks] == ["heading", "paragraph"]
    assert chunks[1].location_reference == "Paragraph: 2"


def test_xlsx_uses_computed_values(tmp_path: Path):
    path = tmp_path / "sample.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = 12
    sheet["B1"] = "=A1*2"
    workbook.calculation.fullCalcOnLoad = True
    workbook.save(path)
    chunks = parse_xlsx(path, "doc")
    assert all("=" not in chunk.text for chunk in chunks)


def test_pptx_speaker_notes_are_caption(tmp_path: Path):
    path = tmp_path / "sample.pptx"
    presentation = Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[1]).notes_slide.notes_text_frame.text = "Presenter note"
    presentation.save(path)
    chunks = parse_pptx(path, "doc")
    assert any(chunk.element_type == "caption" for chunk in chunks)


def test_pageindex_prioritizes_heading_over_footnote():
    heading = Chunk("h", "d", "pdf", "refund policy", "heading")
    footnote = Chunk("f", "d", "pdf", "refund policy", "footnote")
    assert score(heading, "refund policy") / score(footnote, "refund policy") >= 2
    assert rank([footnote, heading], "refund policy")[0] == heading
