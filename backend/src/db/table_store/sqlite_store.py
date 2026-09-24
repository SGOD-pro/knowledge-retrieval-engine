"""SQLite implementation of TableStore for local development, testing, and persistence."""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
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


def _schema_to_json(schema: TableSchema) -> str:
    cols = []
    for c in schema.columns:
        cols.append({
            "col_index": c.col_index,
            "name": c.name,
            "path_hierarchy": list(c.path_hierarchy),
            "inferred_dtype": c.inferred_dtype.value,
            "unit": c.unit,
            "sample_values": list(c.sample_values),
            "null_ratio": c.null_ratio,
        })
    data = {
        "columns": cols,
        "header_rows": list(schema.header_rows),
        "header_topology": schema.header_topology.value,
        "confidence": schema.confidence,
    }
    return json.dumps(data)


def _json_to_schema(s: str) -> TableSchema:
    data = json.loads(s)
    cols = []
    for c in data.get("columns", []):
        cols.append(
            ColumnDefinition(
                col_index=c["col_index"],
                name=c["name"],
                path_hierarchy=tuple(c.get("path_hierarchy", ())),
                inferred_dtype=InferredDtype(c.get("inferred_dtype", "string")),
                unit=c.get("unit"),
                sample_values=tuple(c.get("sample_values", ())),
                null_ratio=c.get("null_ratio", 0.0),
            )
        )
    return TableSchema(
        columns=tuple(cols),
        header_rows=tuple(data.get("header_rows", (0,))),
        header_topology=HeaderTopology(data.get("header_topology", "single_row")),
        confidence=data.get("confidence", 1.0),
    )


def _cells_to_json(cells: tuple[TableCell, ...] | list[TableCell]) -> str:
    raw_list = []
    for c in cells:
        val = c.normalized_value
        if isinstance(val, Decimal):
            norm_val = str(val)
        else:
            norm_val = val
        raw_list.append({
            "cell_id": c.cell_id,
            "row_index": c.row_index,
            "col_index": c.col_index,
            "coordinate": c.coordinate,
            "raw_value": str(c.raw_value) if c.raw_value is not None else None,
            "normalized_value": norm_val,
            "inferred_dtype": c.inferred_dtype.value if hasattr(c.inferred_dtype, "value") else str(c.inferred_dtype),
            "unit": c.unit,
            "cached_value": c.cached_value,
            "raw_formula": c.raw_formula,
            "is_merged": c.is_merged,
        })
    return json.dumps(raw_list)


def _json_to_cells(s: str) -> tuple[TableCell, ...]:
    raw_list = json.loads(s)
    cells = []
    for d in raw_list:
        norm = d.get("normalized_value")
        if norm is not None and d.get("inferred_dtype") in ("decimal", "integer", "currency", "percentage"):
            try:
                norm = Decimal(str(norm))
            except Exception:
                pass
        cells.append(
            TableCell(
                cell_id=d.get("cell_id", ""),
                row_index=d.get("row_index", 0),
                col_index=d.get("col_index", 0),
                coordinate=d.get("coordinate", ""),
                raw_value=d.get("raw_value"),
                cached_value=d.get("cached_value"),
                raw_formula=d.get("raw_formula"),
                normalized_value=norm,
                inferred_dtype=InferredDtype(d.get("inferred_dtype", "string")),
                unit=d.get("unit"),
                is_merged=d.get("is_merged", False),
            )
        )
    return tuple(cells)


