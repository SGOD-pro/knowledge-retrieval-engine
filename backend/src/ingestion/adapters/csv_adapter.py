"""CSV adapter — row-level contextual chunking.

Each data row becomes ONE chunk whose text is a natural-language sentence:
    "Name of the Colleges: GAMC, Bangalore. Principal: 1. Professor: 23. ..."

This makes each chunk semantically self-contained and retrieval-ready.
Individual cell-level chunks are useless to a vector model (e.g. "Professor: 23"
provides no context for *which* college).
"""

import csv
from pathlib import Path

from schemas.models import Chunk


def parse(path: Path, document_id: str, workspace_id: str = "") -> list[Chunk]:
    chunks: list[Chunk] = []

    # Try utf-8-sig first (handles BOM), fallback to latin-1
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
            # Clean multi-line header cells (e.g. "Sl. \nNo." → "Sl. No.")
            headers = [field.strip().replace("\n", " ") for field in row]
            continue  # Skip the header row itself — it is not a data chunk

        if len(chunks) >= 500:
            break

        # Build a natural-language sentence from all non-empty (header, value) pairs
        parts = []
        for col_idx, value in enumerate(row):
            val_text = value.strip()
            if not val_text:
                continue
            header_name = (
                headers[col_idx].strip()
                if col_idx < len(headers)
                else f"Col {col_idx + 1}"
            )
            parts.append(f"{header_name}: {val_text}")

        if not parts:
            continue

        # Join with ". " to form a readable sentence — gives the LLM full context
        sentence = ". ".join(parts) + "."

        chunks.append(
            Chunk(
                id=f"{document_id}:csv:row{row_idx}",
                document_id=document_id,
                source_format="csv",
                text=sentence,
                element_type="row",
                section_path=(path.name,),
                location_reference=f"Row {row_idx}",
                metadata={"row": row_idx, "headers": headers},
                workspace_id=workspace_id,
            )
        )

    return chunks
