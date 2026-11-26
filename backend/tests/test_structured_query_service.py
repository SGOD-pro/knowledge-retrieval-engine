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


def test_zero_row_sum_refuses_while_zero_value_row_and_count_zero_succeed(monkeypatch):
    """Q159 regression: SUM with 0 matching rows must refuse via QueryService; supported 0 value and COUNT=0 succeed."""
    from db.table_store.memory_store import MemoryTableStore
    from modules.query.query_service import QueryRequest, QueryService
    from services.evaluation.benchmark_scorer import evaluate_grounded_answer
    import services.retrieval.run_structured_aggregate as rsa

    refusal_indicators = [
        "couldn't find any relevant",
        "not found",
        "does not contain",
        "not mentioned",
        "no information",
        "cannot find",
        "absent",
        "no positive source",
    ]

    def _derive_ans_is_refusal(ans_text: str) -> bool:
        return any(ind in ans_text.lower() for ind in refusal_indicators) or ans_text.strip() == "NOT_FOUND"

    store = MemoryTableStore()
    ws = "ws_q159_controls"
    _create_sample_sales_table(store, ws)
    tbl = store.get_table("sales_data", ws, version="v1")

    monkeypatch.setattr(rsa, "get_shared_table_store", lambda: store)
    monkeypatch.setattr(rsa, "_resolve_document_filename", lambda *args, **kwargs: "sales_data.csv")

    class _DocRepo:
        def __init__(self, doc_id: str, filename: str):
            self._docs = [{"id": doc_id, "filename": filename}]
        def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
            return {"documents": self._docs}
        def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
            return []
        def get(self, doc_id: str):
            return None
        def search_vector(self, *args, **kwargs):
            return []
        def check_semantic_cache(self, *args, **kwargs):
            return None

    from unittest.mock import patch
    doc_repo = _DocRepo(tbl.document_id, "sales_data.csv")
    qs = QueryService(repo=doc_repo)

    # 1. Q159 case via QueryService: SUM on absent year 2099 (0 matching rows) -> refusal derived by runner logic
    with patch("services.langgraph_pipeline._get_repo", return_value=doc_repo):
        resp_refuse = qs.execute_query(
            QueryRequest(
                query="What is the 2099 total Amount in sales_data.csv?",
                workspace_id=ws,
                document_ids=[tbl.document_id],
                benchmark_mode=True,
                cache=False,
            )
        )
    ans_refuse = str(resp_refuse.get("answer") or "")
    assert "does not contain" in ans_refuse.lower()
    assert resp_refuse["citations"] == []
    assert resp_refuse["retained_evidence_items"] == []
    derived_refusal = _derive_ans_is_refusal(ans_refuse)
    assert derived_refusal is True
    ev_ref = evaluate_grounded_answer(
        answer=ans_refuse,
        citations=resp_refuse["citations"],
        is_answer_text_correct=derived_refusal,
        contract_type="refusal",
        ans_is_refusal=derived_refusal,
        outcome=resp_refuse,
    )
    assert ev_ref["is_grounded_correct"] is True

    # 2. Control via QueryService: supported SUM whose value is 0 (matching row exists)
    store_zero = MemoryTableStore()
    zero_only_tbl = StructuredTable(
        table_id="zero_val_table",
        document_id="doc_zero_val",
        sheet_name=None,
        page_number=None,
        schema=tbl.schema,
        rows=(
            TableRow(
                row_index=0,
                row_id="r_zero_2025",
                cells=(
                    TableCell(cell_id="cz_0", row_index=0, col_index=0, coordinate="A1", raw_value="2025", normalized_value=2025),
                    TableCell(cell_id="cz_1", row_index=0, col_index=1, coordinate="B1", raw_value="Finance", normalized_value="Finance"),
                    TableCell(cell_id="cz_2", row_index=0, col_index=2, coordinate="C1", raw_value="0", normalized_value=Decimal("0")),
                ),
            ),
        ),
    )
    store_zero.store_table(zero_only_tbl, ws)
    store_zero.publish_active_version("zero_val_table", ws, "doc_zero_val", "v1", {"coverage_complete": True, "source_rows": 1, "persisted_rows": 1, "rejected_rows": 0})
    monkeypatch.setattr(rsa, "get_shared_table_store", lambda: store_zero)
    monkeypatch.setattr(rsa, "_resolve_document_filename", lambda *args, **kwargs: "zero_val_table.csv")

    doc_repo_zero = _DocRepo("doc_zero_val", "zero_val_table.csv")
    qs_zero = QueryService(repo=doc_repo_zero)
    with patch("services.langgraph_pipeline._get_repo", return_value=doc_repo_zero):
        resp_zero = qs_zero.execute_query(
            QueryRequest(
                query="What is the 2025 total Amount in zero_val_table.csv?",
                workspace_id=ws,
                document_ids=["doc_zero_val"],
                benchmark_mode=True,
                cache=False,
            )
        )
    ans_zero = str(resp_zero.get("answer") or "")
    assert "0" in ans_zero
    assert len(resp_zero["citations"]) == 1
    assert _derive_ans_is_refusal(ans_zero) is False
    ev_zero = evaluate_grounded_answer(
        answer=ans_zero,
        citations=resp_zero["citations"],
        is_answer_text_correct=True,
        contract_type="structured_aggregate",
        expected_sources=["zero_val_table.csv"],
        expected_source_version="v1",
        retained_evidence_items=resp_zero["retained_evidence_items"],
        question_type="structured_aggregate",
        outcome=resp_zero,
    )
    assert ev_zero["is_grounded_correct"] is True

    # 3. Control: valid COUNT of 0
    monkeypatch.setattr(rsa, "get_shared_table_store", lambda: store)
    monkeypatch.setattr(rsa, "_resolve_document_filename", lambda *args, **kwargs: "sales_data.csv")
    svc_cnt = StructuredQueryService(store)
    cnt_res = svc_cnt.execute("Count the rows where Year = 2099 in sales_data.csv?", workspace_id=ws)
    assert cnt_res is not None
    assert cnt_res.operator == "count"
    assert cnt_res.selection_count == 0
    assert cnt_res.result_value == Decimal(0)
    assert "count is 0" in cnt_res.answer_text.lower()


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


def test_metric_delta_word_boundary_column_matching():
    """Verify that short column names (e.g. 'id') are not selected by substring containment in conversational words (e.g. 'provided')."""
    store = MemoryTableStore()
    ws = "ws_metric_word_boundary"
    table_id = "finance_summary"
    doc_id = "doc_finance"

    cols = (
        ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
        ColumnDefinition(col_index=1, name="id", inferred_dtype=InferredDtype.INTEGER),
        ColumnDefinition(col_index=2, name="revenue", inferred_dtype=InferredDtype.DECIMAL),
    )
    schema = TableSchema(
        columns=cols,
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )
    rows = (
        TableRow(
            row_index=0,
            row_id=f"{table_id}_r0",
            cells=(
                TableCell(cell_id="c0_0", row_index=0, col_index=0, coordinate="A1", raw_value="2024", normalized_value=2024),
                TableCell(cell_id="c0_1", row_index=0, col_index=1, coordinate="B1", raw_value="1", normalized_value=1),
                TableCell(cell_id="c0_2", row_index=0, col_index=2, coordinate="C1", raw_value="100.00", normalized_value=Decimal("100.00")),
            ),
        ),
        TableRow(
            row_index=1,
            row_id=f"{table_id}_r1",
            cells=(
                TableCell(cell_id="c1_0", row_index=1, col_index=0, coordinate="A2", raw_value="2025", normalized_value=2025),
                TableCell(cell_id="c1_1", row_index=1, col_index=1, coordinate="B2", raw_value="1", normalized_value=1),
                TableCell(cell_id="c1_2", row_index=1, col_index=2, coordinate="C2", raw_value="150.00", normalized_value=Decimal("150.00")),
            ),
        ),
    )
    table = StructuredTable(table_id=table_id, document_id=doc_id, sheet_name=None, page_number=None, schema=schema, rows=rows)
    store.store_table(table, ws)
    store.publish_active_version(table_id, ws, doc_id, "v1", {"coverage_complete": True, "source_rows": 2, "persisted_rows": 2, "rejected_rows": 0})

    svc = StructuredQueryService(store)
    # The word "provided" contains "id" as a substring.
    # If substring containment were used, column 'id' would be selected and delta would be 0.
    # With word boundary regex matching, column 'revenue' is correctly selected and delta is 50.00.
    query = "how did revenue change from 2024 to 2025 in the provided report for finance_summary?"
    res = svc.execute(query, workspace_id=ws)
    assert res is not None
    assert res.result_value == Decimal("50.00")
    assert res.secondary_value == Decimal("50.0")
    assert "from 100.00 to 150.00" in res.answer_text


