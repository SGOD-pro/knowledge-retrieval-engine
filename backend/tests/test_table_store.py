from decimal import Decimal

import pytest

from db.table_store import (
    AggregateOp,
    MemoryTableStore,
    Predicate,
    PredicateOp,
    SQLiteTableStore,
    TableStore,
)
from schemas.structured_table import (
    ColumnDefinition,
    HeaderTopology,
    InferredDtype,
    StructuredTable,
    TableCell,
    TableRow,
    TableSchema,
)


def _build_test_table(table_id: str = "tab_fin") -> StructuredTable:
    cols = (
        ColumnDefinition(col_index=0, name="Entity", inferred_dtype=InferredDtype.STRING),
        ColumnDefinition(col_index=1, name="Year", inferred_dtype=InferredDtype.INTEGER),
        ColumnDefinition(col_index=2, name="Revenue", inferred_dtype=InferredDtype.DECIMAL, unit="INR Cr"),
    )
    schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=0.98)

    rows = (
        TableRow(
            row_id="r1",
            row_index=1,
            cells=(
                TableCell("c1_0", 1, 0, "A2", "Acme", normalized_value="Acme"),
                TableCell("c1_1", 1, 1, "B2", 2021, normalized_value=2021),
                TableCell("c1_2", 1, 2, "C2", "500.5", normalized_value=Decimal("500.5")),
            ),
        ),
        TableRow(
            row_id="r2",
            row_index=2,
            cells=(
                TableCell("c2_0", 2, 0, "A3", "Acme", normalized_value="Acme"),
                TableCell("c2_1", 2, 1, "B3", 2022, normalized_value=2022),
                TableCell("c2_2", 2, 2, "C3", "620.0", normalized_value=Decimal("620.0")),
            ),
        ),
        TableRow(
            row_id="r3",
            row_index=3,
            cells=(
                TableCell("c3_0", 3, 0, "A4", "Beta", normalized_value="Beta"),
                TableCell("c3_1", 3, 1, "B4", 2022, normalized_value=2022),
                TableCell("c3_2", 3, 2, "C4", "150.25", normalized_value=Decimal("150.25")),
            ),
        ),
    )

    return StructuredTable(
        table_id=table_id,
        document_id="doc_1",
        sheet_name="Financials",
        page_number=None,
        schema=schema,
        rows=rows,
        row_count=3,
        col_count=3,
    )


@pytest.mark.parametrize("store_cls", [MemoryTableStore, lambda: SQLiteTableStore(":memory:")])
def test_tablestore_basic_and_predicates(store_cls):
    store: TableStore = store_cls()
    table = _build_test_table()
    ws_id = "ws_test_1"

    # Store table
    store.store_table(table, workspace_id=ws_id)

    # Retrieve table
    retrieved = store.get_table("tab_fin", workspace_id=ws_id)
    assert retrieved is not None
    assert retrieved.table_id == "tab_fin"
    assert len(retrieved.schema.columns) == 3

    # Query all rows
    all_rows = store.query_rows("tab_fin", workspace_id=ws_id)
    assert len(all_rows) == 3

    # Predicate: Entity == "Acme"
    acme_rows = store.query_rows(
        "tab_fin",
        workspace_id=ws_id,
        predicates=[Predicate(column_index=0, op=PredicateOp.EQ, value="Acme")],
    )
    assert len(acme_rows) == 2
    assert all(r.cells[0].normalized_value == "Acme" for r in acme_rows)

    # Predicate: Year == 2022
    y2022_rows = store.query_rows(
        "tab_fin",
        workspace_id=ws_id,
        predicates=[Predicate(column_index=1, op=PredicateOp.EQ, value=2022)],
    )
    assert len(y2022_rows) == 2


@pytest.mark.parametrize("store_cls", [MemoryTableStore, lambda: SQLiteTableStore(":memory:")])
def test_tablestore_exact_aggregations(store_cls):
    store: TableStore = store_cls()
    table = _build_test_table()
    ws_id = "ws_test_2"
    store.store_table(table, workspace_id=ws_id)

    # SUM over Revenue (col index 2)
    # 500.5 + 620.0 + 150.25 = 1270.75
    total_rev = store.aggregate("tab_fin", workspace_id=ws_id, column_index=2, op=AggregateOp.SUM)
    assert total_rev == Decimal("1270.75")

    # COUNT
    count = store.aggregate("tab_fin", workspace_id=ws_id, column_index=2, op=AggregateOp.COUNT)
    assert count == Decimal("3")

    # MIN and MAX
    min_rev = store.aggregate("tab_fin", workspace_id=ws_id, column_index=2, op=AggregateOp.MIN)
    max_rev = store.aggregate("tab_fin", workspace_id=ws_id, column_index=2, op=AggregateOp.MAX)
    assert min_rev == Decimal("150.25")
    assert max_rev == Decimal("620.0")

    # Filtered aggregation: SUM of Revenue for Acme only
    acme_sum = store.aggregate(
        "tab_fin",
        workspace_id=ws_id,
        column_index=2,
        op=AggregateOp.SUM,
        predicates=[Predicate(column_index=0, op=PredicateOp.EQ, value="Acme")],
    )
    # 500.5 + 620.0 = 1120.5
    assert acme_sum == Decimal("1120.5")


@pytest.mark.parametrize("store_cls", [MemoryTableStore, lambda: SQLiteTableStore(":memory:")])
def test_tablestore_workspace_isolation(store_cls):
    store: TableStore = store_cls()
    table = _build_test_table()
    store.store_table(table, workspace_id="workspace_alpha")

    # Workspace Beta should not see Alpha's table
    beta_table = store.get_table("tab_fin", workspace_id="workspace_beta")
    assert beta_table is None

    beta_rows = store.query_rows("tab_fin", workspace_id="workspace_beta")
    assert beta_rows == []

    beta_agg = store.aggregate("tab_fin", workspace_id="workspace_beta", column_index=2, op=AggregateOp.SUM)
    assert beta_agg is None
