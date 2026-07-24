from pathlib import Path

from docx import Document as DocxDocument

from kre.models import Chunk


def parse(path: Path, document_id: str) -> list[Chunk]:
    document = DocxDocument(path)
    chunks: list[Chunk] = []
    section: list[str] = []
    for index, paragraph in enumerate(document.paragraphs):
        text = paragraph.text.strip()
        if not text:
            continue
        style = paragraph.style.name.lower() if paragraph.style else ""
        is_heading = style.startswith("heading")
        if is_heading:
            level = style.removeprefix("heading").strip() or "1"
            section = section[: max(0, int(level) - 1)] + [text]
        chunks.append(Chunk(
            id=f"{document_id}:p:{index}", document_id=document_id,
            source_format="docx", text=text,
            element_type="heading" if is_heading else "paragraph",
            section_path=tuple(section),
            location_reference=f"Paragraph: {index + 1}",
        ))
    return chunks
