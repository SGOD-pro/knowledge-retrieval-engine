import json
import subprocess
from pathlib import Path
from typing import Any

from kre.models import Chunk


def parse(path: Path, document_id: str, executable: str = "opendataloader-pdf") -> list[Chunk]:
    """Run opendataloader-pdf in batch mode and normalize its JSON output.

    The adapter accepts either a JSON array or an object containing `pages`.
    Keeping the subprocess boundary here prevents parser-specific details from
    leaking into the unified ingestion service.
    """
    result = subprocess.run(
        [executable, str(path), "--output-format", "json"],
        check=True, capture_output=True, text=True,
    )
    payload: Any = json.loads(result.stdout)
    pages = payload if isinstance(payload, list) else payload.get("pages", [])
    chunks: list[Chunk] = []
    for page in pages:
        page_number = int(page.get("page_number", page.get("page", 1)))
        elements = page.get("elements", [page])
        for index, element in enumerate(elements):
            text = str(element.get("text", "")).strip()
            if not text:
                continue
            box = element.get("bounding_box", element.get("bbox"))
            chunks.append(Chunk(
                id=f"{document_id}:page:{page_number}:element:{index}",
                document_id=document_id, source_format="pdf", text=text,
                element_type=element.get("element_type", "paragraph"),
                page_number=page_number, bounding_box=box,
                location_reference=f"Page: {page_number}",
            ))
    return chunks
