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
    UnresolvedFilterError,
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


def _create_sample_metric_table(store: MemoryTableStore, ws: str, table_id: str, doc_id: str, delta_val: int = 100):
    cols = (
        ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
        ColumnDefinition(col_index=1, name="Value", inferred_dtype=InferredDtype.DECIMAL),
    )
    schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)
    rows = (
        TableRow(
            row_index=0,
            row_id=f"{table_id}_r1",
            cells=(
                TableCell(cell_id=f"{table_id}_c1_1", row_index=0, col_index=0, coordinate="A1", raw_value="2024", normalized_value=2024),
                TableCell(cell_id=f"{table_id}_c1_2", row_index=0, col_index=1, coordinate="B1", raw_value="1000", normalized_value=Decimal("1000")),
            ),
        ),
        TableRow(
            row_index=1,
            row_id=f"{table_id}_r2",
            cells=(
                TableCell(cell_id=f"{table_id}_c2_1", row_index=1, col_index=0, coordinate="A2", raw_value="2025", normalized_value=2025),
                TableCell(cell_id=f"{table_id}_c2_2", row_index=1, col_index=1, coordinate="B2", raw_value=str(1000 + delta_val), normalized_value=Decimal(str(1000 + delta_val))),
            ),
        ),
    )
    table = StructuredTable(table_id=table_id, document_id=doc_id, sheet_name=None, page_number=None, schema=schema, rows=rows)
    store.store_table(table, ws)
    store.publish_active_version(table_id, ws, doc_id, "v1", {"coverage_complete": True, "source_rows": 2, "persisted_rows": 2, "rejected_rows": 0})


def test_two_equally_compatible_tables_raise_ambiguity():
    store = MemoryTableStore()
    ws = "ws_ambig_pair"
    _create_sample_metric_table(store, ws, "table_alpha", "doc_alpha", delta_val=100)
    _create_sample_metric_table(store, ws, "table_beta", "doc_beta", delta_val=200)

    svc = StructuredQueryService(store)
    query = "how did Value change from 2024 to 2025?"
    with pytest.raises(AmbiguousBindingError):
        svc.execute(query, workspace_id=ws)


def test_reverse_table_insertion_order_preserves_ambiguity():
    store = MemoryTableStore()
    ws = "ws_ambig_reverse"
    # Insert beta first, then alpha
    _create_sample_metric_table(store, ws, "table_beta", "doc_beta", delta_val=200)
    _create_sample_metric_table(store, ws, "table_alpha", "doc_alpha", delta_val=100)

    svc = StructuredQueryService(store)
    query = "how did Value change from 2024 to 2025?"
    with pytest.raises(AmbiguousBindingError):
        svc.execute(query, workspace_id=ws)


def test_intended_source_incomplete_raises_incomplete_data_error_without_fallback(survay_path: Path):
    store = MemoryTableStore()
    ws = "ws_incomplete_intended"

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

    # Mark intended table (survay) as coverage_complete=False
    man = store.get_ingestion_manifest("doc_survay", ws)
    man["coverage_complete"] = False
    store.publish_active_version("survay", ws, "doc_survay", man["document_version"], man)

    svc = StructuredQueryService(store)
    query = "For All industries (99999), how did total income change from 2024 to 2025? Give the amount and percentage change."

    # Intended source is survay, but it is incomplete: must raise IncompleteDataError on survay, not fall back to rs table
    with pytest.raises(IncompleteDataError) as exc_info:
        svc.execute(query, workspace_id=ws)

    assert exc_info.value.table_id == "survay"


