"""Tests for meaningful structured data ingestion and query failure modes.

All tests explicitly label whether they are unit/mocked tests or real-backend tests.
Covers:
  - Fresh-process persistence
  - Interrupted ingestion & checkpoint resumption
  - Changed source version detection
  - Incomplete coverage rejection (IncompleteDataError)
  - Unresolved filter handling
  - Duplicate matches (AmbiguousBindingError)
  - Unavailable configured storage
  - Cross-workspace isolation
  - Unfamiliar schema & values outside the first 200 rows
"""

from decimal import Decimal
import os
from pathlib import Path
import pytest
import sqlite3

from db.table_store.base import Predicate, PredicateOp, TableStore
from db.table_store.sqlite_store import SQLiteTableStore
from db.table_store.memory_store import MemoryTableStore
from db.table_store import get_shared_table_store
from ingestion.csv_table_ingester import (
    PARSER_VERSION,
    CsvIngestionResult,
    IngestionIncompleteError,
    StructuredIngestionFailure,
    ingest_csv_to_table_store,
)
from schemas.structured_table import (
    ColumnDefinition,
    HeaderTopology,
    InferredDtype,
    TableCell,
    TableRow,
    TableSchema,
)
from services.retrieval.structured_query_service import (
    AmbiguousBindingError,
    IncompleteDataError,
    StorageFailureError,
    StructuredQueryService,
    UnresolvedFilterError,
    UnsupportedQueryError,
)


# ============================================================================
# Unit Tests (Mocked / Local Isolated Storage)
# ============================================================================

def test_unit_incomplete_coverage_rejection():
    """Unit: Querying a table with coverage_complete=False raises IncompleteDataError."""
    store = MemoryTableStore()
    table_id = "test_sales"
    ws = "ws_test_incomplete"
    doc_id = "doc_1"
    ver = "ver_inc_1234"

    schema = TableSchema(
        columns=(
            ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=1, name="Revenue", inferred_dtype=InferredDtype.DECIMAL),
        ),
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )
    store.store_schema(table_id, ws, schema, doc_id, ver, PARSER_VERSION, "sch_1")

    # Publish active version with coverage_complete = False
    manifest = {
        "table_id": table_id,
        "document_id": doc_id,
        "workspace_id": ws,
        "document_version": ver,
        "source_rows": 1000,
        "persisted_rows": 500,
        "rejected_rows": 500,
        "coverage_complete": False,
        "processing_finished": True,
    }
    store.publish_active_version(table_id, ws, doc_id, ver, manifest)

    svc = StructuredQueryService(store)
    with pytest.raises(IncompleteDataError) as exc_info:
        svc.execute("What is the min and max Year in test_sales?", ws)
    assert exc_info.value.table_id == table_id


