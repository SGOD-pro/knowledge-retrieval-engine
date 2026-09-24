"""Streaming CSV table ingester with durable checkpoints and versioned persistence.

Persists complete structured tabular data to TableStore in bounded atomic batches,
supporting resumability, idempotent replay, and atomic version publication.
"""

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import logging
from pathlib import Path
from typing import Any
import uuid

from db.table_store.base import TableStore
from ingestion.adapters.csv_adapter import infer_schema_from_header
from schemas.structured_table import TableCell, TableRow, TableSchema

logger = logging.getLogger(__name__)

PARSER_VERSION = "1.0.0"


@dataclass
class CsvIngestionResult:
    document_id: str
    document_version: str
    table_id: str
    workspace_id: str
    source_rows: int
    attempted_rows: int
    persisted_rows: int
    rejected_rows: int
    rejection_reasons: list[str]
    content_hash: str
    parser_version: str
    schema_version: str
    processing_finished: bool
    coverage_complete: bool
    checkpoint_batch: int
    completed_at_utc: str | None


class IngestionIncompleteError(RuntimeError):
    """Raised when a query requires coverage_complete but manifest shows incomplete coverage."""

    def __init__(self, table_id: str, manifest: dict[str, Any]) -> None:
        super().__init__(
            f"Table '{table_id}' data coverage is incomplete: "
            f"source={manifest.get('source_rows')} persisted={manifest.get('persisted_rows')} "
            f"rejected={manifest.get('rejected_rows')} coverage_complete={manifest.get('coverage_complete')}"
        )
        self.table_id = table_id
        self.manifest = manifest


class StructuredIngestionFailure(RuntimeError):
    """Raised for unrecoverable storage errors during ingestion."""