def _create_sample_sales_table(
    store: MemoryTableStore,
    ws: str,
    table_id: str = "sales_data",
    doc_id: str = "doc_sales",
) -> None:
    cols = (
        ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
        ColumnDefinition(col_index=1, name="Industry", inferred_dtype=InferredDtype.STRING),
        ColumnDefinition(col_index=2, name="Amount", inferred_dtype=InferredDtype.DECIMAL),
    )
    schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)
    rows = (
        TableRow(
            row_index=0,
            row_id=f"{table_id}_r0",
            cells=(
                TableCell(cell_id=f"{table_id}_c0_0", row_index=0, col_index=0, coordinate="A1", raw_value="2023", normalized_value=2023),
                TableCell(cell_id=f"{table_id}_c0_1", row_index=0, col_index=1, coordinate="B1", raw_value="tech", normalized_value="tech"),
                TableCell(cell_id=f"{table_id}_c0_2", row_index=0, col_index=2, coordinate="C1", raw_value="100.00", normalized_value=Decimal("100.00")),
            ),
        ),
        TableRow(
            row_index=1,
            row_id=f"{table_id}_r1",
            cells=(
                TableCell(cell_id=f"{table_id}_c1_0", row_index=1, col_index=0, coordinate="A2", raw_value="2024", normalized_value=2024),
                TableCell(cell_id=f"{table_id}_c1_1", row_index=1, col_index=1, coordinate="B2", raw_value="tech", normalized_value="tech"),
                TableCell(cell_id=f"{table_id}_c1_2", row_index=1, col_index=2, coordinate="C2", raw_value="200.00", normalized_value=Decimal("200.00")),
            ),
        ),
        TableRow(
            row_index=2,
            row_id=f"{table_id}_r2",
            cells=(
                TableCell(cell_id=f"{table_id}_c2_0", row_index=2, col_index=0, coordinate="A3", raw_value="2024", normalized_value=2024),
                TableCell(cell_id=f"{table_id}_c2_1", row_index=2, col_index=1, coordinate="B3", raw_value="agriculture", normalized_value="agriculture"),
                TableCell(cell_id=f"{table_id}_c2_2", row_index=2, col_index=2, coordinate="C3", raw_value="300.00", normalized_value=Decimal("300.00")),
            ),
        ),
    )
    table = StructuredTable(table_id=table_id, document_id=doc_id, sheet_name=None, page_number=None, schema=schema, rows=rows)
    store.store_table(table, ws)
    store.publish_active_version(
        table_id,
        ws,
        doc_id,
        "v1",
        {"coverage_complete": True, "source_rows": 3, "persisted_rows": 3, "rejected_rows": 0},
    )


def test_filtered_and_whole_table_totals_differ_and_filtered_answer_correct():
    store = MemoryTableStore()
    ws = "ws_filter_differ"
    _create_sample_sales_table(store, ws)
    svc = StructuredQueryService(store)

    # Whole table sum query (unfiltered)
    whole_res = svc.execute("What is the total Amount in sales_data.csv?", workspace_id=ws)
    assert whole_res is not None
    assert whole_res.result_value == Decimal("600.00")
    assert whole_res.selection_count == 3
    assert whole_res.predicate_columns == []
    assert whole_res.predicate_values == []

    # Filtered query for tech (should sum 100.00 in 2023 + 200.00 in 2024 = 300.00)
    filtered_res = svc.execute("What is the total Amount for tech in sales_data.csv?", workspace_id=ws)
    assert filtered_res is not None
    assert filtered_res.result_value == Decimal("300.00")
    assert filtered_res.selection_count == 2
    assert "Industry" in filtered_res.predicate_columns
    assert "tech" in filtered_res.predicate_values

    # Filtered and whole table totals must differ and the filtered answer is correct
    assert filtered_res.result_value != whole_res.result_value
    assert filtered_res.result_value == Decimal("300.00")


def test_multiple_requested_filters_applied_together():
    store = MemoryTableStore()
    ws = "ws_multiple_filters"
    _create_sample_sales_table(store, ws)
    svc = StructuredQueryService(store)

    # Query requesting both tech and 2024
    res = svc.execute("What is the total Amount for tech in 2024 in sales_data.csv?", workspace_id=ws)
    assert res is not None
    assert res.result_value == Decimal("200.00")
    assert res.selection_count == 1
    assert "Year" in res.predicate_columns
    assert "Industry" in res.predicate_columns
    assert 2024 in res.predicate_values
    assert "tech" in res.predicate_values
    assert res.row_ids == ["sales_data_r1"]
    assert res.row_indices == [1]


