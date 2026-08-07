from pathlib import Path
from typing import Callable

from kre.models import Chunk
from kre.ingestion.adapters import csv_adapter, docx_adapter, pdf_adapter, pptx_adapter, xlsx_adapter

SUPPORTED_FORMATS = {".pdf", ".docx", ".xlsx", ".pptx", ".csv"}
Adapter = Callable[[Path, str], list[Chunk]]


def route(path: Path) -> tuple[str, Adapter]:
    suffix = path.suffix.lower()
    adapters: dict[str, tuple[str, Adapter]] = {
        ".pdf": ("pdf", lambda path, doc_id: __import__('kre.ingestion_lambda.adapters.pdf_adapter', fromlist=['']).parse(path, doc_id)),
        ".docx": ("docx", docx_adapter.parse),
        ".xlsx": ("xlsx", xlsx_adapter.parse),
        ".pptx": ("pptx", pptx_adapter.parse),
        ".csv": ("csv", csv_adapter.parse),
    }
    try:
        return adapters[suffix]
    except KeyError as exc:
        raise ValueError(f"Unsupported format: {suffix or '<none>'}") from exc

