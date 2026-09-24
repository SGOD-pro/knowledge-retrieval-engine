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


def generate_deterministic_doc_id(
    path_or_name: Path | str,
    workspace_id: str = "",
) -> str:
    """Generate a deterministic UUID5 for a document.

    Seed is ``f"{workspace_id}::{filename}"`` when workspace_id is provided,
    which guarantees isolation between workspaces that upload a file with the
    same name.  When workspace_id is omitted (legacy / migration fallback)
    the seed is the bare filename — identical to the previous behaviour.
    """
    name = path_or_name.name if isinstance(path_or_name, Path) else path_or_name
    seed = f"{workspace_id}::{name}" if workspace_id else name
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


def parse_file(
    path: Path,
    document_id: str | None = None,
    filename: str | None = None,
    workspace_id: str = "",
) -> Document:
    """Parse a file to a Document with chunks. Does NOT embed or run OKF.

    Embedding and OKF are orchestrated by the ingestion route handler
    so they can be skipped in unit tests that only care about parsing.
    """
    t0 = time.perf_counter()
    doc_filename = filename or path.name
    document_id = document_id or generate_deterministic_doc_id(
        doc_filename, workspace_id=workspace_id
    )
    source_format, adapter = route(path)
    chunks = tuple(adapter(path, document_id, workspace_id=workspace_id))
    doc = Document(
        id=document_id,
        filename=doc_filename,
        source_format=source_format,
        chunks=chunks,
        workspace_id=workspace_id,
    )
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
    path: Path,
    document_id: str | None = None,
    filename: str | None = None,
    provider: str = "dev",
    workspace_id: str = "",
) -> Document:
    """Full ingestion pipeline: parse → embed → OKF → return Document with embeddings.

    Use this in the /ingest route handler.
    Use parse_file() directly in unit tests.
    """
    doc = parse_file(
        path, document_id=document_id, filename=filename, workspace_id=workspace_id
    )
    chunks_list = list(doc.chunks)

    # Structured table ingestion (runs independently before embedding)
    if doc.source_format == "csv":
        try:
            from db.table_store import get_shared_table_store
            from ingestion.csv_table_ingester import ingest_csv_to_table_store

            store = get_shared_table_store()
            res = ingest_csv_to_table_store(path, doc.id, workspace_id, store)
            logger.info(
                "parse_service.structured_ingested doc_id=%s source=%d persisted=%d rejected=%d complete=%s",
                doc.id,
                res.source_rows,
                res.persisted_rows,
                res.rejected_rows,
                res.coverage_complete,
            )
        except Exception as e:
            logger.error("parse_service.structured_ingest_failed doc_id=%s error=%s", doc.id, e)

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
        doc = Document(
            id=doc.id,
            filename=doc.filename,
            source_format=doc.source_format,
            chunks=tuple(embedded),
            workspace_id=doc.workspace_id,
        )
    except Exception as e:
        logger.error("parse_service.embed_failed doc_id=%s error=%s", doc.id, e)

    # OKF — silent, never blocks ingestion
    try:
        from ingestion.okf_builder import build_okf

        build_okf(doc)
    except Exception as e:
        logger.error("parse_service.okf_failed doc_id=%s error=%s", doc.id, e)

    return doc
