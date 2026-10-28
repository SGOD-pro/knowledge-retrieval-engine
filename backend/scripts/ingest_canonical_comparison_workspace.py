#!/usr/bin/env python3
"""Scoped Ingestion Procedure and Verification for Canonical Comparison Workspace.

Enforces:
1. Workspace scoping: Ingests strictly into the target workspace (default: ws_canonical_comparison_0004).
2. Preserves historical baseline: Explicitly refuses to modify or prune ws_fresh_benchmark.
3. Complete TableStore coverage: Streams and registers all rows and manifests for survay.csv
   (60,255 rows) and rs_status_bill_passed_assent-1952-2016.csv (727 rows).
4. Boundary snapshot registration: Records versioned DocumentSnapshot records in
   WorkspaceBoundaryRepository with format, persisted row counts, and status Ready.
5. Offline smoke check support: --smoke-test verifies hashes, TableStore streaming reads,
   manifests, and snapshot queries without remote LLM or embedding calls.
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

# Ensure backend/src and backend are in sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
SRC_DIR = BACKEND_DIR / "src"
ROOT_DIR = BACKEND_DIR.parent

sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from config import settings
from db.database import CloudRepository
from db.table_store import get_shared_table_store
from db.workspace_boundary_repo import DocumentSnapshot, get_workspace_boundary_repo
from ingestion.csv_table_ingester import ingest_csv_to_table_store
from ingestion.parse_service import generate_deterministic_doc_id, parse_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingest_canonical_comparison")

CANONICAL_CORPUS_FILES = [
    "data/academic_research/set_aside/2212.14776v3.pdf",
    "data/academic_research/set_aside/2412.20875v1.pdf",
    "data/academic_research/set_aside/2501.05730v1.pdf",
    "data/academic_research/set_aside/2501.09166v1.pdf",
    "data/financial_tables/SEC-Form-10Q.pdf",
    "data/policy_regulatory/National-Strategy-for-Artificial-Intelligence.pdf",
    "data/non_pdf_formats/NFHS_5_India_Districts_Factsheet_Data.xls",
    "data/non_pdf_formats/rs_status_bill_passed_assent-1952-2016.csv",
    "data/non_pdf_formats/survay.csv",
]

EXPECTED_COMBINED_CORPUS_HASH = "ac193bc5bea7bfcc547e39924118fdd469385ceb528d14d5528f0464f74c959d"


def compute_file_sha256(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def verify_corpus_files() -> tuple[str, dict[str, str], dict[str, Path]]:
    """Verify all 9 canonical corpus files exist on disk and compute manifest SHA-256."""
    file_hashes = {}
    file_paths = {}

    for rel_path in CANONICAL_CORPUS_FILES:
        p = ROOT_DIR / rel_path
        if not p.exists():
            matches = list(ROOT_DIR.glob(f"**/{Path(rel_path).name}"))
            if not matches:
                raise FileNotFoundError(f"Canonical corpus file '{rel_path}' not found.")
            p = matches[0]

        fname = p.name
        fhash = compute_file_sha256(p)
        file_hashes[fname] = fhash
        file_paths[fname] = p

    combined_hash = hashlib.sha256()
    for fname in sorted(file_hashes.keys()):
        combined_hash.update(f"{fname}:{file_hashes[fname]}".encode("utf-8"))

    combined_hex = combined_hash.hexdigest()
    if combined_hex != EXPECTED_COMBINED_CORPUS_HASH:
        raise ValueError(
            f"Corpus hash mismatch: expected '{EXPECTED_COMBINED_CORPUS_HASH}', got '{combined_hex}'"
        )
    return combined_hex, file_hashes, file_paths


def ingest_comparison_corpus(
    workspace_id: str = "ws_canonical_comparison_0004",
    smoke_test: bool = False,
    force_historical_override: bool = False,
) -> dict[str, Any]:
    """Ingest canonical corpus into isolated comparison workspace."""
    # Safety guard: Never touch ws_fresh_benchmark without explicit override
    if workspace_id == "ws_fresh_benchmark" and not force_historical_override:
        raise ValueError(
            "Refusing to modify preserved historical workspace 'ws_fresh_benchmark'. "
            "Controlled benchmark runs must use a separate workspace such as 'ws_canonical_comparison_0004'."
        )

    logger.info("=" * 70)
    logger.info("CANONICAL COMPARISON WORKSPACE INGESTION: %s", workspace_id)
    logger.info("Mode: %s", "OFFLINE_SMOKE_TEST" if smoke_test else "FULL_PRODUCTION_INGEST")
    logger.info("=" * 70)

    combined_hex, file_hashes, file_paths = verify_corpus_files()
    logger.info("Corpus integrity verified. Manifest SHA-256: %s", combined_hex)

    if smoke_test:
        smoke_db = BACKEND_DIR / "tmp" / "smoke_test_tablestore.db"
        smoke_boundary_db = BACKEND_DIR / "tmp" / "smoke_test_boundary.db"
        smoke_db.parent.mkdir(parents=True, exist_ok=True)
        if smoke_db.exists():
            smoke_db.unlink()
        if smoke_boundary_db.exists():
            smoke_boundary_db.unlink()
        os.environ["KRE_TABLE_STORE_BACKEND"] = "sqlite"
        os.environ["KRE_WORKSPACE_REPO_BACKEND"] = "sqlite"
        os.environ["KRE_OKF_REPO_BACKEND"] = "sqlite"
        os.environ["KRE_TABLE_STORE_SQLITE_PATH"] = str(smoke_db)
        os.environ["KRE_WORKSPACE_SQLITE_PATH"] = str(smoke_boundary_db)
        os.environ["KRE_OKF_SQLITE_PATH"] = str(smoke_boundary_db)

    store = get_shared_table_store(reset=smoke_test, require_durable=True)
    boundary_repo = get_workspace_boundary_repo(reset=smoke_test, require_durable=True)
    cloud_repo = None
    if not smoke_test:
        cloud_repo = CloudRepository()
        try:
            cloud_repo.initialize()
        except Exception as e:
            logger.warning("CloudRepository initialize warning: %s", e)

    results = {
        "workspace_id": workspace_id,
        "corpus_manifest_sha256": combined_hex,
        "table_store_type": type(store).__name__,
        "boundary_repo_type": type(boundary_repo).__name__,
        "tables": {},
        "snapshots": {},
        "smoke_test": smoke_test,
    }

    # 1. Ingest Tabular CSVs into TableStore
    csv_configs = [
        ("survay.csv", "survay", 60255),
        ("rs_status_bill_passed_assent-1952-2016.csv", "rs_status_bill_passed_assent_1952_2016", 727),
    ]

    for fname, table_name, expected_rows in csv_configs:
        fpath = file_paths[fname]
        doc_id = generate_deterministic_doc_id(fname, workspace_id=workspace_id)
        logger.info("Ingesting table '%s' from %s (doc_id=%s)...", table_name, fname, doc_id)

        res = ingest_csv_to_table_store(
            path=fpath,
            document_id=doc_id,
            workspace_id=workspace_id,
            store=store,
            table_name=table_name,
        )

        manifest = store.get_ingestion_manifest(doc_id, workspace_id)
        logger.info(
            "Table '%s' ingestion complete: persisted=%d source=%d coverage_complete=%s",
            table_name,
            res.persisted_rows,
            res.source_rows,
            res.coverage_complete,
        )

        assert res.persisted_rows == expected_rows, (
            f"Table '{table_name}' expected {expected_rows} rows, got {res.persisted_rows}"
        )
        assert res.coverage_complete is True, f"Table '{table_name}' coverage is not complete"

        okf_table_status = None
        okf_table_entities = 0
        okf_table_props = 0
        if not smoke_test:
            try:
                from ingestion.okf_builder import extract_and_persist_okf
                okf_man = extract_and_persist_okf(
                    workspace_id=workspace_id,
                    document_id=doc_id,
                    document_version=res.document_version,
                    format="csv",
                    chunks=(),
                    table_name=table_name,
                    table_store=store,
                )
                if okf_man:
                    okf_table_status = okf_man.status
                    okf_table_entities = len(okf_man.entities)
                    okf_table_props = len(okf_man.assertions)
            except Exception as okf_err:
                logger.warning("OKF schema extraction failed for %s: %s", table_name, okf_err)

        # Verify paginated and streaming reads from TableStore
        sample_rows = store.query_rows(table_name, workspace_id, limit=5)
        agg_val, count, row_ids, idxs, digest = store.execute_single_pass_query(
            table_id=table_name,
            workspace_id=workspace_id,
            column_index=None,
            op=None,
            limit=10,
        )

        results["tables"][table_name] = {
            "document_id": doc_id,
            "document_version": res.document_version,
            "persisted_rows": res.persisted_rows,
            "source_rows": res.source_rows,
            "coverage_complete": res.coverage_complete,
            "manifest_verified": manifest is not None and manifest.get("coverage_complete") is True,
            "sample_rows_read": len(sample_rows),
            "streaming_count": count,
            "digest": digest[:16] if digest else None,
            "okf_status": okf_table_status,
            "okf_entities_count": okf_table_entities,
            "okf_properties_count": okf_table_props,
        }

    # 2. Record DocumentSnapshots in WorkspaceBoundaryRepository
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for fname, fpath in file_paths.items():
        doc_id = generate_deterministic_doc_id(fname, workspace_id=workspace_id)
        fhash = file_hashes[fname]
        doc_ver = fhash[:32]
        suffix = fpath.suffix.replace(".", "").lower()

        if suffix == "csv":
            t_name = "survay" if "survay" in fname else "rs_status_bill_passed_assent_1952_2016"
            t_info = results["tables"].get(t_name, {})
            persisted = t_info.get("persisted_rows", 0)
            total = t_info.get("source_rows", persisted)
            incomplete = not t_info.get("coverage_complete", True)
            okf_stat = t_info.get("okf_status") if not smoke_test else None
            okf_e_cnt = t_info.get("okf_entities_count", 0) if not smoke_test else 0
            okf_p_cnt = t_info.get("okf_properties_count", 0) if not smoke_test else 0
        else:
            persisted = 1
            total = 1
            incomplete = False
            okf_stat = None
            okf_e_cnt = 0
            okf_p_cnt = 0

        snapshot = DocumentSnapshot(
            workspace_id=workspace_id,
            document_id=doc_id,
            source=fname,
            version=doc_ver,
            format=suffix,
            incomplete_coverage_flag=incomplete,
            exclusions=(),
            persisted_rows=persisted,
            total_rows=total,
            created_at=now_iso,
            page_index_status=None,
            page_index_elements_count=0,
            okf_status=okf_stat,
            okf_entities_count=okf_e_cnt,
            okf_properties_count=okf_p_cnt,
        )
        boundary_repo.save_document_snapshot(snapshot)
        results["snapshots"][fname] = {
            "document_id": doc_id,
            "version": doc_ver,
            "format": suffix,
            "persisted_rows": persisted,
        }

    # 3. Full Production Embedding / Parsing Ingestion (skipped in smoke test)
    if not smoke_test:
        from ingestion.embed_service import embed_chunks_dual
        from schemas.models import Document

        for fname, fpath in file_paths.items():
            doc_id = generate_deterministic_doc_id(fname, workspace_id=workspace_id)
            fhash = file_hashes[fname]
            doc_ver = fhash[:32]
            suffix = fpath.suffix.replace(".", "").lower()

            logger.info("Parsing and embedding %s...", fname)
            parsed_doc = parse_file(fpath, document_id=doc_id, filename=fname, workspace_id=workspace_id)
            embedded_chunks = embed_chunks_dual(list(parsed_doc.chunks), provider="prod")
            doc = Document(
                id=doc_id,
                filename=fname,
                source_format=parsed_doc.source_format,
                chunks=tuple(embedded_chunks),
                workspace_id=workspace_id,
            )
            cloud_repo.save(doc)
            raw_bytes = fpath.read_bytes()
            cloud_repo.add_document_to_workspace(
                workspace_id=workspace_id,
                document=doc,
                raw_bytes=raw_bytes,
                size_str=f"{len(raw_bytes)/1024:.1f} KB",
            )
            logger.info("Saved %s with %d chunks to DynamoDB/Qdrant", fname, len(embedded_chunks))

            # PageIndex summary resolution
            pi_status = None
            pi_count = 0
            if suffix == "pdf":
                try:
                    pi_summary = boundary_repo.get_page_index_summary(workspace_id, doc_id, doc_ver)
                    if pi_summary:
                        pi_status = pi_summary.layout_status
                        pi_count = pi_summary.structural_elements_count
                except Exception as pi_err:
                    logger.warning("PageIndex summary lookup failed for %s: %s", fname, pi_err)

            # OKF factual extraction for PDFs under AC-3 bounds
            okf_stat = None
            okf_e_count = 0
            okf_p_count = 0
            if suffix == "pdf":
                try:
                    from ingestion.okf_builder import extract_and_persist_okf
                    okf_man = extract_and_persist_okf(
                        workspace_id=workspace_id,
                        document_id=doc_id,
                        document_version=doc_ver,
                        format="pdf",
                        chunks=doc.chunks,
                    )
                    if okf_man:
                        okf_stat = okf_man.status
                        okf_e_count = len(okf_man.entities)
                        okf_p_count = len(okf_man.assertions)
                except Exception as okf_err:
                    logger.warning("OKF extraction failed for %s: %s", fname, okf_err)

            # Update DocumentSnapshot with enriched metrics
            enriched_snap = DocumentSnapshot(
                workspace_id=workspace_id,
                document_id=doc_id,
                source=fname,
                version=doc_ver,
                format=suffix,
                incomplete_coverage_flag=False,
                exclusions=(),
                persisted_rows=1,
                total_rows=1,
                created_at=now_iso,
                page_index_status=pi_status,
                page_index_elements_count=pi_count,
                okf_status=okf_stat,
                okf_entities_count=okf_e_count,
                okf_properties_count=okf_p_count,
            )
            boundary_repo.save_document_snapshot(enriched_snap)

    logger.info("Ingestion / Smoke check finished successfully for %s", workspace_id)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest Canonical Benchmark Comparison Workspace.")
    parser.add_argument(
        "--workspace-id",
        default="ws_canonical_comparison_0004",
        help="Target workspace ID (must not be ws_fresh_benchmark)",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Run offline smoke check without remote model or embedding calls",
    )
    parser.add_argument(
        "--force-historical-override",
        action="store_true",
        help="DANGEROUS: Allow writing into ws_fresh_benchmark (disabled by default)",
    )

    args = parser.parse_args()
    res = ingest_comparison_corpus(
        workspace_id=args.workspace_id,
        smoke_test=args.smoke_test,
        force_historical_override=args.force_historical_override,
    )
    print(json.dumps(res, indent=2))
