from pathlib import Path
from typing import Callable

from kre.shared.models import Chunk
from kre.ingestion_lambda.adapters import csv_adapter, docx_adapter, pdf_adapter, pptx_adapter, xlsx_adapter

SUPPORTED_FORMATS = {".pdf", ".docx", ".xlsx", ".pptx", ".csv"}
Adapter = Callable[[Path, str], list[Chunk]]


def route(path: Path) -> tuple[str, Adapter]:
    suffix = path.suffix.lower()
    adapters: dict[str, tuple[str, Adapter]] = {
        ".pdf": ("pdf", pdf_adapter.parse),
        ".docx": ("docx", docx_adapter.parse),
        ".xlsx": ("xlsx", xlsx_adapter.parse),
        ".pptx": ("pptx", pptx_adapter.parse),
        ".csv": ("csv", csv_adapter.parse),
    }
    try:
        return adapters[suffix]
    except KeyError as exc:
        raise ValueError(f"Unsupported format: {suffix or '<none>'}") from exc