def test_min_max_range_word_boundary_column_matching():
    """Verify that min/max range column resolution does not match short column names by substring."""
    store = MemoryTableStore()
    ws = "ws_min_max_word_boundary"
    table_id = "inventory"
    doc_id = "doc_inv"

    cols = (
        ColumnDefinition(col_index=0, name="id", inferred_dtype=InferredDtype.INTEGER),
        ColumnDefinition(col_index=1, name="Year", inferred_dtype=InferredDtype.INTEGER),
    )
    schema = TableSchema(
        columns=cols,
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )
    rows = (
        TableRow(
            row_index=0,
            row_id="inv_r0",
            cells=(
                TableCell(cell_id="i0_0", row_index=0, col_index=0, coordinate="A1", raw_value="10", normalized_value=10),
                TableCell(cell_id="i0_1", row_index=0, col_index=1, coordinate="B1", raw_value="2010", normalized_value=2010),
            ),
        ),
        TableRow(
            row_index=1,
            row_id="inv_r1",
            cells=(
                TableCell(cell_id="i1_0", row_index=1, col_index=0, coordinate="A2", raw_value="20", normalized_value=20),
                TableCell(cell_id="i1_1", row_index=1, col_index=1, coordinate="B2", raw_value="2020", normalized_value=2020),
            ),
        ),
    )
    table = StructuredTable(table_id=table_id, document_id=doc_id, sheet_name=None, page_number=None, schema=schema, rows=rows)
    store.store_table(table, ws)
    store.publish_active_version(table_id, ws, doc_id, "v1", {"coverage_complete": True, "source_rows": 2, "persisted_rows": 2, "rejected_rows": 0})

    svc = StructuredQueryService(store)
    # The word "valid" contains "id" as a substring.
    # Query mentions Year explicitly, but also has "valid".
    # With word boundary regex, "Year" is matched as the target column.
    query = "What is the earliest and latest Year in the valid inventory table?"
    res = svc.execute(query, workspace_id=ws)
    assert res is not None
    assert res.result_value == 2010
    assert res.secondary_value == 2020


def test_timed_store_proxy_stream_rows_and_execute_single_pass_query():
    import time
    from db.table_store.base import AggregateOp
    from services.retrieval.structured_query_service import _TimedStoreProxy

    class _StubStreamStore:
        def stream_rows(self, *args, **kwargs):
            time.sleep(0.015)
            yield "row1"
            time.sleep(0.015)
            yield "row2"

        def execute_single_pass_query(self, *args, **kwargs):
            time.sleep(0.015)
            return (Decimal("10"), 1, ["r1"], [0], "0" * 16)

    proxy = _TimedStoreProxy(_StubStreamStore())
    for _row in proxy.stream_rows("t", "ws"):
        time.sleep(0.040)  # caller processing time must be excluded

    assert proxy.row_reads_ms >= 25.0
    assert proxy.row_reads_ms < 70.0

    class _FailingStreamStore:
        def stream_rows(self, *args, **kwargs):
            time.sleep(0.015)
            yield "row1"
            time.sleep(0.015)
            raise RuntimeError("iterator broke")

    proxy_fail = _TimedStoreProxy(_FailingStreamStore())
    with pytest.raises(RuntimeError, match="iterator broke"):
        for _row in proxy_fail.stream_rows("t", "ws"):
            time.sleep(0.030)

    assert proxy_fail.row_reads_ms >= 25.0
    assert proxy_fail.row_reads_ms < 65.0

    proxy.execute_single_pass_query("t", "ws", 0, AggregateOp.MIN)
    assert proxy.aggregate_ms >= 10.0


def test_dynamodb_store_pagination_retry_and_telemetry(monkeypatch):
    import json as _json
    from db.table_store.dynamodb_store import DynamoDBTableStore
    from services.retrieval.run_structured_aggregate import execute as run_agg_execute

    class _StubDynamoTable:
        def __init__(self, fail_on_second_page: bool = False):
            self.fail_on_second_page = fail_on_second_page
            self.query_calls = 0

        def get_item(self, Key):
            pk, sk = Key["PK"], Key["SK"]
            if sk == "ACTIVE_VERSION":
                return {"Item": {"active_version": "v1", "coverage_complete": True}}
            if sk.startswith("MANIFEST"):
                return {"Item": {"coverage_complete": True, "persisted_rows": 2, "document_version": "v1"}}
            if sk == "SCHEMA":
                schema_json = _json.dumps({
                    "columns": [
                        {"col_index": 0, "name": "Year", "inferred_dtype": "integer", "nullable": False, "cardinality": 2, "sample_values": ["2024", "2025"]},
                        {"col_index": 1, "name": "Revenue", "inferred_dtype": "decimal", "nullable": False, "cardinality": 2, "sample_values": ["100", "200"]},
                    ],
                    "header_rows": [0],
                    "header_topology": "single_row",
                    "confidence": 1.0,
                })
                return {"Item": {"document_id": "doc1", "schema_json": schema_json}}
            return {}

        def query(self, **kwargs):
            pk = str(kwargs.get("KeyConditionExpression", ""))
            # Distinguish workspace table list vs row pagination
            if "ExclusiveStartKey" not in kwargs and self.query_calls == 0 and kwargs.get("_is_list"):
                return {"Items": [{"table_id": "sales_tbl"}]}
            self.query_calls += 1
            if "ExclusiveStartKey" not in kwargs:
                # First call could be list_tables or first page of rows
                if self.query_calls == 1:
                    return {"Items": [{"table_id": "sales_tbl"}], "ResponseMetadata": {"RetryAttempts": 0}}
                return {
                    "Items": [{"row_id": "r1", "row_index": 0, "cells_json": _json.dumps([{"row_index": 0, "col_index": 0, "raw_value": "2024", "normalized_value": 2024}, {"row_index": 0, "col_index": 1, "raw_value": "100", "normalized_value": "100"}])}],
                    "LastEvaluatedKey": {"PK": "p", "SK": "k1"},
                    "ResponseMetadata": {"RetryAttempts": 2},
                }
            if self.fail_on_second_page:
                raise RuntimeError("ddb page 2 failure")
            return {
                "Items": [{"row_id": "r2", "row_index": 1, "cells_json": _json.dumps([{"row_index": 1, "col_index": 0, "raw_value": "2025", "normalized_value": 2025}, {"row_index": 1, "col_index": 1, "raw_value": "200", "normalized_value": "200"}])}],
                "ResponseMetadata": {"RetryAttempts": 1},
            }

    class _StubResource:
        def __init__(self, tbl):
            self._tbl = tbl

        def Table(self, name):
            return self._tbl

    store_ok = DynamoDBTableStore(table_name="stub_tbl", resource=_StubResource(_StubDynamoTable(fail_on_second_page=False)))
    monkeypatch.setattr("services.retrieval.run_structured_aggregate.get_shared_table_store", lambda: store_ok)
    out_ok = run_agg_execute({"workspace_id": "ws1", "query": "What is the earliest and latest Year in sales_tbl?", "stage_timings": {}})
    assert out_ok["executed_path"] == "structured_aggregate"
    st_ok = out_ok["stage_timings"]
    assert st_ok["dynamodb_query_pages"] >= 2
    assert st_ok["dynamodb_scan_pages"] == 0
    assert st_ok["dynamodb_retry_count"] >= 3
    assert st_ok["dynamodb_request_ms"] >= 0.0
    assert st_ok["dynamodb_fallback_reason"] is None

    store_fail = DynamoDBTableStore(table_name="stub_tbl", resource=_StubResource(_StubDynamoTable(fail_on_second_page=True)))
    monkeypatch.setattr("services.retrieval.run_structured_aggregate.get_shared_table_store", lambda: store_fail)
    out_fail = run_agg_execute({"workspace_id": "ws1", "query": "What is the earliest and latest Year in sales_tbl?", "stage_timings": {}})
    assert out_fail["executed_path"] == "structured_aggregate_storage_failure"
    st_fail = out_fail["stage_timings"]
    assert st_fail["dynamodb_query_pages"] >= 1
    assert st_fail["dynamodb_retry_count"] >= 2
    assert st_fail["dynamodb_request_ms"] >= 0.0


