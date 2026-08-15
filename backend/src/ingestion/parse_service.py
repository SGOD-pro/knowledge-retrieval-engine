import uuid
from pathlib import Path

from ingestion.format_router import route
from schemas.models import Document


def generate_deterministic_doc_id(path: Path) -> str:
    """Generate a deterministic UUID5 for a document based on its filename."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, path.name))


def parse_file(path: Path, document_id: str | None = None) -> Document:
    document_id = document_id or generate_deterministic_doc_id(path)
    source_format, adapter = route(path)
    chunks = tuple(adapter(path, document_id))
    return Document(document_id, path.name, source_format, chunks)
