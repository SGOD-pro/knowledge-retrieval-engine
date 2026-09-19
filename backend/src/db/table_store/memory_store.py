"""In-memory implementation of TableStore for testing and small datasets."""

from decimal import Decimal
import logging
from typing import Any

from db.table_store.base import AggregateOp, Predicate, PredicateOp, TableStore
from schemas.structured_table import StructuredTable, TableRow

logger = logging.getLogger(__name__)


class MemoryTableStore:
    def __init__(self) -> None:
        # Key: (workspace_id, table_id) -> StructuredTable
        self._tables: dict[tuple[str, str], StructuredTable] = {}

    def store_table(self, table: StructuredTable, workspace_id: str) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty for table storage")
        key = (workspace_id, table.table_id)
        self._tables[key] = table
        logger.debug("MemoryTableStore: stored table=%s workspace=%s rows=%d", table.table_id, workspace_id, len(table.rows))

    def get_table(self, table_id: str, workspace_id: str) -> StructuredTable | None:
        if not workspace_id:
            return None
        return self._tables.get((workspace_id, table_id))

    def _matches_predicate(self, row: TableRow, pred: Predicate) -> bool:
        if pred.column_index < 0 or pred.column_index >= len(row.cells):
            return False
        cell = row.cells[pred.column_index]
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

        # Numeric or generic comparisons
        try:
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

    def query_rows(
        self,
        table_id: str,
        workspace_id: str,
        predicates: list[Predicate] | None = None,
        limit: int | None = None,
    ) -> list[TableRow]:
        table = self.get_table(table_id, workspace_id)
        if not table:
            return []

        results: list[TableRow] = []
        for row in table.rows:
            if row.is_header:
                continue
            if predicates:
                if all(self._matches_predicate(row, p) for p in predicates):
                    results.append(row)
            else:
                results.append(row)

            if limit is not None and len(results) >= limit:
                break

        return results

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

        values: list[Decimal] = []
        for r in rows:
            if column_index < len(r.cells):
                cell = r.cells[column_index]
                val = cell.normalized_value
                if val is not None:
                    if isinstance(val, Decimal):
                        values.append(val)
                    else:
                        try:
                            clean_str = str(val).replace(",", "").strip()
                            values.append(Decimal(clean_str))
                        except Exception:
                            pass

        if op == AggregateOp.COUNT:
            return Decimal(len(rows))

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
        key = (workspace_id, table_id)
        if key in self._tables:
            del self._tables[key]
            return True
        return False