def test_unsupported_filters_cannot_produce_whole_table_totals():
    store = MemoryTableStore()
    ws = "ws_unsupported_filter"
    _create_sample_sales_table(store, ws)
    svc = StructuredQueryService(store)

    # 1. Unknown entity that cannot be resolved in any column
    with pytest.raises(UnresolvedFilterError) as exc_info:
        svc.execute("What is the total Amount for NonExistentSector in sales_data.csv?", workspace_id=ws)
    assert "NonExistentSector" in exc_info.value.filter_values

    # 2. Unknown column specified in filter condition
    with pytest.raises(UnresolvedFilterError) as exc_info_col:
        svc.execute("What is the total Amount where FakeColumn is 100 in sales_data.csv?", workspace_id=ws)
    assert "FakeColumn" in exc_info_col.value.filter_values


def test_valid_filter_matching_zero_rows_distinguished_from_binding_failure():
    store = MemoryTableStore()
    ws = "ws_zero_rows_filter"
    _create_sample_sales_table(store, ws)
    svc = StructuredQueryService(store)

    # 1. Filter on valid column 'Year' with non-existent year 2099
    res_year = svc.execute("What is the total Amount where Year = 2099 in sales_data.csv?", workspace_id=ws)
    assert res_year is not None
    assert res_year.selection_count == 0
    assert res_year.result_value == Decimal(0)
    assert res_year.row_ids == []
    assert res_year.row_indices == []
    assert "Year" in res_year.predicate_columns
    assert 2099 in res_year.predicate_values
    assert "0" in res_year.answer_text

    # 2. Filter on valid column 'Industry' with non-existent industry 'aerospace'
    res_ind = svc.execute("What is the total Amount where Industry is aerospace in sales_data.csv?", workspace_id=ws)
    assert res_ind is not None
    assert res_ind.selection_count == 0
    assert res_ind.result_value == Decimal(0)
    assert res_ind.row_ids == []
    assert res_ind.row_indices == []
    assert "Industry" in res_ind.predicate_columns
    assert "aerospace" in res_ind.predicate_values


def test_result_evidence_records_applied_predicates_and_selected_rows():
    from services.retrieval.run_structured_aggregate import build_structured_evidence_ref

    store = MemoryTableStore()
    ws = "ws_evidence_records"
    _create_sample_sales_table(store, ws)
    svc = StructuredQueryService(store)

    res = svc.execute("What is the total Amount for tech in 2024 in sales_data.csv?", workspace_id=ws)
    assert res is not None

    # Verify StructuredQueryResult fields
    assert "Year" in res.predicate_columns
    assert "Industry" in res.predicate_columns
    assert 2024 in res.predicate_values
    assert "tech" in res.predicate_values
    assert res.selection_count == 1
    assert res.row_ids == ["sales_data_r1"]
    assert res.row_indices == [1]
    assert len(res.selection_hash) == 16

    # Verify citation reference payload
    citation = build_structured_evidence_ref(res, ws)
    assert citation["evidence_type"] == "structured_aggregate"
    assert citation["table_id"] == "sales_data"
    assert "Year" in citation["predicate_columns"]
    assert "Industry" in citation["predicate_columns"]
    assert "2024" in citation["predicate_values"]
    assert "tech" in citation["predicate_values"]
    assert citation["selection_count"] == 1
    assert citation["selection_hash"] == res.selection_hash
    assert citation["result_value"] == "200.00"
    assert "1 row(s) from sales_data" in citation["location_reference"]


def test_multiple_years_bind_with_in_predicate():
    store = MemoryTableStore()
    ws = "ws_multi_year_in"
    _create_sample_sales_table(store, ws)
    svc = StructuredQueryService(store)

    res = svc.execute("What is the total Amount for tech in 2023 and 2024 in sales_data.csv?", workspace_id=ws)
    assert res is not None
    assert res.result_value == Decimal("300.00")
    assert res.selection_count == 2
    assert "Year" in res.predicate_columns
    assert "Industry" in res.predicate_columns
    assert [2023, 2024] in res.predicate_values
    assert "tech" in res.predicate_values
    assert res.row_ids == ["sales_data_r0", "sales_data_r1"]
    assert res.row_indices == [0, 1]