def test_documents_repository_gsi1_fallback_to_scan_telemetry(monkeypatch):
    from modules.documents.documents_repository import DocumentsRepository

    class _StubDocTable:
        def __init__(self):
            self.scan_calls = 0

        def query(self, **kwargs):
            if kwargs.get("IndexName") == "GSI1":
                raise RuntimeError("ValidationException: The table does not have the specified index: GSI1")
            return {"Items": [], "ResponseMetadata": {"RetryAttempts": 1}}

        def scan(self, **kwargs):
            raise AssertionError("scan must not be called on missing-GSI1 fallback")

    repo = DocumentsRepository.__new__(DocumentsRepository)
    repo.table = _StubDocTable()
    monkeypatch.setattr("modules.documents.documents_repository._is_test_env", lambda: False)
    monkeypatch.setattr("services.retrieval.bm25_retriever.get_cached_chunks", lambda ws, fn: fn())

    chunks = repo.get_all_chunks(workspace_id="ws_gsi_test")
    assert chunks == []
    tel = repo.last_query_telemetry
    assert tel["dynamodb_query_pages"] == 1
    assert tel["dynamodb_scan_pages"] == 0
    assert tel["dynamodb_retry_count"] == 1
    assert tel["dynamodb_request_ms"] >= 0.0
    assert "GSI1" in str(tel["dynamodb_fallback_reason"])


def test_min_max_range_single_traversal_three_pages_and_grounded_citation(monkeypatch):
    import json as _json
    from db.table_store.dynamodb_store import DynamoDBTableStore
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from schemas.contracts.boundary import DocumentSnapshot
    from services.evaluation.benchmark_scorer import evaluate_grounded_answer
    import services.retrieval.run_structured_aggregate as rsa

    class _ThreePageRegionalTable:
        def __init__(self):
            self.query_log: list[dict[str, Any]] = []

        def get_item(self, Key):
            sk = Key["SK"]
            if sk == "ACTIVE_VERSION":
                return {"Item": {"active_version": "v1", "coverage_complete": True}}
            if sk.startswith("MANIFEST"):
                return {"Item": {"coverage_complete": True, "persisted_rows": 7, "document_version": "v1"}}
            if sk == "SCHEMA":
                schema_json = _json.dumps({
                    "columns": [
                        {"col_index": 0, "name": "Year", "inferred_dtype": "integer", "nullable": False, "cardinality": 5, "sample_values": ["2010", "2018", "2020", "2025", "2035"]},
                        {"col_index": 1, "name": "Region", "inferred_dtype": "string", "nullable": False, "cardinality": 2, "sample_values": ["North", "South"]},
                    ],
                    "header_rows": [0],
                    "header_topology": "single_row",
                    "confidence": 1.0,
                })
                return {"Item": {"document_id": "doc_reg", "schema_json": schema_json}}
            return {}

        def query(self, **kwargs):
            start_key = kwargs.get("ExclusiveStartKey")
            self.query_log.append({"start_key": start_key})
            if start_key is None:
                return {
                    "Items": [
                        # Filtered-out row BELOW matching MIN (2018)
                        {"row_id": "r1", "row_index": 0, "cells_json": _json.dumps([{"row_index": 0, "col_index": 0, "raw_value": "2010", "normalized_value": 2010}, {"row_index": 0, "col_index": 1, "raw_value": "South", "normalized_value": "South"}])},
                        # Matching non-extremum row on page 1
                        {"row_id": "r2", "row_index": 1, "cells_json": _json.dumps([{"row_index": 1, "col_index": 0, "raw_value": "2020", "normalized_value": 2020}, {"row_index": 1, "col_index": 1, "raw_value": "North", "normalized_value": "North"}])},
                    ],
                    "LastEvaluatedKey": {"PK": "p", "SK": "k1"},
                    "ResponseMetadata": {"RetryAttempts": 0},
                }
            if start_key == {"PK": "p", "SK": "k1"}:
                return {
                    "Items": [
                        # Matching MIN on page 2
                        {"row_id": "r3", "row_index": 2, "cells_json": _json.dumps([{"row_index": 2, "col_index": 0, "raw_value": "2018", "normalized_value": 2018}, {"row_index": 2, "col_index": 1, "raw_value": "North", "normalized_value": "North"}])},
                        # Null value in target column -> skipped
                        {"row_id": "r4", "row_index": 3, "cells_json": _json.dumps([{"row_index": 3, "col_index": 0, "raw_value": "", "normalized_value": None}, {"row_index": 3, "col_index": 1, "raw_value": "North", "normalized_value": "North"}])},
                    ],
                    "LastEvaluatedKey": {"PK": "p", "SK": "k2"},
                    "ResponseMetadata": {"RetryAttempts": 0},
                }
            return {
                "Items": [
                    # Non-numeric value in target column -> skipped
                    {"row_id": "r5", "row_index": 4, "cells_json": _json.dumps([{"row_index": 4, "col_index": 0, "raw_value": "N/A", "normalized_value": "N/A"}, {"row_index": 4, "col_index": 1, "raw_value": "North", "normalized_value": "North"}])},
                    # Matching MAX on page 3
                    {"row_id": "r6", "row_index": 5, "cells_json": _json.dumps([{"row_index": 5, "col_index": 0, "raw_value": "2025", "normalized_value": 2025}, {"row_index": 5, "col_index": 1, "raw_value": "North", "normalized_value": "North"}])},
                    # Filtered-out row ABOVE matching MAX (2025)
                    {"row_id": "r7", "row_index": 6, "cells_json": _json.dumps([{"row_index": 6, "col_index": 0, "raw_value": "2035", "normalized_value": 2035}, {"row_index": 6, "col_index": 1, "raw_value": "South", "normalized_value": "South"}])},
                ],
                "ResponseMetadata": {"RetryAttempts": 0},
            }

    class _StubResource:
        def __init__(self, tbl):
            self._tbl = tbl

        def Table(self, name):
            return self._tbl

    raw_tbl = _ThreePageRegionalTable()
    store = DynamoDBTableStore(table_name="regional_metrics", resource=_StubResource(raw_tbl))
    monkeypatch.setattr(store, "list_tables", lambda ws: ["regional_metrics"])

    probe_pages: list[Any] = []
    range_pages: list[Any] = []
    orig_query_rows = store.query_rows

    def _spied_query_rows(*args, **kwargs):
        before = len(raw_tbl.query_log)
        res = orig_query_rows(*args, **kwargs)
        added = [entry["start_key"] for entry in raw_tbl.query_log[before:]]
        if kwargs.get("limit") is not None:
            probe_pages.extend(added)
        else:
            range_pages.extend(added)
        return res

    monkeypatch.setattr(store, "query_rows", _spied_query_rows)

    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id="ws_reg",
            document_id="doc_reg",
            source="regional_metrics.csv",
            version="v1",
            format="csv",
            incomplete_coverage_flag=False,
            persisted_rows=7,
            total_rows=7,
            created_at="2026-11-25T00:00:00Z",
        )
    )
    monkeypatch.setattr(rsa, "get_shared_table_store", lambda: store)
    monkeypatch.setattr(rsa, "get_workspace_boundary_repo", lambda: boundary_repo)

    out = rsa.execute({
        "workspace_id": "ws_reg",
        "query": "For North, what is the earliest and latest Year in regional_metrics?",
        "stage_timings": {},
    })
    assert out["executed_path"] == "structured_aggregate"
    assert out["final_answer"] == "2018 and 2025."
    # Verify exact single range traversal across all 3 pages in order
    assert range_pages == [None, {"PK": "p", "SK": "k1"}, {"PK": "p", "SK": "k2"}]
    # Predicate probing runs with limit=50 and stops once limit or pages finish
    assert len(probe_pages) >= 1
    assert out["stage_timings"]["row_reads_ms"] >= 0.0
    assert out["stage_timings"]["dynamodb_query_pages"] == len(probe_pages) + len(range_pages)

    grd = evaluate_grounded_answer(
        answer=out["final_answer"],
        citations=out["citations"],
        is_answer_text_correct=True,
        question_type="structured_aggregate",
        expected_sources=["regional_metrics.csv"],
        expected_source_version="v1",
        retained_evidence_items=out["retained_evidence_items"],
    )
    assert grd["is_grounded_correct"] is True


