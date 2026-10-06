"""Test edge cases, failure modes, unfamiliar schemas, and narrative router protection.

Requirements (User Requirement 5):
1. Unfamiliar column names and values
2. Requested value occurring after row 200
3. Leading-zero identifiers preserved
4. Nulls handled gracefully
5. Duplicate period matches raise AmbiguousBindingError
6. Zero denominator in percentage change handles without ZeroDivisionError
7. Unresolved required filter fails closed
8. Narrative questions containing 'year', 'table', or 'period' route to semantic/vector retrieval, not structured_table
"""

from decimal import Decimal
import pytest

from db.table_store.base import Predicate, PredicateOp
from db.table_store.memory_store import MemoryTableStore
from schemas.structured_table import (
    ColumnDefinition,
    HeaderTopology,
    InferredDtype,
    TableCell,
    TableRow,
    TableSchema,
)
from services.retrieval.strategy_router import StrategyRouter
from services.retrieval.structured_query_service import (
    AmbiguousBindingError,
    StructuredQueryService,
    UnresolvedFilterError,
    UnsupportedQueryError,
)


def _build_unfamiliar_schema() -> TableSchema:
    return TableSchema(
        columns=(
            ColumnDefinition(col_index=0, name="CycleYear", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=1, name="StationCode", inferred_dtype=InferredDtype.STRING),
            ColumnDefinition(col_index=2, name="ReadingValue", inferred_dtype=InferredDtype.DECIMAL),
            ColumnDefinition(col_index=3, name="StatusNote", inferred_dtype=InferredDtype.STRING),
        ),
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )


def test_unfamiliar_schema_and_column_names():
    """Unfamiliar column names (CycleYear, StationCode, ReadingValue) bind and execute correctly."""
    store = MemoryTableStore()
    schema = _build_unfamiliar_schema()
    ws = "ws_edge"
    doc_id = "doc_unfamiliar"
    ver = "v_unfamiliar"
    table_id = "telemetry_data"

    store.store_schema(table_id, ws, schema, doc_id, ver)

    rows = [
        TableRow(
            row_id="r1",
            row_index=1,
            cells=(
                TableCell(cell_id="c1_0", row_index=1, col_index=0, coordinate="A1", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c1_1", row_index=1, col_index=1, coordinate="B1", raw_value="StationX", normalized_value="StationX", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c1_2", row_index=1, col_index=2, coordinate="C1", raw_value="150.5", normalized_value=Decimal("150.5"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="c1_3", row_index=1, col_index=3, coordinate="D1", raw_value="Normal", normalized_value="Normal", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
        TableRow(
            row_id="r2",
            row_index=2,
            cells=(
                TableCell(cell_id="c2_0", row_index=2, col_index=0, coordinate="A2", raw_value="2025", normalized_value=2025, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c2_1", row_index=2, col_index=1, coordinate="B2", raw_value="StationX", normalized_value="StationX", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c2_2", row_index=2, col_index=2, coordinate="C2", raw_value="180.6", normalized_value=Decimal("180.6"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="c2_3", row_index=2, col_index=3, coordinate="D2", raw_value="Normal", normalized_value="Normal", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
    ]

    store.write_row_batch_with_checkpoint(
        table_id, ws, doc_id, ver, rows, 1, 2, 2, 2, 0, "1.0", "s1", "h1"
    )
    store.publish_active_version(
        table_id, ws, doc_id, ver,
        {"coverage_complete": True, "processing_finished": True, "source_rows": 2, "persisted_rows": 2, "rejected_rows": 0}
    )

    svc = StructuredQueryService(store)
    res = svc.execute(
        query="For StationX, how did ReadingValue change from 2024 to 2025?",
        workspace_id=ws,
    )
    assert res is not None
    assert res.operator == "compare"
    assert res.target_column == "ReadingValue"
    assert res.result_value == Decimal("30.1")
    assert "increased" in res.answer_text


def test_requested_value_after_row_200():
    """Candidate values located beyond the first 200 rows are resolved via direct store query."""
    store = MemoryTableStore()
    schema = _build_unfamiliar_schema()
    ws = "ws_edge"
    doc_id = "doc_row250"
    ver = "v_row250"
    table_id = "deep_data"

    store.store_schema(table_id, ws, schema, doc_id, ver)

    # Generate 250 rows; StationDeep only appears at row 240 and 241
    rows = []
    for i in range(1, 240):
        rows.append(TableRow(
            row_id=f"r_{i}",
            row_index=i,
            cells=(
                TableCell(cell_id=f"c_{i}_0", row_index=i, col_index=0, coordinate="A", raw_value=str(2020 + (i % 5)), normalized_value=2020 + (i % 5), inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id=f"c_{i}_1", row_index=i, col_index=1, coordinate="B", raw_value=f"Station_{i}", normalized_value=f"Station_{i}", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id=f"c_{i}_2", row_index=i, col_index=2, coordinate="C", raw_value="10.0", normalized_value=Decimal("10.0"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id=f"c_{i}_3", row_index=i, col_index=3, coordinate="D", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ))

    # Row 240 (2024) and 241 (2025)
    rows.append(TableRow(
        row_id="r_240",
        row_index=240,
        cells=(
            TableCell(cell_id="c_240_0", row_index=240, col_index=0, coordinate="A", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
            TableCell(cell_id="c_240_1", row_index=240, col_index=1, coordinate="B", raw_value="StationDeep", normalized_value="StationDeep", inferred_dtype=InferredDtype.STRING),
            TableCell(cell_id="c_240_2", row_index=240, col_index=2, coordinate="C", raw_value="500.0", normalized_value=Decimal("500.0"), inferred_dtype=InferredDtype.DECIMAL),
            TableCell(cell_id="c_240_3", row_index=240, col_index=3, coordinate="D", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
        ),
        is_header=False,
    ))
    rows.append(TableRow(
        row_id="r_241",
        row_index=241,
        cells=(
            TableCell(cell_id="c_241_0", row_index=241, col_index=0, coordinate="A", raw_value="2025", normalized_value=2025, inferred_dtype=InferredDtype.INTEGER),
            TableCell(cell_id="c_241_1", row_index=241, col_index=1, coordinate="B", raw_value="StationDeep", normalized_value="StationDeep", inferred_dtype=InferredDtype.STRING),
            TableCell(cell_id="c_241_2", row_index=241, col_index=2, coordinate="C", raw_value="450.0", normalized_value=Decimal("450.0"), inferred_dtype=InferredDtype.DECIMAL),
            TableCell(cell_id="c_241_3", row_index=241, col_index=3, coordinate="D", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
        ),
        is_header=False,
    ))

    store.write_row_batch_with_checkpoint(
        table_id, ws, doc_id, ver, rows, 1, len(rows), len(rows), len(rows), 0, "1.0", "s1", "h1"
    )
    store.publish_active_version(
        table_id, ws, doc_id, ver,
        {"coverage_complete": True, "processing_finished": True, "source_rows": len(rows), "persisted_rows": len(rows), "rejected_rows": 0}
    )

    svc = StructuredQueryService(store)
    res = svc.execute(
        query="For StationDeep, how did ReadingValue change from 2024 to 2025?",
        workspace_id=ws,
    )
    assert res is not None
    assert res.result_value == Decimal("50.0")
    assert "fell" in res.answer_text


def test_leading_zero_identifiers_preserved():
    """Identifiers with leading zeros (e.g. '00123') are preserved as exact strings without numeric truncation."""
    store = MemoryTableStore()
    schema = _build_unfamiliar_schema()
    ws = "ws_edge"
    doc_id = "doc_zeros"
    ver = "v_zeros"
    table_id = "zeros_table"

    store.store_schema(table_id, ws, schema, doc_id, ver)

    rows = [
        TableRow(
            row_id="rz1",
            row_index=1,
            cells=(
                TableCell(cell_id="c1_0", row_index=1, col_index=0, coordinate="A1", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c1_1", row_index=1, col_index=1, coordinate="B1", raw_value="00123", normalized_value="00123", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c1_2", row_index=1, col_index=2, coordinate="C1", raw_value="100", normalized_value=Decimal("100"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="c1_3", row_index=1, col_index=3, coordinate="D1", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
        TableRow(
            row_id="rz2",
            row_index=2,
            cells=(
                TableCell(cell_id="c2_0", row_index=2, col_index=0, coordinate="A2", raw_value="2025", normalized_value=2025, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c2_1", row_index=2, col_index=1, coordinate="B2", raw_value="00123", normalized_value="00123", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c2_2", row_index=2, col_index=2, coordinate="C2", raw_value="200", normalized_value=Decimal("200"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="c2_3", row_index=2, col_index=3, coordinate="D2", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
    ]

    store.write_row_batch_with_checkpoint(table_id, ws, doc_id, ver, rows, 1, 2, 2, 2, 0, "1.0", "s1", "h1")
    store.publish_active_version(
        table_id, ws, doc_id, ver,
        {"coverage_complete": True, "processing_finished": True, "source_rows": 2, "persisted_rows": 2, "rejected_rows": 0}
    )

    svc = StructuredQueryService(store)
    res = svc.execute(
        query="For StationCode (00123), how did ReadingValue change from 2024 to 2025?",
        workspace_id=ws,
    )
    assert res is not None
    assert "00123" in res.predicate_values
    assert res.result_value == Decimal("100")


def test_null_cells_handled_gracefully():
    """Null and empty cell values in numeric columns are skipped without exceptions."""
    store = MemoryTableStore()
    schema = _build_unfamiliar_schema()
    ws = "ws_edge"
    doc_id = "doc_nulls"
    ver = "v_nulls"
    table_id = "nulls_table"

    store.store_schema(table_id, ws, schema, doc_id, ver)

    rows = [
        TableRow(
            row_id="rn1",
            row_index=1,
            cells=(
                TableCell(cell_id="c1_0", row_index=1, col_index=0, coordinate="A1", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c1_1", row_index=1, col_index=1, coordinate="B1", raw_value="StationNull", normalized_value="StationNull", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c1_2", row_index=1, col_index=2, coordinate="C1", raw_value="", normalized_value=None, inferred_dtype=InferredDtype.DECIMAL),  # NULL cell
                TableCell(cell_id="c1_3", row_index=1, col_index=3, coordinate="D1", raw_value="Missing", normalized_value="Missing", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
        TableRow(
            row_id="rn2",
            row_index=2,
            cells=(
                TableCell(cell_id="c2_0", row_index=2, col_index=0, coordinate="A2", raw_value="2025", normalized_value=2025, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c2_1", row_index=2, col_index=1, coordinate="B2", raw_value="StationNull", normalized_value="StationNull", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c2_2", row_index=2, col_index=2, coordinate="C2", raw_value="50", normalized_value=Decimal("50"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="c2_3", row_index=2, col_index=3, coordinate="D2", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
    ]

    store.write_row_batch_with_checkpoint(table_id, ws, doc_id, ver, rows, 1, 2, 2, 2, 0, "1.0", "s1", "h1")
    store.publish_active_version(
        table_id, ws, doc_id, ver,
        {"coverage_complete": True, "processing_finished": True, "source_rows": 2, "persisted_rows": 2, "rejected_rows": 0}
    )

    svc = StructuredQueryService(store)
    # Range min/max query handles nulls without failing
    res = svc.execute(
        query="What is the min and max ReadingValue in nulls_table?",
        workspace_id=ws,
    )
    assert res is not None
    assert res.result_value == Decimal("50")


def test_duplicate_period_matches_raise_ambiguous_binding():
    """Multiple rows matching the same year/period raise AmbiguousBindingError."""
    store = MemoryTableStore()
    schema = _build_unfamiliar_schema()
    ws = "ws_edge"
    doc_id = "doc_dupes"
    ver = "v_dupes"
    table_id = "dupes_table"

    store.store_schema(table_id, ws, schema, doc_id, ver)

    # 2 rows for 2024 for the same station StationDupe
    rows = [
        TableRow(
            row_id="rd1",
            row_index=1,
            cells=(
                TableCell(cell_id="c1_0", row_index=1, col_index=0, coordinate="A1", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c1_1", row_index=1, col_index=1, coordinate="B1", raw_value="StationDupe", normalized_value="StationDupe", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c1_2", row_index=1, col_index=2, coordinate="C1", raw_value="100", normalized_value=Decimal("100"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="c1_3", row_index=1, col_index=3, coordinate="D1", raw_value="Part1", normalized_value="Part1", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
        TableRow(
            row_id="rd2",
            row_index=2,
            cells=(
                TableCell(cell_id="c2_0", row_index=2, col_index=0, coordinate="A2", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c2_1", row_index=2, col_index=1, coordinate="B2", raw_value="StationDupe", normalized_value="StationDupe", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c2_2", row_index=2, col_index=2, coordinate="C2", raw_value="150", normalized_value=Decimal("150"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="c2_3", row_index=2, col_index=3, coordinate="D2", raw_value="Part2", normalized_value="Part2", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
        TableRow(
            row_id="rd3",
            row_index=3,
            cells=(
                TableCell(cell_id="c3_0", row_index=3, col_index=0, coordinate="A3", raw_value="2025", normalized_value=2025, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c3_1", row_index=3, col_index=1, coordinate="B3", raw_value="StationDupe", normalized_value="StationDupe", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c3_2", row_index=3, col_index=2, coordinate="C3", raw_value="200", normalized_value=Decimal("200"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="c3_3", row_index=3, col_index=3, coordinate="D3", raw_value="Part1", normalized_value="Part1", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
    ]

    store.write_row_batch_with_checkpoint(table_id, ws, doc_id, ver, rows, 1, 3, 3, 3, 0, "1.0", "s1", "h1")
    store.publish_active_version(
        table_id, ws, doc_id, ver,
        {"coverage_complete": True, "processing_finished": True, "source_rows": 3, "persisted_rows": 3, "rejected_rows": 0}
    )

    svc = StructuredQueryService(store)
    with pytest.raises(AmbiguousBindingError) as exc_info:
        svc.execute(
            query="For StationDupe, how did ReadingValue change from 2024 to 2025?",
            workspace_id=ws,
        )
    assert "Duplicate matches found" in str(exc_info.value)


def test_zero_denominator_handles_gracefully():
    """Initial value of 0 in percentage delta returns secondary_value=None (null) and explicit undefined explanation.
    Covers 0->positive, 0->negative, and 0->0.
    """
    store = MemoryTableStore()
    schema = _build_unfamiliar_schema()
    ws = "ws_edge"

    # Case A: 0 -> positive (0 to 50)
    ws_a = "ws_zero_a"
    doc_a = "doc_zero_pos"
    ver_a = "v_zero_pos"
    tbl_a = "tbl_zero_pos"
    store.store_schema(tbl_a, ws_a, schema, doc_a, ver_a)
    rows_a = [
        TableRow(
            row_id="ra1", row_index=1,
            cells=(
                TableCell(cell_id="ca1_0", row_index=1, col_index=0, coordinate="A1", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="ca1_1", row_index=1, col_index=1, coordinate="B1", raw_value="StationZero", normalized_value="StationZero", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="ca1_2", row_index=1, col_index=2, coordinate="C1", raw_value="0", normalized_value=Decimal("0"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="ca1_3", row_index=1, col_index=3, coordinate="D1", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
        TableRow(
            row_id="ra2", row_index=2,
            cells=(
                TableCell(cell_id="ca2_0", row_index=2, col_index=0, coordinate="A2", raw_value="2025", normalized_value=2025, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="ca2_1", row_index=2, col_index=1, coordinate="B2", raw_value="StationZero", normalized_value="StationZero", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="ca2_2", row_index=2, col_index=2, coordinate="C2", raw_value="50", normalized_value=Decimal("50"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="ca2_3", row_index=2, col_index=3, coordinate="D2", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
    ]
    store.write_row_batch_with_checkpoint(tbl_a, ws_a, doc_a, ver_a, rows_a, 1, 2, 2, 2, 0, "1.0", "s1", "h1")
    store.publish_active_version(tbl_a, ws_a, doc_a, ver_a, {"coverage_complete": True, "processing_finished": True, "source_rows": 2, "persisted_rows": 2, "rejected_rows": 0})

    svc = StructuredQueryService(store)
    res_pos = svc.execute(query="For StationZero, how did ReadingValue change from 2024 to 2025?", workspace_id=ws_a)
    assert res_pos.result_value == Decimal("50")  # Absolute delta
    assert res_pos.secondary_value is None        # percentage_change = null
    assert "increased by 50" in res_pos.answer_text
    assert "Percentage change is undefined because the initial baseline value is 0." in res_pos.answer_text

    # Case B: 0 -> negative (0 to -25)
    ws_b = "ws_zero_b"
    doc_b = "doc_zero_neg"
    ver_b = "v_zero_neg"
    tbl_b = "tbl_zero_neg"
    store.store_schema(tbl_b, ws_b, schema, doc_b, ver_b)
    rows_b = [
        TableRow(
            row_id="rb1", row_index=1,
            cells=(
                TableCell(cell_id="cb1_0", row_index=1, col_index=0, coordinate="A1", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="cb1_1", row_index=1, col_index=1, coordinate="B1", raw_value="StationNeg", normalized_value="StationNeg", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="cb1_2", row_index=1, col_index=2, coordinate="C1", raw_value="0", normalized_value=Decimal("0"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="cb1_3", row_index=1, col_index=3, coordinate="D1", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
        TableRow(
            row_id="rb2", row_index=2,
            cells=(
                TableCell(cell_id="cb2_0", row_index=2, col_index=0, coordinate="A2", raw_value="2025", normalized_value=2025, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="cb2_1", row_index=2, col_index=1, coordinate="B2", raw_value="StationNeg", normalized_value="StationNeg", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="cb2_2", row_index=2, col_index=2, coordinate="C2", raw_value="-25", normalized_value=Decimal("-25"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="cb2_3", row_index=2, col_index=3, coordinate="D2", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
    ]
    store.write_row_batch_with_checkpoint(tbl_b, ws_b, doc_b, ver_b, rows_b, 1, 2, 2, 2, 0, "1.0", "s1", "h1")
    store.publish_active_version(tbl_b, ws_b, doc_b, ver_b, {"coverage_complete": True, "processing_finished": True, "source_rows": 2, "persisted_rows": 2, "rejected_rows": 0})

    res_neg = svc.execute(query="For StationNeg, how did ReadingValue change from 2024 to 2025?", workspace_id=ws_b)
    assert res_neg.result_value == Decimal("25")  # Absolute delta = 25
    assert res_neg.secondary_value is None        # percentage_change = null
    assert "fell by 25" in res_neg.answer_text
    assert "Percentage change is undefined because the initial baseline value is 0." in res_neg.answer_text

    # Case C: 0 -> 0 (0 to 0)
    ws_c = "ws_zero_c"
    doc_c = "doc_zero_zero"
    ver_c = "v_zero_zero"
    tbl_c = "tbl_zero_zero"
    store.store_schema(tbl_c, ws_c, schema, doc_c, ver_c)
    rows_c = [
        TableRow(
            row_id="rc1", row_index=1,
            cells=(
                TableCell(cell_id="cc1_0", row_index=1, col_index=0, coordinate="A1", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="cc1_1", row_index=1, col_index=1, coordinate="B1", raw_value="StationFlat", normalized_value="StationFlat", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="cc1_2", row_index=1, col_index=2, coordinate="C1", raw_value="0", normalized_value=Decimal("0"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="cc1_3", row_index=1, col_index=3, coordinate="D1", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
        TableRow(
            row_id="rc2", row_index=2,
            cells=(
                TableCell(cell_id="cc2_0", row_index=2, col_index=0, coordinate="A2", raw_value="2025", normalized_value=2025, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="cc2_1", row_index=2, col_index=1, coordinate="B2", raw_value="StationFlat", normalized_value="StationFlat", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="cc2_2", row_index=2, col_index=2, coordinate="C2", raw_value="0", normalized_value=Decimal("0"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="cc2_3", row_index=2, col_index=3, coordinate="D2", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
    ]
    store.write_row_batch_with_checkpoint(tbl_c, ws_c, doc_c, ver_c, rows_c, 1, 2, 2, 2, 0, "1.0", "s1", "h1")
    store.publish_active_version(tbl_c, ws_c, doc_c, ver_c, {"coverage_complete": True, "processing_finished": True, "source_rows": 2, "persisted_rows": 2, "rejected_rows": 0})

    res_zero = svc.execute(query="For StationFlat, how did ReadingValue change from 2024 to 2025?", workspace_id=ws_c)
    assert res_zero.result_value == Decimal("0")   # Absolute delta = 0
    assert res_zero.secondary_value is None        # percentage_change = null
    assert "remained unchanged at 0" in res_zero.answer_text
    assert "Percentage change is undefined because the initial baseline value is 0." in res_zero.answer_text


def test_unresolved_required_filter_fails_closed():
    """A required filter entity not found in table fails closed with UnsupportedQueryError."""
    store = MemoryTableStore()
    schema = _build_unfamiliar_schema()
    ws = "ws_edge"
    doc_id = "doc_unresolved"
    ver = "v_unresolved"
    table_id = "unresolved_table"

    store.store_schema(table_id, ws, schema, doc_id, ver)

    rows = [
        TableRow(
            row_id="ru1",
            row_index=1,
            cells=(
                TableCell(cell_id="c1_0", row_index=1, col_index=0, coordinate="A1", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c1_1", row_index=1, col_index=1, coordinate="B1", raw_value="StationExisting", normalized_value="StationExisting", inferred_dtype=InferredDtype.STRING),
                TableCell(cell_id="c1_2", row_index=1, col_index=2, coordinate="C1", raw_value="100", normalized_value=Decimal("100"), inferred_dtype=InferredDtype.DECIMAL),
                TableCell(cell_id="c1_3", row_index=1, col_index=3, coordinate="D1", raw_value="OK", normalized_value="OK", inferred_dtype=InferredDtype.STRING),
            ),
            is_header=False,
        ),
    ]

    store.write_row_batch_with_checkpoint(table_id, ws, doc_id, ver, rows, 1, 1, 1, 1, 0, "1.0", "s1", "h1")
    store.publish_active_version(
        table_id, ws, doc_id, ver,
        {"coverage_complete": True, "processing_finished": True, "source_rows": 1, "persisted_rows": 1, "rejected_rows": 0}
    )

    svc = StructuredQueryService(store)
    with pytest.raises((UnsupportedQueryError, UnresolvedFilterError)) as exc_info:
        svc.execute(
            query="For NonExistentStation999, how did ReadingValue change from 2024 to 2025?",
            workspace_id=ws,
        )
    assert "not found in table" in str(exc_info.value) or "Matching entity rows not found" in str(exc_info.value)


def test_narrative_queries_with_tabular_tokens_do_not_route_to_structured_table():
    """Narrative questions containing 'year', 'table', or 'period' route to vector_rerank or page_index."""
    router = StrategyRouter()

    narrative_queries = [
        "In what year was the company founded according to the background history section?",
        "The narrative discusses the transition period following the acquisition.",
        "Please describe the overview table presented in the executive summary.",
        "What was the total impact on employee morale during the reorganization period?",
        "Explain how the policy evolved over the three year period.",
    ]

    for q in narrative_queries:
        res = router.route(q)
        assert "structured_table" not in res.primary, (
            f"Query '{q}' incorrectly routed to structured_table: {res.primary}"
        )
        assert any(s in res.primary for s in ("vector_rerank", "page_index", "bm25"))
