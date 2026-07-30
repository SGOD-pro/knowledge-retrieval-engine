import csv
from pathlib import Path

from kre.shared.models import Chunk


def parse(path: Path, document_id: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    # Try reading with utf-8 first, fallback to latin-1
    content = ""
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        content = path.read_text(encoding="latin-1")

    reader = csv.reader(content.splitlines())
    headers: list[str] = []
    for row_idx, row in enumerate(reader, 1):
        if not row or not any(field.strip() for field in row):
            continue
        if row_idx == 1:
            headers = [field.strip() for field in row]
            # Store header row cells or summary row
        for col_idx, value in enumerate(row, 1):
            val_text = value.strip()
            if not val_text:
                continue
            header_name = headers[col_idx - 1] if col_idx - 1 < len(headers) else f"Col {col_idx}"
            chunks.append(Chunk(
                id=f"{document_id}:csv:r{row_idx}:c{col_idx}",
                document_id=document_id,
                source_format="csv",
                text=f"{header_name}: {val_text}" if row_idx > 1 and header_name else val_text,
                element_type="cell",
                section_path=(path.name,),
                location_reference=f"Row: {row_idx}, Col: {col_idx}",
                metadata={"row": row_idx, "col": col_idx, "header": header_name},
            ))
    return chunks