def test_min_max_range_ties_zero_matches_truncation_and_later_page_failure(monkeypatch):
    import json as _json
    from db.table_store.base import TableRowList
    from db.table_store.dynamodb_store import DynamoDBTableStore
    import services.retrieval.run_structured_aggregate as rsa

    class _EdgeCaseTable:
        def __init__(self, fail_on_page3: bool = False):
            self.fail_on_page3 = fail_on_page3

        def get_item(self, Key):
            sk = Key["SK"]
            if sk == "ACTIVE_VERSION":
                return {"Item": {"active_version": "v1", "coverage_complete": True}}
            if sk.startswith("MANIFEST"):
                return {"Item": {"coverage_complete": True, "persisted_rows": 5, "document_version": "v1"}}
            if sk == "SCHEMA":
                schema_json = _json.dumps({
                    "columns": [
                        {"col_index": 0, "name": "Year", "inferred_dtype": "integer", "nullable": False, "cardinality": 3, "sample_values": ["2018", "2020", "2025"]},
                        {"col_index": 1, "name": "Region", "inferred_dtype": "string", "nullable": False, "cardinality": 2, "sample_values": ["North", "East"]},
                        {"col_index": 2, "name": "Category", "inferred_dtype": "string", "nullable": False, "cardinality": 2, "sample_values": ["Hardware", "Software"]},
                    ],
                    "header_rows": [0],
                    "header_topology": "single_row",
                    "confidence": 1.0,
                })
                return {"Item": {"document_id": "doc_edge", "schema_json": schema_json}}
            return {}

        def query(self, **kwargs):
            sk = kwargs.get("ExclusiveStartKey")
            if sk is None:
                return {
                    "Items": [
                        {"row_id": "t1", "row_index": 0, "cells_json": _json.dumps([{"row_index": 0, "col_index": 0, "raw_value": "2018", "normalized_value": 2018}, {"row_index": 0, "col_index": 1, "raw_value": "North", "normalized_value": "North"}, {"row_index": 0, "col_index": 2, "raw_value": "Hardware", "normalized_value": "Hardware"}])},
                    ],
                    "LastEvaluatedKey": {"PK": "p", "SK": "k1"},
                    "ResponseMetadata": {"RetryAttempts": 0},
                }
            if sk == {"PK": "p", "SK": "k1"}:
                return {
                    "Items": [
                        {"row_id": "t2", "row_index": 1, "cells_json": _json.dumps([{"row_index": 1, "col_index": 0, "raw_value": "2018", "normalized_value": 2018}, {"row_index": 1, "col_index": 1, "raw_value": "North", "normalized_value": "North"}, {"row_index": 1, "col_index": 2, "raw_value": "Hardware", "normalized_value": "Hardware"}])},
                        {"row_id": "t3", "row_index": 2, "cells_json": _json.dumps([{"row_index": 2, "col_index": 0, "raw_value": "2025", "normalized_value": 2025}, {"row_index": 2, "col_index": 1, "raw_value": "North", "normalized_value": "North"}, {"row_index": 2, "col_index": 2, "raw_value": "Hardware", "normalized_value": "Hardware"}])},
                        {"row_id": "t5", "row_index": 4, "cells_json": _json.dumps([{"row_index": 4, "col_index": 0, "raw_value": "2020", "normalized_value": 2020}, {"row_index": 4, "col_index": 1, "raw_value": "East", "normalized_value": "East"}, {"row_index": 4, "col_index": 2, "raw_value": "Software", "normalized_value": "Software"}])},
                    ],
                    "LastEvaluatedKey": {"PK": "p", "SK": "k2"},
                    "ResponseMetadata": {"RetryAttempts": 0},
                }
            if self.fail_on_page3:
                raise RuntimeError("ddb page 3 failure")
            return {
                "Items": [
                    {"row_id": "t4", "row_index": 3, "cells_json": _json.dumps([{"row_index": 3, "col_index": 0, "raw_value": "2025", "normalized_value": 2025}, {"row_index": 3, "col_index": 1, "raw_value": "North", "normalized_value": "North"}, {"row_index": 3, "col_index": 2, "raw_value": "Hardware", "normalized_value": "Hardware"}])},
                ],
                "ResponseMetadata": {"RetryAttempts": 0},
            }

    class _StubResource:
        def __init__(self, tbl):
            self._tbl = tbl

        def Table(self, name):
            return self._tbl

    # 1. Ties across pages
    store_ties = DynamoDBTableStore(table_name="edge_tbl", resource=_StubResource(_EdgeCaseTable(fail_on_page3=False)))
    monkeypatch.setattr(store_ties, "list_tables", lambda ws: ["edge_tbl"])
    monkeypatch.setattr(rsa, "get_shared_table_store", lambda: store_ties)
    out_ties = rsa.execute({"workspace_id": "ws_edge", "query": "For North, what is the earliest and latest Year in edge_tbl?", "stage_timings": {}})
    assert out_ties["executed_path"] == "structured_aggregate"
    assert out_ties["final_answer"] == "2018 and 2025."
    assert out_ties["execution_result"].row_ids == ["t1", "t2", "t3", "t4"]

    # 2. Zero matches (both East and Hardware exist in edge_tbl, but their intersection is empty)
    out_zero = rsa.execute({"workspace_id": "ws_edge", "query": "For East Hardware, what is the earliest and latest Year in edge_tbl?", "stage_timings": {}})
    assert out_zero["executed_path"] == "structured_aggregate"
    assert out_zero["is_refusal"] is True
    assert out_zero["citations"] == []
    assert "No rows matched" in out_zero["final_answer"]

    # 3. Truncation reported -> no partial range
    orig_qr = store_ties.query_rows
    monkeypatch.setattr(store_ties, "query_rows", lambda *a, **kw: TableRowList([], is_truncated=True, scanned_count=1) if kw.get("limit") is None else orig_qr(*a, **kw))
    out_trunc = rsa.execute({"workspace_id": "ws_edge", "query": "For North, what is the earliest and latest Year in edge_tbl?", "stage_timings": {}})
    assert out_trunc["executed_path"] == "structured_aggregate_storage_failure"
    assert "citations" not in out_trunc or out_trunc["citations"] == []

    # 4. Later-page failure after earlier pages succeed -> no partial range, timings survive
    store_fail = DynamoDBTableStore(table_name="edge_tbl", resource=_StubResource(_EdgeCaseTable(fail_on_page3=True)))
    monkeypatch.setattr(store_fail, "list_tables", lambda ws: ["edge_tbl"])
    monkeypatch.setattr(rsa, "get_shared_table_store", lambda: store_fail)
    out_fail = rsa.execute({"workspace_id": "ws_edge", "query": "For North, what is the earliest and latest Year in edge_tbl?", "stage_timings": {}})
    assert out_fail["executed_path"] == "structured_aggregate_storage_failure"
    assert "citations" not in out_fail or out_fail["citations"] == []
    assert out_fail["stage_timings"]["table_lookup_ms"] >= 0.0
    assert out_fail["stage_timings"]["row_reads_ms"] >= 0.0
    assert out_fail["stage_timings"]["dynamodb_query_pages"] >= 3
    assert out_fail["stage_timings"]["dynamodb_request_ms"] >= 0.0