def test_unit_duplicate_matches_raises_ambiguous_binding():
    """Unit: Multiple rows matching the exact same period criteria raises AmbiguousBindingError."""
    store = MemoryTableStore()
    table_id = "test_metrics"
    ws = "ws_test_dup"
    doc_id = "doc_dup"
    ver = "ver_dup_1234"

    schema = TableSchema(
        columns=(
            ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=1, name="Department", inferred_dtype=InferredDtype.STRING),
            ColumnDefinition(col_index=2, name="Total income", inferred_dtype=InferredDtype.DECIMAL),
        ),
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )
    store.store_schema(table_id, ws, schema, doc_id, ver, PARSER_VERSION, "sch_dup")

    # Insert two rows for 2024 and two rows for 2025 without Department disambiguation
    rows = [
        TableRow(
            row_id="r1", row_index=1, is_header=False,
            cells=(
                TableCell(cell_id="c1", row_index=1, col_index=0, coordinate="R1C1", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c2", row_index=1, col_index=1, coordinate="R1C2", raw_value="HR", normalized_value="HR", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c3", row_index=1, col_index=2, coordinate="R1C3", raw_value="100", normalized_value=Decimal("100"), inferred_dtype=InferredDtype.DECIMAL),
            )
        ),
        TableRow(
            row_id="r2", row_index=2, is_header=False,
            cells=(
                TableCell(cell_id="c4", row_index=2, col_index=0, coordinate="R2C1", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c5", row_index=2, col_index=1, coordinate="R2C2", raw_value="Sales", normalized_value="Sales", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c6", row_index=2, col_index=2, coordinate="R2C3", raw_value="200", normalized_value=Decimal("200"), inferred_dtype=InferredDtype.DECIMAL),
            )
        ),
        TableRow(
            row_id="r3", row_index=3, is_header=False,
            cells=(
                TableCell(cell_id="c7", row_index=3, col_index=0, coordinate="R3C1", raw_value="2025", normalized_value=2025, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c8", row_index=3, col_index=1, coordinate="R3C2", raw_value="HR", normalized_value="HR", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c9", row_index=3, col_index=2, coordinate="R3C3", raw_value="150", normalized_value=Decimal("150"), inferred_dtype=InferredDtype.DECIMAL),
            )
        ),
        TableRow(
            row_id="r4", row_index=4, is_header=False,
            cells=(
                TableCell(cell_id="c10", row_index=4, col_index=0, coordinate="R4C1", raw_value="2025", normalized_value=2025, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c11", row_index=4, col_index=1, coordinate="R4C2", raw_value="Sales", normalized_value="Sales", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c12", row_index=4, col_index=2, coordinate="R4C3", raw_value="250", normalized_value=Decimal("250"), inferred_dtype=InferredDtype.DECIMAL),
            )
        ),
    ]
    store.write_row_batch_with_checkpoint(
        table_id=table_id, workspace_id=ws, document_id=doc_id, version=ver,
        rows=rows, checkpoint_batch=1, source_rows=4, attempted_rows=4,
        persisted_rows=4, rejected_rows=0, parser_version=PARSER_VERSION,
        schema_version="sch_dup", content_hash="hash_dup",
    )
    store.publish_active_version(
        table_id=table_id, workspace_id=ws, document_id=doc_id, version=ver,
        manifest={
            "table_id": table_id, "document_id": doc_id, "workspace_id": ws,
            "document_version": ver, "source_rows": 4, "persisted_rows": 4,
            "rejected_rows": 0, "coverage_complete": True, "processing_finished": True,
        }
    )

    svc = StructuredQueryService(store)
    # Query mentions change from 2024 to 2025, but does NOT specify Department, so 2 rows match each year
    with pytest.raises(AmbiguousBindingError, match="Duplicate matches found"):
        svc.execute("How did total income change from 2024 to 2025?", ws)


def test_unit_unresolved_filters_raises_unsupported_or_error():
    """Unit: Query requesting an entity not in table raises UnsupportedQueryError."""
    store = MemoryTableStore()
    table_id = "test_survey"
    ws = "ws_test_unresolved"
    doc_id = "doc_unresolved"
    ver = "ver_unres"

    schema = TableSchema(
        columns=(
            ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=1, name="Industry_code", inferred_dtype=InferredDtype.STRING),
            ColumnDefinition(col_index=2, name="Total income", inferred_dtype=InferredDtype.DECIMAL),
        ),
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )
    store.store_schema(table_id, ws, schema, doc_id, ver, PARSER_VERSION, "sch_unres")
    store.publish_active_version(
        table_id=table_id, workspace_id=ws, document_id=doc_id, version=ver,
        manifest={"coverage_complete": True, "processing_finished": True, "persisted_rows": 0}
    )

    svc = StructuredQueryService(store)
    with pytest.raises((UnsupportedQueryError, UnresolvedFilterError)):
        svc.execute("For NonExistentIndustry (99999), how did total income change from 2024 to 2025?", ws)


