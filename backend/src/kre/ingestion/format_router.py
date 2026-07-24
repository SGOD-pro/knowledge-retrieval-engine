from pathlib import Path
from typing import Callable

from kre.models import Chunk
from kre.ingestion.adapters import docx_adapter, pdf_adapter, pptx_adapter, xlsx_adapter

SUPPORTED_FORMATS = {".pdf", ".docx", ".xlsx", ".pptx"}
Adapter = Callable[[Path, str], list[Chunk]]


def route(path: Path) -> tuple[str, Adapter]:
    suffix = path.suffix.lower()
    adapters: dict[str, tuple[str, Adapter]] = {
        ".pdf": ("pdf", pdf_adapter.parse), ".docx": ("docx", docx_adapter.parse),
        ".xlsx": ("xlsx", xlsx_adapter.parse), ".pptx": ("pptx", pptx_adapter.parse),
    }
    try:
        return adapters[suffix]
    except KeyError as exc:
        raise ValueError(f"Unsupported format: {suffix or '<none>'}") from exc