def test_metric_delta_compare_single_traversal_three_pages_and_grounded_citation(monkeypatch):
    import json as _json
    from db.table_store.dynamodb_store import DynamoDBTableStore
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from schemas.contracts.boundary import DocumentSnapshot
    from services.evaluation.benchmark_scorer import evaluate_grounded_answer
    import services.retrieval.run_structured_aggregate as rsa

    class _ThreePageDeltaTable:
        def __init__(self):
            self.query_log: list[dict[str, Any]] = []

        def get_item(self, Key):
            sk = Key["SK"]
            if sk == "ACTIVE_VERSION":
                return {"Item": {"active_version": "v1", "coverage_complete": True}}
            if sk.startswith("MANIFEST"):
                return {"Item": {"coverage_complete": True, "persisted_rows": 5, "document_version": "v1"}}
            if sk == "SCHEMA":
                schema_json = _json.dumps({
                    "columns": [
                        {"col_index": 0, "name": "Year", "inferred_dtype": "integer", "nullable": False, "cardinality": 3, "sample_values": ["2023", "2024", "2025"]},
                        {"col_index": 1, "name": "Sector", "inferred_dtype": "string", "nullable": False, "cardinality": 2, "sample_values": ["Alpha", "Beta"]},
                        {"col_index": 2, "name": "Revenue", "inferred_dtype": "decimal", "nullable": False, "cardinality": 5, "sample_values": ["500", "800", "900", "1000", "1250"]},
                    ],
                    "header_rows": [0],
                    "header_topology": "single_row",
                    "confidence": 1.0,
                })
                return {"Item": {"document_id": "doc_delta", "schema_json": schema_json}}
            return {}

        def query(self, **kwargs):
            start_key = kwargs.get("ExclusiveStartKey")
            self.query_log.append({"start_key": start_key})
            if start_key is None:
                return {
                    "Items": [
                        # Distractor: same shared predicate (Alpha), unrelated year (2023)
                        {"row_id": "d1", "row_index": 0, "cells_json": _json.dumps([{"row_index": 0, "col_index": 0, "raw_value": "2023", "normalized_value": 2023}, {"row_index": 0, "col_index": 1, "raw_value": "Alpha", "normalized_value": "Alpha"}, {"row_index": 0, "col_index": 2, "raw_value": "500", "normalized_value": "500"}])},
                        # Distractor: target year (2024), wrong predicate (Beta)
                        {"row_id": "d2", "row_index": 1, "cells_json": _json.dumps([{"row_index": 1, "col_index": 0, "raw_value": "2024", "normalized_value": 2024}, {"row_index": 1, "col_index": 1, "raw_value": "Beta", "normalized_value": "Beta"}, {"row_index": 1, "col_index": 2, "raw_value": "800", "normalized_value": "800"}])},
                    ],
                    "LastEvaluatedKey": {"PK": "p", "SK": "k1"},
                    "ResponseMetadata": {"RetryAttempts": 0},
                }
            if start_key == {"PK": "p", "SK": "k1"}:
                return {
                    "Items": [
                        # Start-year operand (Alpha, 2024, 1000) on page 2
                        {"row_id": "d3", "row_index": 2, "cells_json": _json.dumps([{"row_index": 2, "col_index": 0, "raw_value": "2024", "normalized_value": 2024}, {"row_index": 2, "col_index": 1, "raw_value": "Alpha", "normalized_value": "Alpha"}, {"row_index": 2, "col_index": 2, "raw_value": "1000", "normalized_value": "1000"}])},
                        # Distractor: target year (2025), wrong predicate (Beta)
                        {"row_id": "d4", "row_index": 3, "cells_json": _json.dumps([{"row_index": 3, "col_index": 0, "raw_value": "2025", "normalized_value": 2025}, {"row_index": 3, "col_index": 1, "raw_value": "Beta", "normalized_value": "Beta"}, {"row_index": 3, "col_index": 2, "raw_value": "900", "normalized_value": "900"}])},
                    ],
                    "LastEvaluatedKey": {"PK": "p", "SK": "k2"},
                    "ResponseMetadata": {"RetryAttempts": 0},
                }
            return {
                "Items": [
                    # End-year operand (Alpha, 2025, 1250) on page 3
                    {"row_id": "d5", "row_index": 4, "cells_json": _json.dumps([{"row_index": 4, "col_index": 0, "raw_value": "2025", "normalized_value": 2025}, {"row_index": 4, "col_index": 1, "raw_value": "Alpha", "normalized_value": "Alpha"}, {"row_index": 4, "col_index": 2, "raw_value": "1250", "normalized_value": "1250"}])},
                ],
                "ResponseMetadata": {"RetryAttempts": 0},
            }

    class _StubResource:
        def __init__(self, tbl):
            self._tbl = tbl

        def Table(self, name):
            return self._tbl

    raw_tbl = _ThreePageDeltaTable()
    store = DynamoDBTableStore(table_name="sector_metrics", resource=_StubResource(raw_tbl))
    monkeypatch.setattr(store, "list_tables", lambda ws: ["sector_metrics"])

    probe_pages: list[Any] = []
    operand_pages: list[Any] = []
    orig_query_rows = store.query_rows

    def _spied_query_rows(*args, **kwargs):
        before = len(raw_tbl.query_log)
        res = orig_query_rows(*args, **kwargs)
        added = [entry["start_key"] for entry in raw_tbl.query_log[before:]]
        if kwargs.get("limit") is not None:
            probe_pages.extend(added)
        else:
            operand_pages.extend(added)
        return res

    monkeypatch.setattr(store, "query_rows", _spied_query_rows)

    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id="ws_delta",
            document_id="doc_delta",
            source="sector_metrics.csv",
            version="v1",
            format="csv",
            incomplete_coverage_flag=False,
            persisted_rows=5,
            total_rows=5,
            created_at="2026-11-25T00:00:00Z",
        )
    )
    monkeypatch.setattr(rsa, "get_shared_table_store", lambda: store)
    monkeypatch.setattr(rsa, "get_workspace_boundary_repo", lambda: boundary_repo)

    out = rsa.execute({
        "workspace_id": "ws_delta",
        "query": "For Alpha, how did Revenue change from 2024 to 2025? Give the amount and percentage change in sector_metrics.",
        "stage_timings": {},
    })
    assert out["executed_path"] == "structured_aggregate"
    assert "250" in out["final_answer"] and "25.00%" in out["final_answer"]
    assert sorted(out["execution_result"].row_ids) == ["d3", "d5"]
    # Exactly ONE 3-page operand traversal (down from 2 traversals = 6 operand pages)
    assert operand_pages == [None, {"PK": "p", "SK": "k1"}, {"PK": "p", "SK": "k2"}]
    assert out["stage_timings"]["dynamodb_query_pages"] == len(probe_pages) + len(operand_pages)

    grd = evaluate_grounded_answer(
        answer=out["final_answer"],
        citations=out["citations"],
        is_answer_text_correct=True,
        question_type="structured_aggregate",
        expected_sources=["sector_metrics.csv"],
        expected_source_version="v1",
        retained_evidence_items=out["retained_evidence_items"],
    )
    assert grd["is_grounded_correct"] is True


