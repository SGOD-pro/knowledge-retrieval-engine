from pathlib import Path
from uuid import uuid4

from kre.ingestion.format_router import route
from kre.models import Document


def parse_file(path: Path, document_id: str | None = None) -> Document:
    document_id = document_id or str(uuid4())
    source_format, adapter = route(path)
    chunks = tuple(adapter(path, document_id))
    return Document(document_id, path.name, source_format, chunks)
