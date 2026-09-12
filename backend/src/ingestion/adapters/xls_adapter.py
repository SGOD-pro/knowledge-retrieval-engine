"""XLS adapter — row-level contextual chunking for legacy Excel (.xls) files.

Each data row becomes ONE chunk whose text is a natural-language sentence:
    "District Names: Nicobars. State/UT: Andaman & Nicobar Islands. Number of Households surveyed: 882.0. ..."

This makes each chunk semantically self-contained and retrieval-ready.
"""

from pathlib import Path
import xlrd
from schemas.models import Chunk


def parse(path: Path, document_id: str, workspace_id: str = "") -> list[Chunk]:
    chunks: list[Chunk] = []
    workbook = xlrd.open_workbook(str(path))

    for sheet in workbook.sheets():
        if sheet.nrows <= 1 or sheet.ncols == 0:
            continue

        # Row 0 is assumed to be the header
        raw_headers = sheet.row_values(0)
        headers = [str(h).strip().replace("\n", " ") for h in raw_headers]

        for row_idx in range(1, sheet.nrows):
            row_vals = sheet.row_values(row_idx)
            if not any(str(v).strip() for v in row_vals):
                continue

            parts = []
            for col_idx, val in enumerate(row_vals):
                val_str = str(val).strip()
                if not val_str or val_str == "None":
                    continue
                # Format floating integers cleanly (e.g. 882.0 -> 882 if whole number)
                if isinstance(val, float) and val.is_integer():
                    val_str = str(int(val))
                header_name = (
                    headers[col_idx]
                    if col_idx < len(headers) and headers[col_idx]
                    else f"Col_{col_idx + 1}"
                )
                parts.append(f"{header_name}: {val_str}")

            if not parts:
                continue

            sentence = ". ".join(parts) + "."
            chunks.append(
                Chunk(
                    id=f"{document_id}:xls:{sheet.name}:row{row_idx}",
                    document_id=document_id,
                    source_format="xls",
                    text=sentence,
                    element_type="row",
                    section_path=(sheet.name,),
                    location_reference=f"Sheet: {sheet.name}, Row: {row_idx + 1}",
                    metadata={"sheet": sheet.name, "row": row_idx + 1, "headers": headers[:20]},
                    workspace_id=workspace_id,
                )
            )

    return chunks
