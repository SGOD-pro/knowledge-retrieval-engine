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
