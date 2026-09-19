"""SQLite implementation of TableStore for local development and persistence."""

import json
import logging
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from db.table_store.base import AggregateOp, Predicate, PredicateOp, TableStore
from schemas.structured_table import (
    ColumnDefinition,
    HeaderTopology,
    InferredDtype,
    MergedRange,
    StructuredTable,
    TableCell,
    TableRow,
    TableSchema,
)

logger = logging.getLogger(__name__)


class SQLiteTableStore:
    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tables (
                    workspace_id TEXT NOT NULL,
                    table_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    sheet_name TEXT,
                    page_number INTEGER,
                    schema_json TEXT NOT NULL,
                    merged_ranges_json TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    col_count INTEGER NOT NULL,
                    PRIMARY KEY (workspace_id, table_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS table_rows (
                    workspace_id TEXT NOT NULL,
                    table_id TEXT NOT NULL,
                    row_id TEXT NOT NULL,
                    row_index INTEGER NOT NULL,
                    is_header INTEGER NOT NULL,
                    cells_json TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, table_id, row_id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rows_lookup ON table_rows(workspace_id, table_id, row_index)"
            )

    def store_table(self, table: StructuredTable, workspace_id: str) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty for table storage")

        schema_dict = {
            "columns": [
                {
                    "col_index": c.col_index,
                    "name": c.name,
                    "inferred_dtype": c.inferred_dtype.value,
                    "unit": c.unit,
                }
                for c in table.schema.columns
            ],
            "header_rows": list(table.schema.header_rows),
            "header_topology": table.schema.header_topology.value,
            "confidence": table.schema.confidence,
        }
        merged_dict = [
            {
                "range_str": m.range_str,
                "start_row": m.start_row,
                "start_col": m.start_col,
                "end_row": m.end_row,
                "end_col": m.end_col,
                "anchor_cell": m.anchor_cell,
                "value": m.value,
            }
            for m in table.merged_ranges
        ]

        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO tables 
                (workspace_id, table_id, document_id, sheet_name, page_number, schema_json, merged_ranges_json, row_count, col_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    table.table_id,
                    table.document_id,
                    table.sheet_name,
                    table.page_number,
                    json.dumps(schema_dict),
                    json.dumps(merged_dict),
                    len(table.rows),
                    table.col_count,
                ),
            )
            # Insert rows
            for row in table.rows:
                cells_data = [
                    {
                        "cell_id": c.cell_id,
                        "row_index": c.row_index,
                        "col_index": c.col_index,
                        "coordinate": c.coordinate,
                        "raw_value": str(c.raw_value) if c.raw_value is not None else None,
                        "normalized_value": str(c.normalized_value) if c.normalized_value is not None else None,
                        "cached_value": str(c.cached_value) if c.cached_value is not None else None,
                        "raw_formula": c.raw_formula,
                        "inferred_dtype": c.inferred_dtype.value,
                        "unit": c.unit,
                        "is_merged": c.is_merged,
                    }
                    for c in row.cells
                ]
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO table_rows
                    (workspace_id, table_id, row_id, row_index, is_header, cells_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        table.table_id,
                        row.row_id,
                        row.row_index,
                        1 if row.is_header else 0,
                        json.dumps(cells_data),
                    ),
                )

    def get_table(self, table_id: str, workspace_id: str) -> StructuredTable | None:
        if not workspace_id:
            return None
        cur = self._conn.execute(
            "SELECT * FROM tables WHERE workspace_id = ? AND table_id = ?",
            (workspace_id, table_id),
        )
        row = cur.fetchone()
        if not row:
            return None

        schema_raw = json.loads(row["schema_json"])
        columns = tuple(
            ColumnDefinition(
                col_index=c["col_index"],
                name=c["name"],
                inferred_dtype=InferredDtype(c["inferred_dtype"]),
                unit=c.get("unit"),
            )
            for c in schema_raw["columns"]
        )
        schema = TableSchema(
            columns=columns,
            header_rows=tuple(schema_raw["header_rows"]),
            header_topology=HeaderTopology(schema_raw["header_topology"]),
            confidence=schema_raw["confidence"],
        )

        merged_raw = json.loads(row["merged_ranges_json"])
        merged_ranges = tuple(
            MergedRange(
                range_str=m["range_str"],
                start_row=m["start_row"],
                start_col=m["start_col"],
                end_row=m["end_row"],
                end_col=m["end_col"],
                anchor_cell=m["anchor_cell"],
                value=m.get("value", ""),
            )
            for m in merged_raw
        )

        rows = self.query_rows(table_id, workspace_id)
        return StructuredTable(
            table_id=table_id,
            document_id=row["document_id"],
            sheet_name=row["sheet_name"],
            page_number=row["page_number"],
            schema=schema,
            rows=tuple(rows),
            merged_ranges=merged_ranges,
            row_count=row["row_count"],
            col_count=row["col_count"],
        )

    def _row_from_db(self, db_row: sqlite3.Row) -> TableRow:
        cells_data = json.loads(db_row["cells_json"])
        cells = []
        for c in cells_data:
            norm_val = c["normalized_value"]
            # Convert to Decimal if numeric
            if norm_val is not None:
                try:
                    norm_val = Decimal(norm_val)
                except Exception:
                    pass
            cells.append(
                TableCell(
                    cell_id=c["cell_id"],
                    row_index=c["row_index"],
                    col_index=c["col_index"],
                    coordinate=c["coordinate"],
                    raw_value=c["raw_value"],
                    cached_value=c.get("cached_value"),
                    raw_formula=c.get("raw_formula"),
                    normalized_value=norm_val,
                    inferred_dtype=InferredDtype(c.get("inferred_dtype", "string")),
                    unit=c.get("unit"),
                    is_merged=c.get("is_merged", False),
                )
            )
        return TableRow(
            row_id=db_row["row_id"],
            row_index=db_row["row_index"],
            cells=tuple(cells),
            is_header=bool(db_row["is_header"]),
        )

    def query_rows(
        self,
        table_id: str,
        workspace_id: str,
        predicates: list[Predicate] | None = None,
        limit: int | None = None,
    ) -> list[TableRow]:
        if not workspace_id:
            return []

        cur = self._conn.execute(
            "SELECT * FROM table_rows WHERE workspace_id = ? AND table_id = ? ORDER BY row_index ASC",
            (workspace_id, table_id),
        )
        matched_rows: list[TableRow] = []
        for db_row in cur:
            row = self._row_from_db(db_row)
            if row.is_header:
                continue

            if predicates:
                match = True
                for pred in predicates:
                    if pred.column_index < len(row.cells):
                        cell = row.cells[pred.column_index]
                        cell_val = cell.normalized_value if cell.normalized_value is not None else cell.raw_value
                        target_val = pred.value
                        if pred.op == PredicateOp.EQ and str(cell_val).lower() != str(target_val).lower():
                            match = False
                            break
                        elif pred.op == PredicateOp.CONTAINS and str(target_val).lower() not in str(cell_val).lower():
                            match = False
                            break
                    else:
                        match = False
                        break
                if match:
                    matched_rows.append(row)
            else:
                matched_rows.append(row)

            if limit is not None and len(matched_rows) >= limit:
                break

        return matched_rows

    def aggregate(
        self,
        table_id: str,
        workspace_id: str,
        column_index: int,
        op: AggregateOp,
        predicates: list[Predicate] | None = None,
    ) -> Decimal | None:
        rows = self.query_rows(table_id, workspace_id, predicates=predicates)
        if not rows:
            return Decimal("0") if op == AggregateOp.COUNT else None

        if op == AggregateOp.COUNT:
            return Decimal(len(rows))

        values: list[Decimal] = []
        for r in rows:
            if column_index < len(r.cells):
                val = r.cells[column_index].normalized_value
                if val is not None:
                    if isinstance(val, Decimal):
                        values.append(val)
                    else:
                        try:
                            clean_str = str(val).replace(",", "").strip()
                            values.append(Decimal(clean_str))
                        except Exception:
                            pass

        if not values:
            return None
        if op == AggregateOp.SUM:
            return sum(values)
        if op == AggregateOp.AVG:
            return sum(values) / Decimal(len(values))
        if op == AggregateOp.MIN:
            return min(values)
        if op == AggregateOp.MAX:
            return max(values)
        return None

    def delete_table(self, table_id: str, workspace_id: str) -> bool:
        if not workspace_id:
            return False
        with self._conn:
            self._conn.execute(
                "DELETE FROM tables WHERE workspace_id = ? AND table_id = ?",
                (workspace_id, table_id),
            )
            self._conn.execute(
                "DELETE FROM table_rows WHERE workspace_id = ? AND table_id = ?",
                (workspace_id, table_id),
            )
        return True