def test_metric_delta_missing_operand_duplicate_and_interrupted_traversal(monkeypatch):
    import json as _json
    from db.table_store.base import TableRowList
    from db.table_store.dynamodb_store import DynamoDBTableStore
    import services.retrieval.run_structured_aggregate as rsa

    class _EdgeDeltaTable:
        def __init__(self, duplicate_end: bool = False, fail_on_page3: bool = False):
            self.duplicate_end = duplicate_end
            self.fail_on_page3 = fail_on_page3

        def get_item(self, Key):
            sk = Key["SK"]
            if sk == "ACTIVE_VERSION":
                return {"Item": {"active_version": "v1", "coverage_complete": True}}
            if sk.startswith("MANIFEST"):
                return {"Item": {"coverage_complete": True, "persisted_rows": 4, "document_version": "v1"}}
            if sk == "SCHEMA":
                schema_json = _json.dumps({
                    "columns": [
                        {"col_index": 0, "name": "Year", "inferred_dtype": "integer", "nullable": False, "cardinality": 2, "sample_values": ["2024", "2025"]},
                        {"col_index": 1, "name": "Sector", "inferred_dtype": "string", "nullable": False, "cardinality": 1, "sample_values": ["Alpha"]},
                        {"col_index": 2, "name": "Revenue", "inferred_dtype": "decimal", "nullable": False, "cardinality": 3, "sample_values": ["1000", "1250", "1300"]},
                    ],
                    "header_rows": [0],
                    "header_topology": "single_row",
                    "confidence": 1.0,
                })
                return {"Item": {"document_id": "doc_edge_d", "schema_json": schema_json}}
            return {}

        def query(self, **kwargs):
            sk = kwargs.get("ExclusiveStartKey")
            if sk is None:
                return {
                    "Items": [],
                    "LastEvaluatedKey": {"PK": "p", "SK": "k1"},
                    "ResponseMetadata": {"RetryAttempts": 0},
                }
            if sk == {"PK": "p", "SK": "k1"}:
                return {
                    "Items": [
                        {"row_id": "e1", "row_index": 0, "cells_json": _json.dumps([{"row_index": 0, "col_index": 0, "raw_value": "2024", "normalized_value": 2024}, {"row_index": 0, "col_index": 1, "raw_value": "Alpha", "normalized_value": "Alpha"}, {"row_index": 0, "col_index": 2, "raw_value": "1000", "normalized_value": "1000"}])},
                    ],
                    "LastEvaluatedKey": {"PK": "p", "SK": "k2"},
                    "ResponseMetadata": {"RetryAttempts": 0},
                }
            if self.fail_on_page3:
                raise RuntimeError("ddb page 3 delta failure")
            items = [
                {"row_id": "e2", "row_index": 1, "cells_json": _json.dumps([{"row_index": 1, "col_index": 0, "raw_value": "2025", "normalized_value": 2025}, {"row_index": 1, "col_index": 1, "raw_value": "Alpha", "normalized_value": "Alpha"}, {"row_index": 1, "col_index": 2, "raw_value": "1250", "normalized_value": "1250"}])},
            ]
            if self.duplicate_end:
                items.append(
                    {"row_id": "e3", "row_index": 2, "cells_json": _json.dumps([{"row_index": 2, "col_index": 0, "raw_value": "2025", "normalized_value": 2025}, {"row_index": 2, "col_index": 1, "raw_value": "Alpha", "normalized_value": "Alpha"}, {"row_index": 2, "col_index": 2, "raw_value": "1300", "normalized_value": "1300"}])},
                )
            return {"Items": items, "ResponseMetadata": {"RetryAttempts": 0}}

    class _StubResource:
        def __init__(self, tbl):
            self._tbl = tbl

        def Table(self, name):
            return self._tbl

    # 1. Missing operand (2026 not present)
    store_ok = DynamoDBTableStore(table_name="edge_d_tbl", resource=_StubResource(_EdgeDeltaTable()))
    monkeypatch.setattr(store_ok, "list_tables", lambda ws: ["edge_d_tbl"])
    monkeypatch.setattr(rsa, "get_shared_table_store", lambda: store_ok)
    out_missing = rsa.execute({
        "workspace_id": "ws_ed",
        "query": "For Alpha, how did Revenue change from 2024 to 2026? Give the amount and percentage change in edge_d_tbl.",
        "stage_timings": {},
    })
    assert out_missing["executed_path"] == "structured_aggregate"
    assert out_missing["is_refusal"] is True
    assert out_missing["citations"] == []
    assert "No rows matched" in out_missing["final_answer"]

    # 2. Duplicate for end year on page 3 -> AmbiguousBindingError
    store_dup = DynamoDBTableStore(table_name="edge_d_tbl", resource=_StubResource(_EdgeDeltaTable(duplicate_end=True)))
    monkeypatch.setattr(store_dup, "list_tables", lambda ws: ["edge_d_tbl"])
    monkeypatch.setattr(rsa, "get_shared_table_store", lambda: store_dup)
    out_dup = rsa.execute({
        "workspace_id": "ws_ed",
        "query": "For Alpha, how did Revenue change from 2024 to 2025? Give the amount and percentage change in edge_d_tbl.",
        "stage_timings": {},
    })
    assert out_dup["executed_path"] == "structured_aggregate_ambiguous"

    # 3. Truncated traversal -> no partial delta or citation
    monkeypatch.setattr(rsa, "get_shared_table_store", lambda: store_ok)
    orig_qr = store_ok.query_rows
    monkeypatch.setattr(store_ok, "query_rows", lambda *a, **kw: TableRowList([], is_truncated=True, scanned_count=1) if kw.get("limit") is None else orig_qr(*a, **kw))
    out_trunc = rsa.execute({
        "workspace_id": "ws_ed",
        "query": "For Alpha, how did Revenue change from 2024 to 2025? Give the amount and percentage change in edge_d_tbl.",
        "stage_timings": {},
    })
    assert out_trunc["executed_path"] == "structured_aggregate_storage_failure"
    assert "citations" not in out_trunc or out_trunc["citations"] == []

    # 4. Interrupted traversal on page 3 -> no partial delta, telemetry survives
    store_fail = DynamoDBTableStore(table_name="edge_d_tbl", resource=_StubResource(_EdgeDeltaTable(fail_on_page3=True)))
    monkeypatch.setattr(store_fail, "list_tables", lambda ws: ["edge_d_tbl"])
    monkeypatch.setattr(rsa, "get_shared_table_store", lambda: store_fail)
    out_fail = rsa.execute({
        "workspace_id": "ws_ed",
        "query": "For Alpha, how did Revenue change from 2024 to 2025? Give the amount and percentage change in edge_d_tbl.",
        "stage_timings": {},
    })
    assert out_fail["executed_path"] == "structured_aggregate_storage_failure"
    assert "citations" not in out_fail or out_fail["citations"] == []
    assert out_fail["stage_timings"]["table_lookup_ms"] >= 0.0
    assert out_fail["stage_timings"]["row_reads_ms"] >= 0.0
    assert out_fail["stage_timings"]["dynamodb_query_pages"] >= 3
    assert out_fail["stage_timings"]["dynamodb_request_ms"] >= 0.0


def test_how_many_records_count_and_incomplete_coverage():
    """Unseen 'How many records...' COUNT query routes, binds, and enforces coverage."""
    from services.retrieval.planner import planner
    from services.retrieval.structured_query_service import IncompleteDataError

    store = MemoryTableStore()
    ws = "ws_how_many_count"
    cols = (
        ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
        ColumnDefinition(col_index=1, name="Region", inferred_dtype=InferredDtype.STRING),
    )
    schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)
    rows = (
        TableRow(
            row_index=0,
            row_id="fdl_r0",
            cells=(
                TableCell(cell_id="fdl_c00", row_index=0, col_index=0, coordinate="A1", raw_value="2024", normalized_value=2024),
                TableCell(cell_id="fdl_c01", row_index=0, col_index=1, coordinate="B1", raw_value="North", normalized_value="North"),
            ),
        ),
        TableRow(
            row_index=1,
            row_id="fdl_r1",
            cells=(
                TableCell(cell_id="fdl_c10", row_index=1, col_index=0, coordinate="A2", raw_value="2024", normalized_value=2024),
                TableCell(cell_id="fdl_c11", row_index=1, col_index=1, coordinate="B2", raw_value="South", normalized_value="South"),
            ),
        ),
        TableRow(
            row_index=2,
            row_id="fdl_r2",
            cells=(
                TableCell(cell_id="fdl_c20", row_index=2, col_index=0, coordinate="A3", raw_value="2025", normalized_value=2025),
                TableCell(cell_id="fdl_c21", row_index=2, col_index=1, coordinate="B3", raw_value="East", normalized_value="East"),
            ),
        ),
    )
    table = StructuredTable(
        table_id="fleet_dispatch_log",
        document_id="doc_fleet",
        sheet_name=None,
        page_number=None,
        schema=schema,
        rows=rows,
    )
    store.store_table(table, ws)
    store.publish_active_version(
        "fleet_dispatch_log",
        ws,
        "doc_fleet",
        "v1",
        {"coverage_complete": True, "source_rows": 3, "persisted_rows": 3, "rejected_rows": 0},
    )

    q = "How many records have Year equal to 2024 in the fleet_dispatch_log CSV?"
    plan = planner.route(q)
    assert plan.use_structured_aggregate is True

    svc = StructuredQueryService(store)
    res = svc.execute(q, workspace_id=ws)
    assert res is not None
    assert res.operator == "count"
    assert res.selection_count == 2
    assert res.result_value == Decimal(2)
    assert res.predicate_columns == ["Year"]
    assert res.predicate_values == [2024]

    # Incomplete coverage must never return a complete count
    store.publish_active_version(
        "fleet_dispatch_log",
        ws,
        "doc_fleet",
        "v2",
        {"coverage_complete": False, "source_rows": 5, "persisted_rows": 3, "rejected_rows": 2},
    )
    with pytest.raises(IncompleteDataError):
        svc.execute(q, workspace_id=ws)


def test_structured_count_source_binding_and_safety_controls(monkeypatch):
    """Structured COUNT must bind to trusted requested source or refuse safely."""
    from unittest.mock import patch
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    import services.retrieval.run_structured_aggregate as rsa

    monkeypatch.setenv("ENVIRONMENT", "test")
    ws = "ws_count_source_safety"
    store = MemoryTableStore()
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Count Source Safety WS", "tester", workspace_id=ws)

    def _add_table(tid: str, doc_id: str, fname: str, years_units: list[tuple[int, str, str]], *, coverage_complete: bool = True, snap_ver: str = "v1", table_ver: str = "v1"):
        cols = (
            ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=1, name="Region", inferred_dtype=InferredDtype.STRING),
            ColumnDefinition(col_index=2, name="Units", inferred_dtype=InferredDtype.DECIMAL),
        )
        schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)
        rows = tuple(
            TableRow(
                row_index=i,
                row_id=f"{tid}_r{i}",
                cells=(
                    TableCell(cell_id=f"{tid}_c{i}_0", row_index=i, col_index=0, coordinate=f"A{i+1}", raw_value=str(yr), normalized_value=yr),
                    TableCell(cell_id=f"{tid}_c{i}_1", row_index=i, col_index=1, coordinate=f"B{i+1}", raw_value=reg, normalized_value=reg),
                    TableCell(cell_id=f"{tid}_c{i}_2", row_index=i, col_index=2, coordinate=f"C{i+1}", raw_value=val, normalized_value=Decimal(val)),
                ),
            )
            for i, (yr, reg, val) in enumerate(years_units)
        )
        tbl = StructuredTable(table_id=tid, document_id=doc_id, sheet_name=None, page_number=None, schema=schema, rows=rows)
        store.store_table(tbl, ws)
        store.publish_active_version(
            tid,
            ws,
            doc_id,
            table_ver,
            {"coverage_complete": coverage_complete, "source_rows": len(rows), "persisted_rows": len(rows), "rejected_rows": 0},
        )
        b_repo.create_source(ws, fname, source_id=doc_id)
        b_repo.save_document_snapshot(
            DocumentSnapshot(
                workspace_id=ws,
                document_id=doc_id,
                source=fname,
                version=snap_ver,
                format="csv",
                created_at="2026-11-26T00:00:00Z",
            )
        )

    # alpha has 2 rows for 2024; beta has 3 rows for 2024 (plus extra numeric column Units)
    _add_table("alpha_emissions_tbl", "doc-alpha", "alpha_emissions.csv", [(2024, "North", "100"), (2024, "South", "200"), (2025, "North", "150")])
    _add_table("beta_outages_tbl", "doc-beta", "beta_outages.csv", [(2024, "North", "10"), (2024, "East", "20"), (2024, "West", "30"), (2025, "North", "40")])

    class _MinimalDocRepo:
        def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
            return {"documents": [{"id": "doc-alpha", "filename": "alpha_emissions.csv"}, {"id": "doc-beta", "filename": "beta_outages.csv"}]}
        def get_all_chunks(self, *a, **kw): return []
        def get(self, doc_id: str): return None
        def search_vector(self, *a, **kw): return []
        def check_semantic_cache(self, *a, **kw): return None

    qs = QueryService(repo=_MinimalDocRepo())

    def _ask(query: str, document_ids: list[str] | None = None):
        req = QueryRequest(query=query, workspace_id=ws, document_ids=document_ids, cache=False, benchmark_mode=True)
        with (
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("db.table_store.get_shared_table_store", return_value=store),
            patch.object(rsa, "get_shared_table_store", return_value=store),
            patch.object(rsa, "get_workspace_boundary_repo", return_value=b_repo),
        ):
            return qs.execute_query(req)

    # 1. Trusted source filename/alias selects the right table
    out_alpha = _ask("How many records have Year equal to 2024 in the alpha_emissions CSV?")
    assert out_alpha["executed_path"] == "structured_aggregate"
    assert "2" in out_alpha["answer"] and "3" not in out_alpha["answer"]
    assert out_alpha["citations"][0]["document_filename"] == "alpha_emissions.csv"

    out_beta = _ask("How many records have Year equal to 2024 in the beta_outages CSV?")
    assert out_beta["executed_path"] == "structured_aggregate"
    assert "3" in out_beta["answer"]
    assert out_beta["citations"][0]["document_filename"] == "beta_outages.csv"

    # 2. Unresolvable explicit source reference must NEVER silently select another table
    out_unknown = _ask("How many records have Year equal to 2024 in the Gamma Telemetry CSV?")
    assert out_unknown["executed_path"] != "structured_aggregate"
    assert out_unknown["citations"] == []
    assert out_unknown.get("confidence_score", 0.0) == 0.0

    # 3a. Ambiguous (shared Year column, no source specified) must not guess
    out_ambig = _ask("How many records have Year equal to 2024 in the CSV?")
    assert out_ambig["executed_path"] != "structured_aggregate"
    assert out_ambig["citations"] == []

    # 3b. Out-of-scope document_ids filter
    out_scope = _ask("How many records have Year equal to 2024 in the beta_outages CSV?", document_ids=["doc-alpha"])
    assert out_scope["executed_path"] != "structured_aggregate"
    assert out_scope["citations"] == []

    # 3c. Supported zero count + SUM + delta controls
    out_zero = _ask("How many records have Year equal to 1999 in the alpha_emissions CSV?")
    assert out_zero["executed_path"] == "structured_aggregate"
    assert "0" in out_zero["answer"]

    out_sum = _ask("What is the total Units in the alpha_emissions CSV?")
    assert out_sum["executed_path"] == "structured_aggregate"
    assert "450" in out_sum["answer"]

    out_delta = _ask("For North, how did Units change from 2024 to 2025 in the alpha_emissions CSV?")
    assert out_delta["executed_path"] == "structured_aggregate"
    assert "50" in out_delta["answer"]

    # 3d. Stale snapshot version cannot produce a confident count
    b_repo.save_document_snapshot(
        DocumentSnapshot(workspace_id=ws, document_id="doc-alpha", source="alpha_emissions.csv", version="v999", format="csv", created_at="2026-11-26T00:00:00Z")
    )
    out_stale = _ask("How many records have Year equal to 2024 in the alpha_emissions CSV?")
    assert out_stale["executed_path"] != "structured_aggregate"
    assert out_stale["citations"] == []

    # Restore v1, then test revoked source (tombstone)
    b_repo.save_document_snapshot(
        DocumentSnapshot(workspace_id=ws, document_id="doc-alpha", source="alpha_emissions.csv", version="v1", format="csv", created_at="2026-11-26T00:00:00Z")
    )
    b_repo.record_source_tombstone(ws, "doc-alpha", "tester")
    out_revoked = _ask("How many records have Year equal to 2024 in the alpha_emissions CSV?")
    assert out_revoked["executed_path"] != "structured_aggregate"
    assert out_revoked["citations"] == []

    # 3e. Incomplete coverage on beta cannot produce a complete count
    store.publish_active_version(
        "beta_outages_tbl", ws, "doc-beta", "v1",
        {"coverage_complete": False, "source_rows": 10, "persisted_rows": 4, "rejected_rows": 6},
    )
    out_incomp = _ask("How many records have Year equal to 2024 in the beta_outages CSV?")
    assert out_incomp["executed_path"] != "structured_aggregate"
    assert out_incomp["citations"] == []
    assert "incomplete" in out_incomp["answer"].lower()


def test_structured_count_known_plus_missing_csv_refuses_and_same_source_aliases_succeed(monkeypatch):
    """COUNT requiring a known CSV and a missing CSV must never return the known CSV's count alone."""
    from unittest.mock import patch
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    import services.retrieval.run_structured_aggregate as rsa

    monkeypatch.setenv("ENVIRONMENT", "test")
    ws = "ws_count_known_plus_missing"
    store = MemoryTableStore()
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Known Plus Missing WS", "tester", workspace_id=ws)

    cols = (
        ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
        ColumnDefinition(col_index=1, name="Units", inferred_dtype=InferredDtype.DECIMAL),
    )
    schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)
    rows_a = (
        TableRow(row_index=0, row_id="a_r0", cells=(
            TableCell(cell_id="a_c0_0", row_index=0, col_index=0, coordinate="A1", raw_value="2024", normalized_value=2024),
            TableCell(cell_id="a_c0_1", row_index=0, col_index=1, coordinate="B1", raw_value="10", normalized_value=Decimal("10")),
        )),
        TableRow(row_index=1, row_id="a_r1", cells=(
            TableCell(cell_id="a_c1_0", row_index=1, col_index=0, coordinate="A2", raw_value="2024", normalized_value=2024),
            TableCell(cell_id="a_c1_1", row_index=1, col_index=1, coordinate="B2", raw_value="20", normalized_value=Decimal("20")),
        )),
    )
    tbl_a = StructuredTable(table_id="alpha_emissions_tbl", document_id="doc-alpha", sheet_name=None, page_number=None, schema=schema, rows=rows_a)
    store.store_table(tbl_a, ws)
    store.publish_active_version(
        "alpha_emissions_tbl", ws, "doc-alpha", "v1",
        {"coverage_complete": True, "source_rows": 2, "persisted_rows": 2, "rejected_rows": 0},
    )
    b_repo.create_source(ws, "alpha_emissions.csv", source_id="doc-alpha")
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws,
            document_id="doc-alpha",
            source="alpha_emissions.csv",
            version="v1",
            format="csv",
            created_at="2026-11-26T00:00:00Z",
        )
    )

    class _MinimalDocRepo:
        def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
            return {"documents": [{"id": "doc-alpha", "filename": "alpha_emissions.csv"}]}
        def get_all_chunks(self, *a, **kw): return []
        def get(self, doc_id: str): return None
        def search_vector(self, *a, **kw): return []
        def check_semantic_cache(self, *a, **kw): return None

    qs = QueryService(repo=_MinimalDocRepo())

    def _ask(query: str):
        req = QueryRequest(query=query, workspace_id=ws, cache=False, benchmark_mode=True)
        with (
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("db.table_store.get_shared_table_store", return_value=store),
            patch.object(rsa, "get_shared_table_store", return_value=store),
            patch.object(rsa, "get_workspace_boundary_repo", return_value=b_repo),
        ):
            return qs.execute_query(req)

    # 1. Single known CSV returns exact count
    out_single = _ask("How many records have Year equal to 2024 in alpha_emissions.csv?")
    assert out_single["executed_path"] == "structured_aggregate"
    assert "2" in out_single["answer"]

    # 2. Two names for the SAME active source do not create false ambiguity
    out_same_alias = _ask("How many records have Year equal to 2024 in alpha_emissions.csv and the alpha emissions CSV?")
    assert out_same_alias["executed_path"] == "structured_aggregate"
    assert "2" in out_same_alias["answer"]

    # 3. Known CSV + missing CSV must ask for clarification / refuse, NEVER return count 2 as total
    for q_missing in [
        "How many records have Year equal to 2024 across alpha_emissions.csv and gamma_telemetry.csv?",
        "How many records have Year equal to 2024 in the alpha_emissions CSV and the gamma_telemetry CSV?",
        "How many records have Year equal to 2024 using alpha_emissions.csv and missing_sheet.xlsx?",
    ]:
        out_missing = _ask(q_missing)
        assert out_missing["executed_path"] != "structured_aggregate", f"Leaked partial count for: {q_missing}"
        assert out_missing["citations"] == []
        assert (
            "clarification" in out_missing["answer"].lower()
            or "could not be resolved" in out_missing["answer"].lower()
            or "couldn't find" in out_missing["answer"].lower()
        )

    # 4. Generic qualifier ("valid") before matched table name does not trigger false missing-source error
    out_qualified = _ask("How many records have Year equal to 2024 in the valid alpha_emissions table?")
    assert out_qualified["executed_path"] == "structured_aggregate"
    assert "2" in out_qualified["answer"]