def _compute_streamed_hash(path: Path) -> str:
    """Compute SHA-256 of file in 64KB blocks without reading entire file into memory."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def _compute_schema_version(schema: TableSchema) -> str:
    parts = []
    for c in schema.columns:
        parts.append(f"{c.col_index}:{c.name}:{c.inferred_dtype.value}")
    return hashlib.sha256(";".join(parts).encode()).hexdigest()[:16]


def ingest_csv_to_table_store(
    path: Path,
    document_id: str,
    workspace_id: str,
    store: TableStore,
    batch_size: int = 99,
    force_reingest: bool = False,
    allow_rejections: bool = False,
    exclusion_policy: str | None = None,
) -> CsvIngestionResult:
    """Ingest CSV file into TableStore using streaming, checkpoints, and versioned keys."""
    if not workspace_id:
        raise ValueError("workspace_id must not be empty for ingestion")

    # 1. Streamed content hash
    content_hash = _compute_streamed_hash(path)
    document_version = content_hash[:32]
    table_id = path.stem.lower()

    # 2. Sample file to infer schema
    sample_rows: list[list[str]] = []
    headers: list[str] = []

    # Detect encoding
    encoding = "utf-8-sig"
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            f.readline()
    except UnicodeDecodeError:
        encoding = "latin-1"

    with path.open("r", encoding=encoding, newline="") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i == 0:
                headers = [h.strip().replace("\n", " ") for h in row]
            else:
                if row and any(cell.strip() for cell in row):
                    sample_rows.append(row)
                if len(sample_rows) >= 200:
                    break

    schema = infer_schema_from_header(headers, sample_rows)
    schema_version = _compute_schema_version(schema)

    # 3. Check for existing manifest (idempotency check)
    existing_manifest = store.get_ingestion_manifest(document_id, workspace_id, version=document_version)
    if not existing_manifest:
        # Check active manifest
        act_man = store.get_ingestion_manifest(document_id, workspace_id)
        if act_man and act_man.get("document_version") == document_version:
            existing_manifest = act_man

    if not force_reingest and existing_manifest:
        if (
            existing_manifest.get("content_hash") == content_hash
            and existing_manifest.get("parser_version") == PARSER_VERSION
            and existing_manifest.get("schema_version") == schema_version
            and existing_manifest.get("processing_finished")
        ):
            logger.info("ingest_csv: idempotent skip for doc_id=%s version=%s",
                        document_id, document_version)
            return CsvIngestionResult(
                document_id=document_id,
                document_version=document_version,
                table_id=table_id,
                workspace_id=workspace_id,
                source_rows=int(existing_manifest.get("source_rows", 0)),
                attempted_rows=int(existing_manifest.get("attempted_rows", 0)),
                persisted_rows=int(existing_manifest.get("persisted_rows", 0)),
                rejected_rows=int(existing_manifest.get("rejected_rows", 0)),
                rejection_reasons=list(existing_manifest.get("rejection_reasons", [])),
                content_hash=content_hash,
                parser_version=PARSER_VERSION,
                schema_version=schema_version,
                processing_finished=bool(existing_manifest.get("processing_finished", False)),
                coverage_complete=bool(existing_manifest.get("coverage_complete", False)),
                checkpoint_batch=int(existing_manifest.get("checkpoint_batch", 0)),
                completed_at_utc=existing_manifest.get("completed_at_utc"),
            )

    # 4. Check for in-progress checkpoint to resume
    resume_batch = 0
    attempted_rows = 0
    persisted_rows = 0
    rejected_rows = 0
    rejection_reasons: list[str] = []

    if not force_reingest:
        cp = store.get_checkpoint(document_id, workspace_id, document_version)
        if (
            cp
            and cp.get("content_hash") == content_hash
            and cp.get("parser_version") == PARSER_VERSION
            and cp.get("schema_version") == schema_version
        ):
            resume_batch = int(cp.get("checkpoint_batch", 0))
            attempted_rows = int(cp.get("attempted_rows", 0))
            persisted_rows = int(cp.get("persisted_rows", 0))
            rejected_rows = int(cp.get("rejected_rows", 0))
            logger.info("ingest_csv: resuming from checkpoint_batch=%d for doc_id=%s",
                        resume_batch, document_id)

    # 5. Store versioned schema item
    store.store_schema(
        table_id=table_id,
        workspace_id=workspace_id,
        schema=schema,
        document_id=document_id,
        document_version=document_version,
        parser_version=PARSER_VERSION,
        schema_version=schema_version,
    )

    # 6. Stream and persist rows in batches of at most batch_size (<=99)
    current_batch_index = 0
    batch_rows: list[TableRow] = []
    source_row_count = 0
    skip_rows_up_to = resume_batch * batch_size

    with path.open("r", encoding=encoding, newline="") as f:
        reader = csv.reader(f)
        for row_idx, row in enumerate(reader):
            if row_idx == 0:
                continue  # header
            if not row or not any(c.strip() for c in row):
                continue  # skip completely blank line

            source_row_count += 1

            if source_row_count <= skip_rows_up_to:
                continue

            attempted_rows += 1

            # Parse TableRow
            cells = []
            parse_error = None
            try:
                for col_idx, col_def in enumerate(schema.columns):
                    raw_val = row[col_idx] if col_idx < len(row) else ""
                    raw_str = raw_val.strip()

                    norm_val = None
                    if raw_str:
                        if col_def.inferred_dtype in ("decimal", "integer"):
                            try:
                                clean_val = raw_str.replace(",", "")
                                norm_val = Decimal(clean_val)
                            except Exception:
                                norm_val = raw_str
                        else:
                            norm_val = raw_str

                    cells.append(TableCell(
                        cell_id=f"{table_id}:{source_row_count}:{col_idx}",
                        row_index=source_row_count,
                        col_index=col_idx,
                        coordinate=f"R{source_row_count}C{col_idx + 1}",
                        raw_value=raw_val if raw_val != "" else None,
                        normalized_value=norm_val,
                        inferred_dtype=col_def.inferred_dtype,
                        unit=col_def.unit,
                    ))
            except Exception as e:
                parse_error = str(e)

            if parse_error:
                rejected_rows += 1
                if len(rejection_reasons) < 100:
                    rejection_reasons.append(f"Row {source_row_count}: {parse_error}")
                continue

            # Stable row_id
            row_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{table_id}:{source_row_count}"))
            batch_rows.append(TableRow(
                row_id=row_id,
                row_index=source_row_count,
                cells=tuple(cells),
                is_header=False,
            ))

            if len(batch_rows) >= batch_size:
                current_batch_index = (source_row_count // batch_size)
                persisted_rows += len(batch_rows)
                try:
                    store.write_row_batch_with_checkpoint(
                        table_id=table_id,
                        workspace_id=workspace_id,
                        document_id=document_id,
                        version=document_version,
                        rows=batch_rows,
                        checkpoint_batch=current_batch_index,
                        source_rows=source_row_count,
                        attempted_rows=attempted_rows,
                        persisted_rows=persisted_rows,
                        rejected_rows=rejected_rows,
                        parser_version=PARSER_VERSION,
                        schema_version=schema_version,
                        content_hash=content_hash,
                    )
                except Exception as e:
                    logger.error("Failed to write batch %d for table %s: %s",
                                 current_batch_index, table_id, e)
                    raise StructuredIngestionFailure(f"Failed to persist batch {current_batch_index}: {e}") from e
                batch_rows.clear()

        # Flush remaining rows
        if batch_rows:
            current_batch_index += 1
            persisted_rows += len(batch_rows)
            try:
                store.write_row_batch_with_checkpoint(
                    table_id=table_id,
                    workspace_id=workspace_id,
                    document_id=document_id,
                    version=document_version,
                    rows=batch_rows,
                    checkpoint_batch=current_batch_index,
                    source_rows=source_row_count,
                    attempted_rows=attempted_rows,
                    persisted_rows=persisted_rows,
                    rejected_rows=rejected_rows,
                    parser_version=PARSER_VERSION,
                    schema_version=schema_version,
                    content_hash=content_hash,
                )
            except Exception as e:
                logger.error("Failed to write final batch for table %s: %s", table_id, e)
                raise StructuredIngestionFailure(f"Failed to persist final batch: {e}") from e
            batch_rows.clear()

    # Total rows in source
    source_rows = max(source_row_count, attempted_rows + skip_rows_up_to)
    processing_finished = True
    # Rejection policy: coverage_complete is True ONLY when rejected_rows == 0
    coverage_complete = (processing_finished and (rejected_rows == 0))
    now = datetime.now(timezone.utc).isoformat()

    manifest_dict = {
        "table_id": table_id,
        "document_id": document_id,
        "workspace_id": workspace_id,
        "document_version": document_version,
        "source_rows": source_rows,
        "attempted_rows": attempted_rows,
        "persisted_rows": persisted_rows,
        "rejected_rows": rejected_rows,
        "rejection_reasons": rejection_reasons,
        "content_hash": content_hash,
        "parser_version": PARSER_VERSION,
        "schema_version": schema_version,
        "processing_finished": processing_finished,
        "coverage_complete": coverage_complete,
        "checkpoint_batch": current_batch_index,
        "completed_at_utc": now,
        "allow_rejections": allow_rejections,
        "exclusion_policy": exclusion_policy,
    }

    # 7. Atomic publication of active version
    prev_active_version = store.get_active_version(table_id, workspace_id)
    store.publish_active_version(
        table_id=table_id,
        workspace_id=workspace_id,
        document_id=document_id,
        version=document_version,
        manifest=manifest_dict,
    )

    # 8. GC old version if replaced
    if prev_active_version and prev_active_version != document_version:
        logger.info("Pruning old version %s for table %s", prev_active_version, table_id)
        store.invalidate_document_version(document_id, workspace_id, prev_active_version)

    logger.info("CSV ingestion complete: table=%s doc=%s source=%d persisted=%d rejected=%d complete=%s",
                table_id, document_id, source_rows, persisted_rows, rejected_rows, coverage_complete)

    return CsvIngestionResult(
        document_id=document_id,
        document_version=document_version,
        table_id=table_id,
        workspace_id=workspace_id,
        source_rows=source_rows,
        attempted_rows=attempted_rows,
        persisted_rows=persisted_rows,
        rejected_rows=rejected_rows,
        rejection_reasons=rejection_reasons,
        content_hash=content_hash,
        parser_version=PARSER_VERSION,
        schema_version=schema_version,
        processing_finished=processing_finished,
        coverage_complete=coverage_complete,
        checkpoint_batch=current_batch_index,
        completed_at_utc=now,
    )