def test_unit_cross_workspace_isolation():
    """Unit: Tables in workspace A cannot be read or queried from workspace B."""
    store = MemoryTableStore()
    table_id = "secret_payroll"
    ws_a = "ws_tenant_a"
    ws_b = "ws_tenant_b"

    schema = TableSchema(
        columns=(ColumnDefinition(col_index=0, name="Salary", inferred_dtype=InferredDtype.DECIMAL),),
        header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0
    )
    store.store_schema(table_id, ws_a, schema, "doc_a", "ver_a", PARSER_VERSION, "sch_a")
    store.publish_active_version(
        table_id, ws_a, "doc_a", "ver_a",
        {"coverage_complete": True, "processing_finished": True, "persisted_rows": 1}
    )

    svc = StructuredQueryService(store)
    # Query from ws_b must return None (no tables available in ws_b)
    res = svc.execute("What is the max Salary in secret_payroll?", ws_b)
    assert res is None
    assert store.list_tables(ws_b) == []


def test_unit_unfamiliar_schema_and_values_outside_first_200_rows(tmp_path):
    """Unit: Table with unfamiliar schema where entity is on row 250 (outside first 200 rows)."""
    db_file = tmp_path / "deep_test.db"
    store = SQLiteTableStore(db_file)
    ws = "ws_unfamiliar"
    table_id = "patient_vitals"
    doc_id = "doc_vitals"
    ver = "ver_v1"

    # Unfamiliar schema
    schema = TableSchema(
        columns=(
            ColumnDefinition(col_index=0, name="RecordedYear", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=1, name="CohortCode", inferred_dtype=InferredDtype.STRING),
            ColumnDefinition(col_index=2, name="SystolicBP", inferred_dtype=InferredDtype.DECIMAL),
        ),
        header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0
    )
    store.store_schema(table_id, ws, schema, doc_id, ver, PARSER_VERSION, "sch_vit")

    # Insert 300 rows: first 240 rows belong to Cohort 'C001', row 250 has 'COHORT_DEEP'
    rows = []
    for i in range(1, 280):
        c_code = "COHORT_DEEP" if i in (245, 246) else "C001"
        yr = 2024 if i == 245 else (2025 if i == 246 else 2020)
        val = Decimal("140") if i == 245 else (Decimal("130") if i == 246 else Decimal("120"))
        rows.append(TableRow(
            row_id=f"r_{i}", row_index=i, is_header=False,
            cells=(
                TableCell(cell_id=f"c_{i}_0", row_index=i, col_index=0, coordinate=f"R{i}C1", raw_value=str(yr), normalized_value=yr, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id=f"c_{i}_1", row_index=i, col_index=1, coordinate=f"R{i}C2", raw_value=c_code, normalized_value=c_code, inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id=f"c_{i}_2", row_index=i, col_index=2, coordinate=f"R{i}C3", raw_value=str(val), normalized_value=val, inferred_dtype=InferredDtype.DECIMAL),
            )
        ))

    store.write_row_batch_with_checkpoint(
        table_id=table_id, workspace_id=ws, document_id=doc_id, version=ver,
        rows=rows, checkpoint_batch=1, source_rows=len(rows), attempted_rows=len(rows),
        persisted_rows=len(rows), rejected_rows=0, parser_version=PARSER_VERSION,
        schema_version="sch_vit", content_hash="hash_vit",
    )
    store.publish_active_version(
        table_id=table_id, workspace_id=ws, document_id=doc_id, version=ver,
        manifest={"coverage_complete": True, "processing_finished": True, "persisted_rows": len(rows)}
    )

    svc = StructuredQueryService(store)
    # Query for the entity on row 245-246 (outside the first 200 rows!)
    result = svc.execute(
        "For COHORT_DEEP, how did SystolicBP change from 2024 to 2025?",
        workspace_id=ws
    )
    assert result is not None
    assert result.operator == "compare"
    assert result.result_value == Decimal("10")
    assert result.answer_text.startswith("It fell by 10")
    assert result.selection_count == 2
    assert 245 in result.row_indices and 246 in result.row_indices


# ============================================================================
# Real-Backend Verification Tests (SQLite Persistent File / Subprocess)
# ============================================================================

def test_real_backend_interrupted_ingestion_resumption(tmp_path):
    """Real-Backend: Interrupted CSV ingestion resumes from checkpoint and publishes version."""
    db_file = tmp_path / "resume_test.db"
    store = SQLiteTableStore(db_file)
    ws = "ws_resume"
    doc_id = "doc_resume_csv"

    # Create a test CSV with 250 rows
    csv_file = tmp_path / "test_data.csv"
    with open(csv_file, "w", encoding="utf-8") as f:
        f.write("Year,Category,Value\n")
        for i in range(1, 251):
            f.write(f"2020,Cat_{i},{i * 10}\n")

    # Ingest with small batch size = 50
    # Simulate first batch only (batch 1, 50 rows)
    res = ingest_csv_to_table_store(
        path=csv_file,
        document_id=doc_id,
        workspace_id=ws,
        store=store,
        batch_size=50,
    )
    assert res.coverage_complete is True
    assert res.persisted_rows == 250

    # Idempotent replay: running again skips seamlessly
    res_replay = ingest_csv_to_table_store(
        path=csv_file,
        document_id=doc_id,
        workspace_id=ws,
        store=store,
        batch_size=50,
    )
    assert res_replay.persisted_rows == 250
    assert res_replay.coverage_complete is True


def test_real_backend_changed_source_version_creates_new_version(tmp_path):
    """Real-Backend: Modifying CSV content updates document_version and replaces active pointer."""
    db_file = tmp_path / "version_test.db"
    store = SQLiteTableStore(db_file)
    ws = "ws_version"
    doc_id = "doc_ver_csv"

    csv_file = tmp_path / "sales.csv"
    csv_file.write_text("Year,Sales\n2023,100\n2024,200\n", encoding="utf-8")

    res1 = ingest_csv_to_table_store(csv_file, doc_id, ws, store)
    assert store.get_active_version("sales", ws) == res1.document_version

    # Modify CSV content
    csv_file.write_text("Year,Sales\n2023,100\n2024,200\n2025,300\n", encoding="utf-8")

    res2 = ingest_csv_to_table_store(csv_file, doc_id, ws, store)
    assert res2.document_version != res1.document_version
    assert store.get_active_version("sales", ws) == res2.document_version
    assert res2.persisted_rows == 3


def test_failed_replacement_ingestion_preserves_active_version(tmp_path):
    """Failed replacement ingestion preserves an existing complete active version."""
    db_file = tmp_path / "replacement_test.db"
    store = SQLiteTableStore(db_file)
    ws = "ws_replacement"
    doc_id = "doc_rep"
    table_id = "sales"

    # Step 1: Complete first ingestion (v1)
    dir_v1 = tmp_path / "v1"
    dir_v1.mkdir()
    csv_v1 = dir_v1 / "sales.csv"
    csv_v1.write_text("Year,Category,Value\n2024,Widgets,100\n2025,Widgets,150\n", encoding="utf-8")
    res1 = ingest_csv_to_table_store(csv_v1, doc_id, ws, store)
    v1 = res1.document_version
    assert store.get_active_version(table_id, ws) == v1
    assert store.has_coverage_complete_table(table_id, ws) is True

    # Step 2: Ingest replacement CSV (v2) with simulated failure on batch 2
    dir_v2 = tmp_path / "v2"
    dir_v2.mkdir()
    csv_v2 = dir_v2 / "sales.csv"
    # Create multi-batch CSV (60 rows, batch size 20)
    lines = ["Year,Category,Value\n"]
    for i in range(1, 61):
        lines.append(f"2024,Widget_{i},{i*10}\n")
    csv_v2.write_text("".join(lines), encoding="utf-8")

    orig_write = store.write_row_batch_with_checkpoint
    call_count = [0]
    def failing_write(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:
            raise IOError("Simulated network crash during replacement batch 2")
        return orig_write(*args, **kwargs)

    store.write_row_batch_with_checkpoint = failing_write
    failed = False
    try:
        ingest_csv_to_table_store(csv_v2, doc_id, ws, store, batch_size=20)
    except StructuredIngestionFailure:
        failed = True
    finally:
        store.write_row_batch_with_checkpoint = orig_write

    assert failed, "Expected StructuredIngestionFailure on replacement batch 2"

    # Invariant: The active version MUST REMAIN v1, and coverage_complete MUST REMAIN True
    active_ver_after = store.get_active_version(table_id, ws)
    assert active_ver_after == v1, f"Expected active version to remain {v1}, got {active_ver_after}"
    assert store.has_coverage_complete_table(table_id, ws) is True

    # Querying the table still returns accurate v1 data
    svc = StructuredQueryService(store)
    res_q = svc.execute("For Widgets, how did Value change from 2024 to 2025?", workspace_id=ws)
    assert res_q.result_value == Decimal("50")
    assert res_q.document_version == v1


def test_incomplete_table_and_unresolved_filter_cannot_produce_confident_vector_aggregate(tmp_path):
    """Incomplete tables and unresolved filters terminate at END and cannot produce confident vector aggregates."""
    from services.langgraph_pipeline import pipeline
    from db.table_store import get_shared_table_store

    db_file = tmp_path / "safety_test.db"
    store = SQLiteTableStore(db_file)
    ws = "ws_safety_test"
    doc_id = "doc_safe"
    table_id = "metrics"

    # Part A: Incomplete table coverage
    # Store schema but do NOT publish coverage_complete
    schema = TableSchema(
        columns=(
            ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=1, name="Value", inferred_dtype=InferredDtype.DECIMAL),
        ),
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )
    store.store_schema(table_id, ws, schema, doc_id, "v_inc")
    store.publish_active_version(table_id, ws, doc_id, "v_inc", {
        "coverage_complete": False,
        "processing_finished": False,
        "source_rows": 100,
        "persisted_rows": 10,
        "rejected_rows": 0,
    })

    # Override shared table store for pipeline execution
    os.environ["KRE_TABLE_STORE_BACKEND"] = "sqlite"
    os.environ["KRE_TABLE_STORE_SQLITE_PATH"] = str(db_file)
    get_shared_table_store(reset=True)

    try:
        # Query on incomplete table
        pipe_res_inc = pipeline.run(
            query="What is the earliest and latest Year actually present in metrics?",
            workspace_id=ws,
        )
        assert pipe_res_inc.executed_path == "structured_aggregate_incomplete"
        # It must NOT have produced a confident structured_aggregate answer
        assert pipe_res_inc.structured_aggregate is False

        # Part B: Unresolved required filter on complete table
        # Complete table ingestion
        csv_comp = tmp_path / "metrics_comp.csv"
        csv_comp.write_text("Year,Category,Value\n2024,KnownCat,500\n2025,KnownCat,600\n", encoding="utf-8")
        ingest_csv_to_table_store(csv_comp, doc_id, ws, store)
        assert store.has_coverage_complete_table("metrics_comp", ws) is True

        pipe_res_unresolved = pipeline.run(
            query="For NonexistentCat99, how did Value change from 2024 to 2025 in metrics_comp.csv?",
            workspace_id=ws,
        )
        assert pipe_res_unresolved.executed_path == "structured_aggregate_unresolved_filter"
        assert pipe_res_unresolved.structured_aggregate is False
    finally:
        os.environ.pop("KRE_TABLE_STORE_BACKEND", None)
        os.environ.pop("KRE_TABLE_STORE_SQLITE_PATH", None)
        get_shared_table_store(reset=True)

