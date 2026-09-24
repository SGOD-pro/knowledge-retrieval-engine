"""CSV adapter — row-level contextual chunking.

Each data row becomes ONE chunk whose text is a natural-language sentence:
    "Name of the Colleges: GAMC, Bangalore. Principal: 1. Professor: 23. ..."

This makes each chunk semantically self-contained and retrieval-ready.
Individual cell-level chunks are useless to a vector model (e.g. "Professor: 23"
provides no context for *which* college).
"""

import csv
import os
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

        # Explicit vector indexing chunk limit
        raw_limit = os.getenv("VECTOR_MAX_CHUNKS")
        if raw_limit is None and "CSV_MAX_CHUNKS" in os.environ:
            import logging
            logging.getLogger(__name__).warning("CSV_MAX_CHUNKS is deprecated; use VECTOR_MAX_CHUNKS instead")
            raw_limit = os.getenv("CSV_MAX_CHUNKS")
        max_vector_chunks = int(raw_limit) if raw_limit else 5000

        if len(chunks) >= max_vector_chunks:
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


def infer_schema_from_header(headers: list[str], sample_rows: list[list[str]]) -> "TableSchema":
    """Infer column data types and build TableSchema by sampling up to 200 rows."""
    from schemas.structured_table import ColumnDefinition, HeaderTopology, InferredDtype, TableSchema

    col_defs: list[ColumnDefinition] = []
    num_samples = len(sample_rows)

    for col_idx, raw_header in enumerate(headers):
        header_name = raw_header.strip() if raw_header else f"Col_{col_idx}"
        vals = [row[col_idx].strip() for row in sample_rows if col_idx < len(row)]
        non_empty = [v for v in vals if v]
        null_ratio = 1.0 - (len(non_empty) / num_samples) if num_samples > 0 else 0.0
        sample_vals = tuple(non_empty[:5])

        inferred_dtype = InferredDtype.STRING
        if non_empty:
            # Check integer
            is_int = True
            for v in non_empty:
                clean = v.replace(",", "")
                if not (clean.isdigit() or (clean.startswith("-") and clean[1:].isdigit())):
                    is_int = False
                    break
            if is_int:
                inferred_dtype = InferredDtype.INTEGER
            else:
                # Check decimal
                is_dec = True
                for v in non_empty:
                    clean = v.replace(",", "")
                    try:
                        float(clean)
                    except ValueError:
                        is_dec = False
                        break
                if is_dec:
                    inferred_dtype = InferredDtype.DECIMAL
                elif all(v.endswith("%") for v in non_empty):
                    inferred_dtype = InferredDtype.PERCENTAGE
                elif all(any(v.startswith(sym) for sym in ("$", "₹", "€", "£")) for v in non_empty):
                    inferred_dtype = InferredDtype.CURRENCY

        col_defs.append(ColumnDefinition(
            col_index=col_idx,
            name=header_name,
            inferred_dtype=inferred_dtype,
            sample_values=sample_vals,
            null_ratio=null_ratio,
        ))

    return TableSchema(
        columns=tuple(col_defs),
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )

