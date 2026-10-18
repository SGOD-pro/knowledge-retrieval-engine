"""Unit tests for StructuredQueryService."""

from decimal import Decimal
from pathlib import Path
import pytest

from db.table_store.memory_store import MemoryTableStore
from ingestion.csv_table_ingester import ingest_csv_to_table_store
from schemas.structured_table import (
    ColumnDefinition,
    HeaderTopology,
    InferredDtype,
    StructuredTable,
    TableCell,
    TableRow,
    TableSchema,
)
from services.retrieval.structured_query_service import (
    AmbiguousBindingError,
    IncompleteDataError,
    StructuredQueryResult,
    StructuredQueryService,
    UnsupportedQueryError,
)


@pytest.fixture
def survay_path() -> Path:
    p = Path(__file__).parent / "data" / "survay.csv"
    if not p.exists():
        pytest.skip("survay.csv not found")
    return p


@pytest.fixture
def populated_store(survay_path: Path) -> tuple[MemoryTableStore, str, str]:
    store = MemoryTableStore()
    ws = "ws_structured_test"
    doc_id = "doc_survay_test"
    ingest_csv_to_table_store(
        path=survay_path,
        document_id=doc_id,
        workspace_id=ws,
        store=store,
        batch_size=10000,
    )
    return store, ws, doc_id


def test_min_max_year_on_survay(populated_store):
    store, ws, doc_id = populated_store
    svc = StructuredQueryService(store)

    query = "What is the earliest and latest Year actually present in survay.csv?"
    result = svc.execute(query, workspace_id=ws)

    assert result is not None
    assert isinstance(result, StructuredQueryResult)
    assert result.result_value == 2013
    assert result.secondary_value == 2025
    assert result.target_column.lower() == "year"
    assert result.answer_text == "2013 and 2025."
    assert result.selection_count == 60255
    assert result.total_rows_in_table == 60255
    assert len(result.selection_hash) == 16


def test_total_income_change_2024_2025(populated_store):
    store, ws, doc_id = populated_store
    svc = StructuredQueryService(store)

    query = "For All industries (99999), how did total income change from 2024 to 2025? Give the amount and percentage change."
    result = svc.execute(query, workspace_id=ws)

    assert result is not None
    assert result.result_value == Decimal("4191")
    assert abs(result.secondary_value - Decimal("0.43")) <= Decimal("0.02")
    assert "4,191" in result.answer_text
    assert "0.43%" in result.answer_text
    assert ("fell" in result.answer_text.lower() or "decrease" in result.answer_text.lower() or "decline" in result.answer_text.lower())
    assert result.selection_count == 2
    assert len(result.row_ids) == 2
    assert len(result.selection_hash) == 16


def test_result_carries_provenance(populated_store):
    store, ws, doc_id = populated_store
    svc = StructuredQueryService(store)

    query = "What is the earliest and latest Year actually present in survay.csv?"
    result = svc.execute(query, workspace_id=ws)

    assert len(result.row_ids) > 0
    assert len(result.row_indices) > 0
    assert result.table_id == "survay"
    assert result.document_id == doc_id
    assert result.document_version != ""


def test_document_scope_filter(populated_store):
    store, ws, doc_id = populated_store
    svc = StructuredQueryService(store)

    # When query document_ids does not match table's document_id
    result = svc.execute(
        "What is the earliest and latest Year actually present in survay.csv?",
        workspace_id=ws,
        document_ids=["other_doc_id"],
    )
    assert result is None


def test_incomplete_table_raises(populated_store):
    store, ws, doc_id = populated_store
    svc = StructuredQueryService(store)

    # Mark table as incomplete
    man = store.get_ingestion_manifest(doc_id, ws)
    man["coverage_complete"] = False
    store.publish_active_version("survay", ws, doc_id, man["document_version"], man)

    with pytest.raises(IncompleteDataError) as exc_info:
        svc.execute(
            "What is the earliest and latest Year actually present in survay.csv?",
            workspace_id=ws,
        )
    assert "incomplete" in str(exc_info.value)


def test_duplicate_column_labels_raises_ambiguous():
    store = MemoryTableStore()
    ws = "ws_ambig"
    doc_id = "doc_ambig"

    cols = (
        ColumnDefinition(col_index=0, name="Revenue", inferred_dtype=InferredDtype.DECIMAL),
        ColumnDefinition(col_index=1, name="Revenue", inferred_dtype=InferredDtype.DECIMAL),
    )
    schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)
    table = StructuredTable(table_id="ambig_tab", document_id=doc_id, sheet_name=None, page_number=None, schema=schema, rows=())
    store.store_table(table, ws)
    store.publish_active_version("ambig_tab", ws, doc_id, "v1", {"coverage_complete": True, "source_rows": 0, "persisted_rows": 0})

    svc = StructuredQueryService(store)
    with pytest.raises(AmbiguousBindingError):
        svc.execute("What is the total Revenue in ambig_tab?", workspace_id=ws)


def test_normalize_dtype_enum_and_string():
    from schemas.structured_table import InferredDtype
    from services.retrieval.structured_query_service import StructuredQueryService

    assert StructuredQueryService._normalize_dtype(InferredDtype.INTEGER) == "integer"
    assert StructuredQueryService._normalize_dtype(InferredDtype.DECIMAL) == "decimal"
    assert StructuredQueryService._normalize_dtype("INTEGER") == "integer"
    assert StructuredQueryService._normalize_dtype("decimal") == "decimal"
    assert StructuredQueryService._normalize_dtype(InferredDtype.PERCENTAGE) == "percentage"


def test_multi_table_workspace_binds_correct_table_for_metric_delta(survay_path: Path):
    store = MemoryTableStore()
    ws = "ws_multi_table_test"

    # Ingest distractor table first so it appears first in list_tables
    rs_path = Path(__file__).parent.parent.parent / "data" / "non_pdf_formats" / "rs_status_bill_passed_assent-1952-2016.csv"
    if rs_path.exists():
        ingest_csv_to_table_store(
            path=rs_path,
            table_name="rs_status_bill_passed_assent_1952_2016",
            document_id="doc_rs_distractor",
            workspace_id=ws,
            store=store,
            batch_size=10000,
        )
    else:
        cols = (
            ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=1, name="Bill_Title", inferred_dtype=InferredDtype.STRING),
        )
        schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)
        table = StructuredTable(table_id="rs_status_bill_passed_assent_1952_2016", document_id="doc_rs_distractor", sheet_name=None, page_number=None, schema=schema, rows=())
        store.store_table(table, ws)
        store.publish_active_version("rs_status_bill_passed_assent_1952_2016", ws, "doc_rs_distractor", "v1", {"coverage_complete": True, "source_rows": 0, "persisted_rows": 0})

    # Ingest survay second
    ingest_csv_to_table_store(
        path=survay_path,
        table_name="survay",
        document_id="doc_survay",
        workspace_id=ws,
        store=store,
        batch_size=10000,
    )

    # Verify tables in workspace
    tables = store.list_tables(ws)
    assert tables[0] == "rs_status_bill_passed_assent_1952_2016"
    assert "survay" in tables

    svc = StructuredQueryService(store)
    # Query Q153 does NOT name survay.csv
    query = "For All industries (99999), how did total income change from 2024 to 2025? Give the amount and percentage change."
    result = svc.execute(query, workspace_id=ws)

    assert result is not None
    assert result.table_id == "survay"
    assert result.result_value == Decimal("4191")
    assert abs(result.secondary_value - Decimal("0.43")) <= Decimal("0.02")
    assert "4,191" in result.answer_text
    assert "0.43%" in result.answer_text


def test_explicit_table_binding_does_not_switch_to_other_tables_on_filter_failure(survay_path: Path):
    store = MemoryTableStore()
    ws = "ws_explicit_test"

    # Ingest both distractor and survay
    rs_path = Path(__file__).parent.parent.parent / "data" / "non_pdf_formats" / "rs_status_bill_passed_assent-1952-2016.csv"
    if rs_path.exists():
        ingest_csv_to_table_store(
            path=rs_path,
            table_name="rs_status_bill_passed_assent_1952_2016",
            document_id="doc_rs_distractor",
            workspace_id=ws,
            store=store,
            batch_size=10000,
        )
    else:
        cols = (
            ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=1, name="Bill_Title", inferred_dtype=InferredDtype.STRING),
        )
        schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)
        table = StructuredTable(table_id="rs_status_bill_passed_assent_1952_2016", document_id="doc_rs_distractor", sheet_name=None, page_number=None, schema=schema, rows=())
        store.store_table(table, ws)
        store.publish_active_version("rs_status_bill_passed_assent_1952_2016", ws, "doc_rs_distractor", "v1", {"coverage_complete": True, "source_rows": 0, "persisted_rows": 0})

    ingest_csv_to_table_store(
        path=survay_path,
        table_name="survay",
        document_id="doc_survay",
        workspace_id=ws,
        store=store,
        batch_size=10000,
    )

    svc = StructuredQueryService(store)
    from services.retrieval.structured_query_service import UnresolvedFilterError

    # Explicit query naming the distractor table must not switch to survay
    query = "For All industries (99999) in rs_status_bill_passed_assent_1952_2016, how did total income change from 2024 to 2025?"
    with pytest.raises(UnresolvedFilterError) as exc_info:
        svc.execute(query, workspace_id=ws)

    assert exc_info.value.table_id == "rs_status_bill_passed_assent_1952_2016"


def test_multi_table_unresolved_filter_in_all_candidates_raises_error(survay_path: Path):
    store = MemoryTableStore()
    ws = "ws_unresolved_all_test"

    rs_path = Path(__file__).parent.parent.parent / "data" / "non_pdf_formats" / "rs_status_bill_passed_assent-1952-2016.csv"
    if rs_path.exists():
        ingest_csv_to_table_store(
            path=rs_path,
            table_name="rs_status_bill_passed_assent_1952_2016",
            document_id="doc_rs_distractor",
            workspace_id=ws,
            store=store,
            batch_size=10000,
        )
    else:
        cols = (
            ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=1, name="Bill_Title", inferred_dtype=InferredDtype.STRING),
        )
        schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)
        table = StructuredTable(table_id="rs_status_bill_passed_assent_1952_2016", document_id="doc_rs_distractor", sheet_name=None, page_number=None, schema=schema, rows=())
        store.store_table(table, ws)
        store.publish_active_version("rs_status_bill_passed_assent_1952_2016", ws, "doc_rs_distractor", "v1", {"coverage_complete": True, "source_rows": 0, "persisted_rows": 0})

    ingest_csv_to_table_store(
        path=survay_path,
        table_name="survay",
        document_id="doc_survay",
        workspace_id=ws,
        store=store,
        batch_size=10000,
    )

    svc = StructuredQueryService(store)
    from services.retrieval.structured_query_service import UnresolvedFilterError

    # Entity not present in any table in workspace raises UnresolvedFilterError
    query = "For NonexistentEntity (00000), how did total income change from 2024 to 2025? Give the amount and percentage change."
    with pytest.raises(UnresolvedFilterError):
        svc.execute(query, workspace_id=ws)



