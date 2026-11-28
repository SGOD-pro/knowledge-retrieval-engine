#!/usr/bin/env python3
"""Manifest-driven Ingestion and Verification Procedure for Mixed-180 Workspace.

Features:
1. Workspace scoping: Ingests into target workspace (default: ws_mixed_180_baseline).
   Refuses ws_fresh_benchmark unless explicit override is provided.
2. Manifest validation: Validates all 17 canonical corpus files against kre_shuffled_documents.json.
3. Offline dry-run / smoke-test: Validates hashes, parses all 17 files, computes exact chunks (12,114)
   and data rows (87,045 across 4 CSVs) with zero remote writes and zero model calls.
4. Resumable & idempotent vector ingestion: Reuses existing chunk embeddings and skips points
   already landed in Qdrant Cloud.
5. Workspace ownership & snapshots: Registers normal workspace ownership in WorkspacesRepository
   and authoritative snapshots in WorkspaceBoundaryRepository.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any
import uuid

# Ensure backend/src and backend are in sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
SRC_DIR = BACKEND_DIR / "src"
ROOT_DIR = BACKEND_DIR.parent

sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from config import settings
from db.table_store import get_shared_table_store
from db.workspace_boundary_repo import (
    DocumentSnapshot,
    WorkbookSheetInventory,
    get_workspace_boundary_repo,
)
from schemas.contracts.boundary import SheetInventoryEntry
from ingestion.csv_table_ingester import ingest_csv_to_table_store
from ingestion.format_router import route
from ingestion.parse_service import generate_deterministic_doc_id, parse_file
from modules.documents.documents_repository import DocumentsRepository
from modules.workspaces.workspaces_repository import WorkspacesRepository
from schemas.models import Chunk, Document

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingest_mixed_180")

# 4 CSV configurations: (filename, table_name, expected_rows)
CSV_TABLE_CONFIGS = [
    ("survay.csv", "survay", 60255),
    (
        "machine-readable-business-employment-data-mar-2026-quarter.csv",
        "machine_readable_business_employment_data_mar_2026_quarter",
        26059,
    ),
    (
        "rs_status_bill_passed_assent-1952-2016.csv",
        "rs_status_bill_passed_assent_1952_2016",
        727,
    ),
    ("MO_Post_2024_25.csv", "mo_post_2024_25", 4),
]


def compute_file_sha256(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def load_manifest_files(
    manifest_path: Path,
    staged_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Path], dict[str, str]]:
    """Validate all files from manifest exist and match expected SHA-256 hashes."""
    if not manifest_path.exists() and (ROOT_DIR / manifest_path).exists():
        manifest_path = ROOT_DIR / manifest_path
    if not manifest_path.exists():
        raise FileNotFoundError(f"Corpus manifest not found: {manifest_path}")

    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = raw_manifest.get("manifest", [])
    if not entries:
        raise ValueError(f"Corpus manifest has no entries: {manifest_path}")

    file_paths: dict[str, Path] = {}
    file_hashes: dict[str, str] = {}

    for entry in entries:
        fname = entry["filename"]
        expected_hash = entry["sha256"]
        p: Path | None = None

        if staged_dir and (staged_dir / fname).exists():
            p = staged_dir / fname
        else:
            candidates = list(ROOT_DIR.glob(f"**/{fname}"))
            if candidates:
                p = candidates[0]

        if not p or not p.exists():
            raise FileNotFoundError(f"Manifest document '{fname}' not found in staged dir or repository.")

        actual_hash = compute_file_sha256(p)
        if actual_hash != expected_hash:
            raise ValueError(
                f"Hash mismatch for '{fname}': expected {expected_hash}, got {actual_hash}"
            )

        file_paths[fname] = p
        file_hashes[fname] = actual_hash

    return raw_manifest, file_paths, file_hashes


def extract_excel_sheet_inventory(xls_path: Path, doc_ver: str) -> WorkbookSheetInventory | None:
    """Extract authoritative sheet inventory for Excel workbooks."""
    try:
        import xlrd

        workbook = xlrd.open_workbook(str(xls_path))
        entries: list[SheetInventoryEntry] = []
        for sheet in workbook.sheets():
            status = "populated" if sheet.nrows > 1 else ("empty" if sheet.nrows == 0 else "unknown")
            header_only = sheet.nrows == 1
            data_rows = max(0, sheet.nrows - 1)
            entries.append(
                SheetInventoryEntry(
                    name=sheet.name,
                    status=status,
                    data_rows=data_rows,
                    header_only=header_only,
                )
            )
        return WorkbookSheetInventory(version=doc_ver, complete=True, sheets=tuple(entries))
    except Exception as e:
        logger.warning("Could not extract sheet inventory for %s: %s", xls_path.name, e)
        return None


def run_dry_run_validation(
    file_paths: dict[str, Path],
    file_hashes: dict[str, str],
    workspace_id: str,
) -> dict[str, Any]:
    """Offline dry run: parse chunks and CSV rows with zero remote writes or model calls."""
    logger.info("Running offline dry run validation for %d files...", len(file_paths))

    chunks_by_file: dict[str, int] = {}
    total_chunks = 0
    total_rows = 0
    rows_by_table: dict[str, int] = {}

    for fname, p in sorted(file_paths.items()):
        fmt, adapter = route(p)
        doc_id = generate_deterministic_doc_id(fname, workspace_id=workspace_id)
        parsed_chunks = adapter(p, doc_id)
        chunk_count = len(parsed_chunks)
        chunks_by_file[fname] = chunk_count
        total_chunks += chunk_count

    import csv

    for fname, tname, expected_rows in CSV_TABLE_CONFIGS:
        p = file_paths.get(fname)
        if not p:
            continue
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            row_count = sum(1 for _ in reader)
            rows_by_table[tname] = row_count
            total_rows += row_count
            if row_count != expected_rows:
                raise ValueError(
                    f"Row count mismatch for '{fname}' ({tname}): expected {expected_rows}, got {row_count}"
                )

    report = {
        "workspace_id": workspace_id,
        "dry_run": True,
        "files_verified": len(file_paths),
        "total_chunks": total_chunks,
        "chunks_by_file": chunks_by_file,
        "total_rows": total_rows,
        "rows_by_table": rows_by_table,
        "remote_writes_performed": 0,
        "paid_calls_performed": 0,
        "status": "DRY_RUN_PASSED",
    }
    return report


def get_existing_qdrant_points(client: Any, collection_name: str, point_ids: list[str]) -> set[str]:
    """Query Qdrant collection to retrieve existing point IDs."""
    existing = set()
    if not point_ids or client is None:
        return existing
    try:
        batch_size = 100
        for i in range(0, len(point_ids), batch_size):
            chunk_batch = point_ids[i : i + batch_size]
            points = client.retrieve(
                collection_name=collection_name,
                ids=chunk_batch,
                with_payload=False,
                with_vectors=False,
            )
            for p in points:
                existing.add(str(p.id))
    except Exception as e:
        logger.warning("Could not query existing Qdrant points (%s); will attempt write", e)
    return existing


def embed_chunks_safe(chunks: list[Chunk], provider: str = "prod") -> list[Chunk]:
    """Embed chunks while reusing already populated embeddings to prevent duplicate provider calls."""
    needs_embedding: list[tuple[int, Chunk]] = []
    for idx, c in enumerate(chunks):
        if (
            not c.embedding_fast
            or not c.embedding_full
            or len(c.embedding_fast) != 384
            or len(c.embedding_full) != 1024
        ):
            needs_embedding.append((idx, c))

    if not needs_embedding:
        logger.info("Reusing existing embeddings for all %d chunks", len(chunks))
        return list(chunks)

    logger.info("Computing embeddings for %d of %d chunks", len(needs_embedding), len(chunks))
    from ingestion.embed_service import embed_chunks_dual

    missing_chunks = [c for _, c in needs_embedding]
    embedded_missing = embed_chunks_dual(missing_chunks, provider=provider)

    result = list(chunks)
    for (idx, _), embedded in zip(needs_embedding, embedded_missing):
        result[idx] = embedded
    return result


def ingest_mixed_180_corpus(
    workspace_id: str = "ws_mixed_180_baseline",
    staged_dir: Path | None = None,
    manifest_path: Path | None = None,
    dry_run: bool = False,
    force_historical_override: bool = False,
) -> dict[str, Any]:
    """Ingest all 17 canonical corpus documents into ws_mixed_180_baseline."""
    if workspace_id == "ws_fresh_benchmark" and not force_historical_override:
        raise ValueError(
            "Refusing to overwrite historical baseline ws_fresh_benchmark. "
            "Use target workspace ws_mixed_180_baseline."
        )

    mf_path = manifest_path or (ROOT_DIR / "data" / "kre_shuffled_documents.json")
    raw_manifest, file_paths, file_hashes = load_manifest_files(mf_path, staged_dir=staged_dir)

    if dry_run:
        return run_dry_run_validation(file_paths, file_hashes, workspace_id)

    # Initialize live repositories
    boundary_repo = get_workspace_boundary_repo()
    store = get_shared_table_store()
    doc_repo = DocumentsRepository()
    ws_repo = WorkspacesRepository()

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    results: dict[str, Any] = {
        "workspace_id": workspace_id,
        "table_store_type": type(store).__name__,
        "boundary_repo_type": type(boundary_repo).__name__,
        "tables": {},
        "snapshots": {},
        "documents": {},
    }

    # 1. Ingest 4 CSVs into TableStore
    for fname, table_name, expected_rows in CSV_TABLE_CONFIGS:
        fpath = file_paths[fname]
        doc_id = generate_deterministic_doc_id(fname, workspace_id=workspace_id)
        fhash = file_hashes[fname]

        # Check if table already complete in TableStore
        existing_manifest = store.get_ingestion_manifest(doc_id, workspace_id)
        if (
            existing_manifest
            and existing_manifest.get("coverage_complete") is True
            and existing_manifest.get("persisted_rows") == expected_rows
        ):
            logger.info("Table '%s' already fully persisted in TableStore (%d rows), skipping stream", table_name, expected_rows)
            res_persisted = expected_rows
            res_coverage = True
            res_doc_ver = existing_manifest.get("document_version", fhash[:32])
        else:
            logger.info("Ingesting table '%s' (%s) into TableStore...", table_name, fname)
            res = ingest_csv_to_table_store(
                path=fpath,
                document_id=doc_id,
                workspace_id=workspace_id,
                store=store,
                table_name=table_name,
            )
            res_persisted = res.persisted_rows
            res_coverage = res.coverage_complete
            res_doc_ver = res.document_version

        # Read back verification
        agg_val, full_count, row_ids, idxs, digest = store.execute_single_pass_query(
            table_id=table_name,
            workspace_id=workspace_id,
            column_index=None,
            op=None,
        )
        if full_count != expected_rows:
            raise ValueError(
                f"Table '{table_name}' complete read mismatch: expected {expected_rows}, got {full_count}"
            )

        results["tables"][table_name] = {
            "document_id": doc_id,
            "document_version": res_doc_ver,
            "persisted_rows": res_persisted,
            "coverage_complete": res_coverage,
            "verified_count": full_count,
        }

    # 2. Register DocumentSnapshots in WorkspaceBoundaryRepository
    for fname, fpath in file_paths.items():
        doc_id = generate_deterministic_doc_id(fname, workspace_id=workspace_id)
        fhash = file_hashes[fname]
        doc_ver = fhash[:32]
        suffix = fpath.suffix.replace(".", "").lower()

        sheet_inv = None
        if suffix in ("xls", "xlsx"):
            sheet_inv = extract_excel_sheet_inventory(fpath, doc_ver)

        if suffix == "csv":
            matching_cfg = next((c for c in CSV_TABLE_CONFIGS if c[0] == fname), None)
            persisted = matching_cfg[2] if matching_cfg else 1
            total = persisted
        else:
            persisted = 1
            total = 1

        snapshot = DocumentSnapshot(
            workspace_id=workspace_id,
            document_id=doc_id,
            source=fname,
            version=doc_ver,
            format=suffix,
            incomplete_coverage_flag=False,
            exclusions=(),
            persisted_rows=persisted,
            total_rows=total,
            created_at=now_iso,
            sheet_inventory=sheet_inv,
        )
        boundary_repo.save_document_snapshot(snapshot)
        results["snapshots"][fname] = {
            "document_id": doc_id,
            "version": doc_ver,
            "format": suffix,
            "persisted_rows": persisted,
            "has_sheet_inventory": sheet_inv is not None,
        }

    # 3. Vector Ingestion with Resumption
    for fname, fpath in file_paths.items():
        doc_id = generate_deterministic_doc_id(fname, workspace_id=workspace_id)
        suffix = fpath.suffix.replace(".", "").lower()

        logger.info("Parsing %s for vector ingestion...", fname)
        parsed_doc = parse_file(fpath, document_id=doc_id, filename=fname, workspace_id=workspace_id)
        raw_chunks = list(parsed_doc.chunks)

        # Check existing points in Qdrant
        expected_point_ids = [
            str(uuid.uuid5(uuid.NAMESPACE_OID, str(c.id))) for c in raw_chunks
        ]
        existing_point_ids = get_existing_qdrant_points(
            doc_repo.qclient, doc_repo.collection_name, expected_point_ids
        )
        missing_count = len(expected_point_ids) - len(existing_point_ids)

        if missing_count == 0 and len(expected_point_ids) > 0:
            logger.info("All %d points for %s already exist in Qdrant, skipping vector upsert", len(expected_point_ids), fname)
            embedded_chunks = raw_chunks
        else:
            logger.info("%d of %d points missing in Qdrant for %s", missing_count, len(expected_point_ids), fname)
            embedded_chunks = embed_chunks_safe(raw_chunks, provider="prod")

        doc = Document(
            id=doc_id,
            filename=fname,
            source_format=parsed_doc.source_format,
            chunks=tuple(embedded_chunks),
            workspace_id=workspace_id,
            incomplete_coverage_flag=getattr(parsed_doc, "incomplete_coverage_flag", False),
            exclusions=getattr(parsed_doc, "exclusions", ()),
        )

        # Persist document metadata and missing Qdrant points
        doc_repo.save(doc)

        # Register normal workspace ownership
        raw_bytes = fpath.read_bytes()
        ws_repo.add_document_to_workspace(
            workspace_id=workspace_id,
            document=doc,
            raw_bytes=raw_bytes,
            size_str=f"{len(raw_bytes)/1024:.1f} KB",
        )
        results["documents"][fname] = {
            "document_id": doc_id,
            "chunks_total": len(embedded_chunks),
            "points_existed": len(existing_point_ids),
            "points_upserted": missing_count,
        }

    results["status"] = "INGESTION_COMPLETE"
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Mixed-180 Benchmark Workspace.")
    parser.add_argument(
        "--workspace-id",
        default="ws_mixed_180_baseline",
        help="Target workspace ID (default: ws_mixed_180_baseline)",
    )
    parser.add_argument(
        "--staged-dir",
        default=None,
        help="Directory containing the staged 17 files",
    )
    parser.add_argument(
        "--corpus-manifest",
        default=None,
        help="Path to kre_shuffled_documents.json",
    )
    parser.add_argument(
        "--dry-run",
        "--smoke-test",
        action="store_true",
        dest="dry_run",
        help="Run offline dry run without remote model or database writes",
    )
    parser.add_argument(
        "--force-historical-override",
        action="store_true",
        help="Allow writing into ws_fresh_benchmark (disabled by default)",
    )

    args = parser.parse_args()
    staged_p = Path(args.staged_dir) if args.staged_dir else None
    manifest_p = Path(args.corpus_manifest) if args.corpus_manifest else None

    res = ingest_mixed_180_corpus(
        workspace_id=args.workspace_id,
        staged_dir=staged_p,
        manifest_path=manifest_p,
        dry_run=args.dry_run,
        force_historical_override=args.force_historical_override,
    )
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
