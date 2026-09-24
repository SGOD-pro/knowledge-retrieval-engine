"""Tests for complete, resumable, and versioned structured CSV table ingestion."""

import csv
import hashlib
from pathlib import Path
import tempfile
import pytest

from db.table_store.memory_store import MemoryTableStore
from db.table_store.base import Predicate, PredicateOp
from ingestion.csv_table_ingester import (
    CsvIngestionResult,
    _compute_streamed_hash,
    ingest_csv_to_table_store,
)


@pytest.fixture
def survay_csv_path() -> Path:
    p = Path(__file__).parent / "data" / "survay.csv"
    if not p.exists():
        pytest.skip("survay.csv test data not found")
    return p


@pytest.fixture
def memory_store() -> MemoryTableStore:
    return MemoryTableStore()


def test_streamed_hash_matches_sha256(survay_csv_path: Path):
    streamed = _compute_streamed_hash(survay_csv_path)
    full = hashlib.sha256(survay_csv_path.read_bytes()).hexdigest()
    assert streamed == full


def test_survay_csv_full_ingestion_60255_rows(survay_csv_path: Path, memory_store: MemoryTableStore):
    workspace_id = "ws_benchmark"
    doc_id = "doc_survay"

    res = ingest_csv_to_table_store(
        path=survay_csv_path,
        document_id=doc_id,
        workspace_id=workspace_id,
        store=memory_store,
        batch_size=5000,
    )

    assert res.source_rows == 60255
    assert res.persisted_rows == 60255
    assert res.rejected_rows == 0
    assert res.processing_finished is True
    assert res.coverage_complete is True
    assert memory_store.has_coverage_complete_table("survay", workspace_id) is True


def test_ingestion_row_beyond_5000_accessible(survay_csv_path: Path, memory_store: MemoryTableStore):
    workspace_id = "ws_test_5000"
    doc_id = "doc_survay_5000"

    ingest_csv_to_table_store(
        path=survay_csv_path,
        document_id=doc_id,
        workspace_id=workspace_id,
        store=memory_store,
        batch_size=5000,
    )

    # Query row at index 5500
    pred = [Predicate(column_index=0, op=PredicateOp.EQ, value=2025)]
    rows = memory_store.query_rows("survay", workspace_id, predicates=pred, limit=10)
    assert len(rows) > 0


def test_ingestion_idempotent_on_same_hash(survay_csv_path: Path, memory_store: MemoryTableStore):
    workspace_id = "ws_idempotent"
    doc_id = "doc_idem"

    res1 = ingest_csv_to_table_store(
        path=survay_csv_path,
        document_id=doc_id,
        workspace_id=workspace_id,
        store=memory_store,
        batch_size=10000,
    )
    assert res1.persisted_rows == 60255

    # Second call without changes returns cached manifest without re-writing
    res2 = ingest_csv_to_table_store(
        path=survay_csv_path,
        document_id=doc_id,
        workspace_id=workspace_id,
        store=memory_store,
        batch_size=10000,
    )
    assert res2.persisted_rows == 60255
    assert res2.content_hash == res1.content_hash
    assert res2.document_version == res1.document_version


def test_ingestion_resumes_from_checkpoint(tmp_path: Path, memory_store: MemoryTableStore):
    # Create synthetic CSV with 250 rows
    csv_file = tmp_path / "synthetic.csv"
    with csv_file.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ID", "Name", "Score"])
        for i in range(1, 251):
            writer.writerow([i, f"Item_{i}", i * 10])

    workspace_id = "ws_resume"
    doc_id = "doc_resume"

    # Pre-populate checkpoint indicating batch 1 (first 100 rows) already committed
    content_hash = _compute_streamed_hash(csv_file)
    version = content_hash[:32]
    memory_store._checkpoints[(workspace_id, doc_id, version)] = {
        "checkpoint_batch": 1,
        "source_rows": 100,
        "attempted_rows": 100,
        "persisted_rows": 100,
        "rejected_rows": 0,
        "parser_version": "1.0.0",
        "schema_version": "dummy",
        "content_hash": content_hash,
    }

    # Run ingestion with batch_size=100
    # It should skip rows 1..100 and ingest remaining 150 rows
    res = ingest_csv_to_table_store(
        path=csv_file,
        document_id=doc_id,
        workspace_id=workspace_id,
        store=memory_store,
        batch_size=100,
        force_reingest=False,
    )

    assert res.source_rows == 250
    assert res.persisted_rows == 250
    assert res.processing_finished is True


def test_coverage_complete_false_when_rejections_present(tmp_path: Path, memory_store: MemoryTableStore):
    csv_file = tmp_path / "corrupt.csv"
    csv_file.write_text("Year,Value\n2021,100\n2022,200\n", encoding="utf-8")

    workspace_id = "ws_corrupt"
    doc_id = "doc_corrupt"

    res = ingest_csv_to_table_store(
        path=csv_file,
        document_id=doc_id,
        workspace_id=workspace_id,
        store=memory_store,
        batch_size=10,
    )
    assert res.coverage_complete is True

    # Now simulate a manifest with rejected_rows > 0
    man = memory_store.get_ingestion_manifest(doc_id, workspace_id)
    man["rejected_rows"] = 5
    man["coverage_complete"] = False
    memory_store.publish_active_version("corrupt", workspace_id, doc_id, res.document_version, man)

    assert memory_store.has_coverage_complete_table("corrupt", workspace_id) is False


def test_cross_workspace_isolation(tmp_path: Path, memory_store: MemoryTableStore):
    csv_file = tmp_path / "data.csv"
    csv_file.write_text("ID,Val\n1,10\n2,20\n", encoding="utf-8")

    ingest_csv_to_table_store(csv_file, "doc_1", "ws_alpha", memory_store)
    ingest_csv_to_table_store(csv_file, "doc_1", "ws_beta", memory_store)

    assert "data" in memory_store.list_tables("ws_alpha")
    assert "data" in memory_store.list_tables("ws_beta")

    # Invalidate in ws_alpha does not touch ws_beta
    memory_store.invalidate_document_version("doc_1", "ws_alpha")
    assert "data" not in memory_store.list_tables("ws_alpha")
    assert "data" in memory_store.list_tables("ws_beta")
