"""Ingestion parse service.

Parses a file into a Document, then:
1. Embeds all chunks (both fast + full embeddings).
2. Runs OKF extraction and persists to DynamoDB.

OKF extraction is fire-and-forget: failures are logged, never raised.
"""

import logging
import time
import uuid
from pathlib import Path

from ingestion.format_router import route
from schemas.models import Document

logger = logging.getLogger(__name__)


def generate_deterministic_doc_id(path: Path) -> str:
    """Generate a deterministic UUID5 for a document based on its filename."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, path.name))


def parse_file(path: Path, document_id: str | None = None) -> Document:
    """Parse a file to a Document with chunks. Does NOT embed or run OKF.

    Embedding and OKF are orchestrated by the ingestion route handler
    so they can be skipped in unit tests that only care about parsing.
    """
    t0 = time.perf_counter()
    document_id = document_id or generate_deterministic_doc_id(path)
    source_format, adapter = route(path)
    chunks = tuple(adapter(path, document_id))
    doc = Document(document_id, path.name, source_format, chunks)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "parse_service.done doc_id=%s format=%s chunks=%d latency_ms=%.2f",
        document_id,
        source_format,
        len(chunks),
        latency_ms,
    )
    return doc


def ingest_document(
    path: Path, document_id: str | None = None, provider: str = "dev"
) -> Document:
    """Full ingestion pipeline: parse → embed → OKF → return Document with embeddings.

    Use this in the /ingest route handler.
    Use parse_file() directly in unit tests.
    """
    doc = parse_file(path, document_id)
    chunks_list = list(doc.chunks)

    # Embed both columns
    try:
        from ingestion.embed_service import embed_chunks_dual

        t0 = time.perf_counter()
        embedded = embed_chunks_dual(chunks_list, provider=provider)
        logger.info(
            "parse_service.embed_done doc_id=%s latency_ms=%.2f",
            doc.id,
            (time.perf_counter() - t0) * 1000,
        )
        doc = Document(doc.id, doc.filename, doc.source_format, tuple(embedded))
    except Exception as e:
        logger.error("parse_service.embed_failed doc_id=%s error=%s", doc.id, e)

    # OKF — silent, never blocks ingestion
    try:
        from ingestion.okf_builder import build_okf

        build_okf(doc)
    except Exception as e:
        logger.error("parse_service.okf_failed doc_id=%s error=%s", doc.id, e)

    return doc