class SQLiteTableStore:
    """SQLite TableStore supporting both legacy tables and versioned tables with checkpoints."""

    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            # Legacy tables
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

            # Versioned storage tables
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS versioned_schemas (
                    workspace_id TEXT NOT NULL,
                    table_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    schema_json TEXT NOT NULL,
                    col_count INTEGER NOT NULL,
                    ingested_at_utc TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, table_id, version)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS versioned_rows (
                    workspace_id TEXT NOT NULL,
                    table_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    row_id TEXT NOT NULL,
                    row_index INTEGER NOT NULL,
                    is_header INTEGER NOT NULL,
                    is_subtotal INTEGER NOT NULL DEFAULT 0,
                    is_empty INTEGER NOT NULL DEFAULT 0,
                    cells_json TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, table_id, version, row_id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ver_rows_lookup ON versioned_rows(workspace_id, table_id, version, row_index)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_versions (
                    workspace_id TEXT NOT NULL,
                    table_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    coverage_complete INTEGER NOT NULL,
                    processing_finished INTEGER NOT NULL,
                    source_rows INTEGER NOT NULL,
                    persisted_rows INTEGER NOT NULL,
                    rejected_rows INTEGER NOT NULL,
                    published_at_utc TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, table_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    workspace_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, document_id, version)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS manifests (
                    workspace_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    is_latest INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (workspace_id, document_id, version)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_catalogue (
                    workspace_id TEXT NOT NULL,
                    table_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    active_version TEXT NOT NULL,
                    coverage_complete INTEGER NOT NULL,
                    PRIMARY KEY (workspace_id, table_id)
                )
                """
            )

    # -------------------------------------------------------------------------
    # Versioned API
    # -------------------------------------------------------------------------

    def store_schema(
        self,
        table_id: str,
        workspace_id: str,
        schema: TableSchema,
        document_id: str,
        document_version: str,
        parser_version: str = "1.0.0",
        schema_version: str = "",
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")

        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO versioned_schemas
                (workspace_id, table_id, version, document_id, parser_version, schema_version, schema_json, col_count, ingested_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    table_id,
                    document_version,
                    document_id,
                    parser_version,
                    schema_version,
                    _schema_to_json(schema),
                    len(schema.columns),
                    now,
                ),
            )

    def write_row_batch_with_checkpoint(
        self,
        table_id: str,
        workspace_id: str,
        document_id: str,
        version: str,
        rows: list[TableRow],
        checkpoint_batch: int,
        source_rows: int,
        attempted_rows: int,
        persisted_rows: int,
        rejected_rows: int,
        parser_version: str,
        schema_version: str,
        content_hash: str,
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")

        now = datetime.now(timezone.utc).isoformat()
        cp_data = {
            "table_id": table_id,
            "document_id": document_id,
            "workspace_id": workspace_id,
            "version": version,
            "checkpoint_batch": checkpoint_batch,
            "source_rows": source_rows,
            "attempted_rows": attempted_rows,
            "persisted_rows": persisted_rows,
            "rejected_rows": rejected_rows,
            "parser_version": parser_version,
            "schema_version": schema_version,
            "content_hash": content_hash,
            "updated_at_utc": now,
        }

        with self._conn:
            row_params = [
                (
                    workspace_id,
                    table_id,
                    version,
                    r.row_id,
                    r.row_index,
                    1 if r.is_header else 0,
                    1 if r.is_subtotal else 0,
                    1 if r.is_empty else 0,
                    _cells_to_json(r.cells),
                )
                for r in rows
            ]
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO versioned_rows
                (workspace_id, table_id, version, row_id, row_index, is_header, is_subtotal, is_empty, cells_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row_params,
            )
            self._conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints
                (workspace_id, document_id, version, checkpoint_json)
                VALUES (?, ?, ?, ?)
                """,
                (workspace_id, document_id, version, json.dumps(cp_data)),
            )

    def publish_active_version(
        self,
        table_id: str,
        workspace_id: str,
        document_id: str,
        version: str,
        manifest: dict,
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")

        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO active_versions
                (workspace_id, table_id, version, document_id, coverage_complete, processing_finished, source_rows, persisted_rows, rejected_rows, published_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    table_id,
                    version,
                    document_id,
                    1 if manifest.get("coverage_complete") else 0,
                    1 if manifest.get("processing_finished") else 0,
                    int(manifest.get("source_rows", 0)),
                    int(manifest.get("persisted_rows", 0)),
                    int(manifest.get("rejected_rows", 0)),
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE manifests SET is_latest = 0 WHERE workspace_id = ? AND document_id = ?",
                (workspace_id, document_id),
            )
            self._conn.execute(
                """
                INSERT OR REPLACE INTO manifests
                (workspace_id, document_id, version, manifest_json, is_latest)
                VALUES (?, ?, ?, ?, 1)
                """,
                (workspace_id, document_id, version, json.dumps(manifest)),
            )
            self._conn.execute(
                """
                INSERT OR REPLACE INTO workspace_catalogue
                (workspace_id, table_id, document_id, active_version, coverage_complete)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    table_id,
                    document_id,
                    version,
                    1 if manifest.get("coverage_complete") else 0,
                ),
            )

    def get_active_version(self, table_id: str, workspace_id: str) -> str | None:
        if not workspace_id:
            return None
        cur = self._conn.execute(
            "SELECT version FROM active_versions WHERE workspace_id = ? AND table_id = ?",
            (workspace_id, table_id),
        )
        row = cur.fetchone()
        if row:
            return row["version"]

        # Check legacy tables
        cur_leg = self._conn.execute(
            "SELECT table_id FROM tables WHERE workspace_id = ? AND table_id = ?",
            (workspace_id, table_id),
        )
        if cur_leg.fetchone():
            return "legacy"
        return None

    def list_tables(self, workspace_id: str) -> list[str]:
        if not workspace_id:
            return []
        cur = self._conn.execute(
            "SELECT DISTINCT table_id FROM workspace_catalogue WHERE workspace_id = ? ORDER BY table_id ASC",
            (workspace_id,),
        )
        tables = [r["table_id"] for r in cur.fetchall()]
        if not tables:
            cur_leg = self._conn.execute(
                "SELECT DISTINCT table_id FROM tables WHERE workspace_id = ? ORDER BY table_id ASC",
                (workspace_id,),
            )
            tables = [r["table_id"] for r in cur_leg.fetchall()]
        return tables

    def get_ingestion_manifest(
        self,
        document_id: str,
        workspace_id: str,
        version: str | None = None,
    ) -> dict | None:
        if not workspace_id or not document_id:
            return None
        if version:
            cur = self._conn.execute(
                "SELECT manifest_json FROM manifests WHERE workspace_id = ? AND document_id = ? AND version = ?",
                (workspace_id, document_id, version),
            )
        else:
            cur = self._conn.execute(
                "SELECT manifest_json FROM manifests WHERE workspace_id = ? AND document_id = ? AND is_latest = 1",
                (workspace_id, document_id),
            )
        row = cur.fetchone()
        if row:
            return json.loads(row["manifest_json"])
        return None

    def get_checkpoint(
        self,
        document_id: str,
        workspace_id: str,
        version: str,
    ) -> dict | None:
        if not workspace_id or not document_id:
            return None
        cur = self._conn.execute(
            "SELECT checkpoint_json FROM checkpoints WHERE workspace_id = ? AND document_id = ? AND version = ?",
            (workspace_id, document_id, version),
        )
        row = cur.fetchone()
        if row:
            return json.loads(row["checkpoint_json"])
        return None

    def has_coverage_complete_table(self, table_id: str, workspace_id: str) -> bool:
        if not workspace_id:
            return False
        cur = self._conn.execute(
            "SELECT coverage_complete FROM active_versions WHERE workspace_id = ? AND table_id = ?",
            (workspace_id, table_id),
        )
        row = cur.fetchone()
        if row:
            return bool(row["coverage_complete"])
        return False

    def _matches_predicate(self, cells: tuple[TableCell, ...], pred: Predicate) -> bool:
        if pred.column_index < 0 or pred.column_index >= len(cells):
            return False
        cell = cells[pred.column_index]
        cell_val = cell.normalized_value if cell.normalized_value is not None else cell.raw_value
        target_val = pred.value

        # Normalize string comparison
        if isinstance(cell_val, str) and isinstance(target_val, str):
            c_str, t_str = cell_val.strip().lower(), target_val.strip().lower()
            if pred.op == PredicateOp.EQ:
                return c_str == t_str
            if pred.op == PredicateOp.NEQ:
                return c_str != t_str
            if pred.op == PredicateOp.CONTAINS:
                return t_str in c_str

        try:
            if pred.op in (
                PredicateOp.EQ,
                PredicateOp.NEQ,
                PredicateOp.GT,
                PredicateOp.LT,
                PredicateOp.GTE,
                PredicateOp.LTE,
            ):
                try:
                    num_cell = Decimal(str(cell_val).replace(",", "").strip())
                    num_target = Decimal(str(target_val).replace(",", "").strip())
                    if pred.op == PredicateOp.EQ:
                        return num_cell == num_target
                    if pred.op == PredicateOp.NEQ:
                        return num_cell != num_target
                    if pred.op == PredicateOp.GT:
                        return num_cell > num_target
                    if pred.op == PredicateOp.LT:
                        return num_cell < num_target
                    if pred.op == PredicateOp.GTE:
                        return num_cell >= num_target
                    if pred.op == PredicateOp.LTE:
                        return num_cell <= num_target
                except Exception:
                    pass

            if pred.op == PredicateOp.EQ:
                return cell_val == target_val
            if pred.op == PredicateOp.NEQ:
                return cell_val != target_val
            if pred.op == PredicateOp.GT:
                return cell_val > target_val
            if pred.op == PredicateOp.LT:
                return cell_val < target_val
            if pred.op == PredicateOp.GTE:
                return cell_val >= target_val
            if pred.op == PredicateOp.LTE:
                return cell_val <= target_val
            if pred.op == PredicateOp.IN:
                return cell_val in target_val
            if pred.op == PredicateOp.CONTAINS:
                return str(target_val).lower() in str(cell_val).lower()
        except Exception:
            return False
        return False

    def execute_single_pass_query(
        self,
        table_id: str,
        workspace_id: str,
        column_index: int | None,
        op: AggregateOp | None,
        predicates: list[Predicate] | None = None,
        limit: int | None = None,
        version: str | None = None,
    ) -> tuple[Decimal | None, int, list[str], list[int], str]:
        ver = version or self.get_active_version(table_id, workspace_id)
        if not ver:
            return None, 0, [], [], hashlib.sha256(b"").hexdigest()[:16]

        matched_ids: list[str] = []
        matched_indices: list[int] = []
        numeric_values: list[Decimal] = []
        total_count = 0

        if ver == "legacy":
            cur = self._conn.execute(
                "SELECT * FROM table_rows WHERE workspace_id = ? AND table_id = ? ORDER BY row_index ASC",
                (workspace_id, table_id),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM versioned_rows WHERE workspace_id = ? AND table_id = ? AND version = ? ORDER BY row_index ASC",
                (workspace_id, table_id, ver),
            )

        for db_row in cur:
            if db_row["is_header"]:
                continue
            cells = _json_to_cells(db_row["cells_json"])
            if predicates:
                if not all(self._matches_predicate(cells, p) for p in predicates):
                    continue

            r_id = db_row["row_id"]
            r_idx = int(db_row["row_index"])

            total_count += 1
            matched_ids.append(r_id)
            matched_indices.append(r_idx)

            if column_index is not None and column_index < len(cells):
                cell = cells[column_index]
                val = cell.normalized_value
                if val is not None:
                    if isinstance(val, Decimal):
                        numeric_values.append(val)
                    else:
                        try:
                            clean_str = str(val).replace(",", "").strip()
                            numeric_values.append(Decimal(clean_str))
                        except Exception:
                            pass

            if limit is not None and total_count >= limit:
                break

        digest = hashlib.sha256(",".join(sorted(matched_ids)).encode()).hexdigest()[:16]

        agg_val: Decimal | None = None
        if op == AggregateOp.COUNT:
            agg_val = Decimal(total_count)
        elif op and numeric_values:
            if op == AggregateOp.SUM:
                agg_val = sum(numeric_values)
            elif op == AggregateOp.AVG:
                agg_val = sum(numeric_values) / Decimal(len(numeric_values))
            elif op == AggregateOp.MIN:
                agg_val = min(numeric_values)
            elif op == AggregateOp.MAX:
                agg_val = max(numeric_values)

        return agg_val, total_count, matched_ids, matched_indices, digest

    def invalidate_document_version(
        self,
        document_id: str,
        workspace_id: str,
        version: str | None = None,
    ) -> int:
        if not workspace_id:
            return 0
        deleted_count = 0
        with self._conn:
            if version:
                cur = self._conn.execute(
                    "SELECT COUNT(*) as cnt FROM versioned_rows WHERE workspace_id = ? AND version = ?",
                    (workspace_id, version),
                )
                deleted_count = cur.fetchone()["cnt"]
                self._conn.execute(
                    "DELETE FROM versioned_rows WHERE workspace_id = ? AND version = ?",
                    (workspace_id, version),
                )
                self._conn.execute(
                    "DELETE FROM versioned_schemas WHERE workspace_id = ? AND version = ?",
                    (workspace_id, version),
                )
                self._conn.execute(
                    "DELETE FROM active_versions WHERE workspace_id = ? AND version = ?",
                    (workspace_id, version),
                )
                self._conn.execute(
                    "DELETE FROM checkpoints WHERE workspace_id = ? AND document_id = ? AND version = ?",
                    (workspace_id, document_id, version),
                )
                self._conn.execute(
                    "DELETE FROM manifests WHERE workspace_id = ? AND document_id = ? AND version = ?",
                    (workspace_id, document_id, version),
                )
                self._conn.execute(
                    "DELETE FROM workspace_catalogue WHERE workspace_id = ? AND document_id = ? AND active_version = ?",
                    (workspace_id, document_id, version),
                )
            else:
                cur = self._conn.execute(
                    "SELECT COUNT(*) as cnt FROM versioned_rows WHERE workspace_id = ? AND table_id IN (SELECT table_id FROM versioned_schemas WHERE workspace_id = ? AND document_id = ?)",
                    (workspace_id, workspace_id, document_id),
                )
                deleted_count = cur.fetchone()["cnt"]
                self._conn.execute(
                    "DELETE FROM versioned_rows WHERE workspace_id = ? AND table_id IN (SELECT table_id FROM versioned_schemas WHERE workspace_id = ? AND document_id = ?)",
                    (workspace_id, workspace_id, document_id),
                )
                self._conn.execute(
                    "DELETE FROM versioned_schemas WHERE workspace_id = ? AND document_id = ?",
                    (workspace_id, document_id),
                )
                self._conn.execute(
                    "DELETE FROM active_versions WHERE workspace_id = ? AND document_id = ?",
                    (workspace_id, document_id),
                )
                self._conn.execute(
                    "DELETE FROM checkpoints WHERE workspace_id = ? AND document_id = ?",
                    (workspace_id, document_id),
                )
                self._conn.execute(
                    "DELETE FROM manifests WHERE workspace_id = ? AND document_id = ?",
                    (workspace_id, document_id),
                )
                self._conn.execute(
                    "DELETE FROM workspace_catalogue WHERE workspace_id = ? AND document_id = ?",
                    (workspace_id, document_id),
                )
        return deleted_count

    # -------------------------------------------------------------------------
    # Legacy / Compatibility API
    # -------------------------------------------------------------------------

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
                        _cells_to_json(row.cells),
                    ),
                )
            self._conn.execute(
                """
                INSERT OR REPLACE INTO workspace_catalogue
                (workspace_id, table_id, document_id, active_version, coverage_complete)
                VALUES (?, ?, ?, ?, ?)
                """,
                (workspace_id, table.table_id, table.document_id, "legacy", 1),
            )

    def get_table(self, table_id: str, workspace_id: str, version: str | None = None) -> StructuredTable | None:
        if not workspace_id:
            return None

        ver = version or self.get_active_version(table_id, workspace_id)
        if ver and ver != "legacy":
            cur_ver = self._conn.execute(
                "SELECT * FROM versioned_schemas WHERE workspace_id = ? AND table_id = ? AND version = ?",
                (workspace_id, table_id, ver),
            )
            v_row = cur_ver.fetchone()
            if v_row:
                schema = _json_to_schema(v_row["schema_json"])
                rows = self.query_rows(table_id, workspace_id, version=ver)
                return StructuredTable(
                    table_id=table_id,
                    document_id=v_row["document_id"],
                    sheet_name=None,
                    page_number=None,
                    schema=schema,
                    rows=tuple(rows),
                    row_count=len(rows),
                    col_count=len(schema.columns),
                )

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

        rows = self.query_rows(table_id, workspace_id, version="legacy")
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

    def query_rows(
        self,
        table_id: str,
        workspace_id: str,
        predicates: list[Predicate] | None = None,
        limit: int | None = None,
        version: str | None = None,
    ) -> list[TableRow]:
        if not workspace_id:
            return []

        ver = version or self.get_active_version(table_id, workspace_id)
        if not ver:
            return []

        if ver == "legacy":
            cur = self._conn.execute(
                "SELECT * FROM table_rows WHERE workspace_id = ? AND table_id = ? ORDER BY row_index ASC",
                (workspace_id, table_id),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM versioned_rows WHERE workspace_id = ? AND table_id = ? AND version = ? ORDER BY row_index ASC",
                (workspace_id, table_id, ver),
            )

        matched_rows: list[TableRow] = []
        for db_row in cur:
            if db_row["is_header"]:
                continue
            cells = _json_to_cells(db_row["cells_json"])
            if predicates and not all(self._matches_predicate(cells, p) for p in predicates):
                continue

            matched_rows.append(
                TableRow(
                    row_id=db_row["row_id"],
                    row_index=int(db_row["row_index"]),
                    cells=cells,
                    is_header=bool(db_row["is_header"]),
                    is_subtotal=bool(db_row["is_subtotal"]) if "is_subtotal" in db_row.keys() else False,
                    is_empty=bool(db_row["is_empty"]) if "is_empty" in db_row.keys() else False,
                )
            )
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
        version: str | None = None,
    ) -> Decimal | None:
        agg_val, _, _, _, _ = self.execute_single_pass_query(
            table_id=table_id,
            workspace_id=workspace_id,
            column_index=column_index,
            op=op,
            predicates=predicates,
            limit=None,
            version=version,
        )
        return agg_val

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
            self._conn.execute(
                "DELETE FROM versioned_schemas WHERE workspace_id = ? AND table_id = ?",
                (workspace_id, table_id),
            )
            self._conn.execute(
                "DELETE FROM versioned_rows WHERE workspace_id = ? AND table_id = ?",
                (workspace_id, table_id),
            )
            self._conn.execute(
                "DELETE FROM active_versions WHERE workspace_id = ? AND table_id = ?",
                (workspace_id, table_id),
            )
            self._conn.execute(
                "DELETE FROM workspace_catalogue WHERE workspace_id = ? AND table_id = ?",
                (workspace_id, table_id),
            )
        return True
