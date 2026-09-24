"""In-memory implementation of TableStore for testing and small datasets."""

from decimal import Decimal
import logging
from typing import Any

from db.table_store.base import AggregateOp, Predicate, PredicateOp, TableStore
from schemas.structured_table import StructuredTable, TableRow

logger = logging.getLogger(__name__)


class MemoryTableStore:
    def __init__(self) -> None:
        # Key: (workspace_id, table_id) -> StructuredTable (legacy)
        self._tables: dict[tuple[str, str], StructuredTable] = {}
        # Versioned schemas: (workspace_id, table_id, version) -> (TableSchema, doc_id, doc_ver, parser_ver, schema_ver)
        self._schemas: dict[tuple[str, str, str], tuple[TableSchema, str, str, str, str]] = {}
        # Versioned rows: (workspace_id, table_id, version) -> list[TableRow]
        self._versioned_rows: dict[tuple[str, str, str], list[TableRow]] = {}
        # Active versions: (workspace_id, table_id) -> active_version
        self._active_versions: dict[tuple[str, str], str] = {}
        # Active version metadata: (workspace_id, table_id) -> dict
        self._active_meta: dict[tuple[str, str], dict] = {}
        # Checkpoints: (workspace_id, document_id, version) -> dict
        self._checkpoints: dict[tuple[str, str, str], dict] = {}
        # Manifests: (workspace_id, document_id, version) -> dict
        self._manifests: dict[tuple[str, str, str], dict] = {}
        # Latest manifests: (workspace_id, document_id) -> dict
        self._latest_manifests: dict[tuple[str, str], dict] = {}
        # Workspace catalogue: workspace_id -> set of table_ids
        self._workspace_catalogue: dict[str, set[str]] = {}

    def store_table(self, table: StructuredTable, workspace_id: str) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty for table storage")
        key = (workspace_id, table.table_id)
        self._tables[key] = table
        if workspace_id not in self._workspace_catalogue:
            self._workspace_catalogue[workspace_id] = set()
        self._workspace_catalogue[workspace_id].add(table.table_id)
        logger.debug("MemoryTableStore: stored table=%s workspace=%s rows=%d", table.table_id, workspace_id, len(table.rows))

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
        key = (workspace_id, table_id, document_version)
        self._schemas[key] = (schema, document_id, document_version, parser_version, schema_version)
        if key not in self._versioned_rows:
            self._versioned_rows[key] = []

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
        row_key = (workspace_id, table_id, version)
        if row_key not in self._versioned_rows:
            self._versioned_rows[row_key] = []
        self._versioned_rows[row_key].extend(rows)

        cp_key = (workspace_id, document_id, version)
        self._checkpoints[cp_key] = {
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
        }

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
        self._active_versions[(workspace_id, table_id)] = version
        self._active_meta[(workspace_id, table_id)] = {
            "version": version,
            "document_id": document_id,
            "coverage_complete": manifest.get("coverage_complete", False),
            "processing_finished": manifest.get("processing_finished", False),
            "source_rows": manifest.get("source_rows", 0),
            "persisted_rows": manifest.get("persisted_rows", 0),
        }
        self._manifests[(workspace_id, document_id, version)] = dict(manifest)
        self._latest_manifests[(workspace_id, document_id)] = dict(manifest)
        if workspace_id not in self._workspace_catalogue:
            self._workspace_catalogue[workspace_id] = set()
        self._workspace_catalogue[workspace_id].add(table_id)

    def get_active_version(self, table_id: str, workspace_id: str) -> str | None:
        return self._active_versions.get((workspace_id, table_id))

    def list_tables(self, workspace_id: str) -> list[str]:
        return sorted(list(self._workspace_catalogue.get(workspace_id, set())))

    def get_ingestion_manifest(
        self,
        document_id: str,
        workspace_id: str,
        version: str | None = None,
    ) -> dict | None:
        if version:
            return self._manifests.get((workspace_id, document_id, version))
        return self._latest_manifests.get((workspace_id, document_id))

    def get_checkpoint(
        self,
        document_id: str,
        workspace_id: str,
        version: str,
    ) -> dict | None:
        return self._checkpoints.get((workspace_id, document_id, version))

    def has_coverage_complete_table(self, table_id: str, workspace_id: str) -> bool:
        meta = self._active_meta.get((workspace_id, table_id))
        if meta:
            return bool(meta.get("coverage_complete", False))
        return False

    def get_table(self, table_id: str, workspace_id: str, version: str | None = None) -> StructuredTable | None:
        if not workspace_id:
            return None
        # Check versioned schema
        ver = version or self.get_active_version(table_id, workspace_id)
        if ver and (workspace_id, table_id, ver) in self._schemas:
            schema, doc_id, doc_ver, _, _ = self._schemas[(workspace_id, table_id, ver)]
            rows = tuple(self._versioned_rows.get((workspace_id, table_id, ver), []))
            return StructuredTable(
                table_id=table_id,
                document_id=doc_id,
                sheet_name=None,
                page_number=None,
                schema=schema,
                rows=rows,
                row_count=len(rows),
                col_count=len(schema.columns) if schema else 0,
            )
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
            # Handle string-formatted numbers in cell_val or target_val
            if pred.op in (PredicateOp.EQ, PredicateOp.NEQ, PredicateOp.GT, PredicateOp.LT, PredicateOp.GTE, PredicateOp.LTE):
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

    def query_rows(
        self,
        table_id: str,
        workspace_id: str,
        predicates: list[Predicate] | None = None,
        limit: int | None = None,
        version: str | None = None,
    ) -> list[TableRow]:
        _, _, _, rows_matched, _ = self._execute_pass(
            table_id=table_id,
            workspace_id=workspace_id,
            column_index=None,
            op=None,
            predicates=predicates,
            limit=limit,
            version=version,
        )
        return rows_matched

    def aggregate(
        self,
        table_id: str,
        workspace_id: str,
        column_index: int,
        op: AggregateOp,
        predicates: list[Predicate] | None = None,
        version: str | None = None,
    ) -> Decimal | None:
        val, _, _, _, _ = self._execute_pass(
            table_id=table_id,
            workspace_id=workspace_id,
            column_index=column_index,
            op=op,
            predicates=predicates,
            limit=None,
            version=version,
        )
        return val

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
        val, count, row_ids, _, digest = self._execute_pass(
            table_id=table_id,
            workspace_id=workspace_id,
            column_index=column_index,
            op=op,
            predicates=predicates,
            limit=limit,
            version=version,
        )
        row_indices = []
        ver = version or self.get_active_version(table_id, workspace_id)
        candidate_rows = []
        if ver and (workspace_id, table_id, ver) in self._versioned_rows:
            candidate_rows = self._versioned_rows[(workspace_id, table_id, ver)]
        elif (workspace_id, table_id) in self._tables:
            candidate_rows = self._tables[(workspace_id, table_id)].rows
        row_id_set = set(row_ids)
        for r in candidate_rows:
            if r.row_id in row_id_set:
                row_indices.append(r.row_index)
        return val, count, row_ids, row_indices, digest

    def _execute_pass(
        self,
        table_id: str,
        workspace_id: str,
        column_index: int | None,
        op: AggregateOp | None,
        predicates: list[Predicate] | None = None,
        limit: int | None = None,
        version: str | None = None,
    ) -> tuple[Decimal | None, int, list[str], list[TableRow], str]:
        import hashlib

        ver = version or self.get_active_version(table_id, workspace_id)
        candidate_rows: list[TableRow] = []
        if ver and (workspace_id, table_id, ver) in self._versioned_rows:
            candidate_rows = self._versioned_rows[(workspace_id, table_id, ver)]
        elif (workspace_id, table_id) in self._tables:
            candidate_rows = list(self._tables[(workspace_id, table_id)].rows)

        matched_rows: list[TableRow] = []
        matched_ids: list[str] = []
        numeric_values: list[Decimal] = []

        for row in candidate_rows:
            if row.is_header:
                continue
            if predicates:
                if not all(self._matches_predicate(row, p) for p in predicates):
                    continue

            matched_rows.append(row)
            matched_ids.append(row.row_id)

            if column_index is not None and column_index < len(row.cells):
                cell = row.cells[column_index]
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

            if limit is not None and len(matched_rows) >= limit:
                break

        count = len(matched_rows)
        digest = hashlib.sha256(",".join(sorted(matched_ids)).encode()).hexdigest()[:16]

        agg_val: Decimal | None = None
        if op == AggregateOp.COUNT:
            agg_val = Decimal(count)
        elif op and numeric_values:
            if op == AggregateOp.SUM:
                agg_val = sum(numeric_values)
            elif op == AggregateOp.AVG:
                agg_val = sum(numeric_values) / Decimal(len(numeric_values))
            elif op == AggregateOp.MIN:
                agg_val = min(numeric_values)
            elif op == AggregateOp.MAX:
                agg_val = max(numeric_values)

        return agg_val, count, matched_ids, matched_rows, digest

    def invalidate_document_version(
        self,
        document_id: str,
        workspace_id: str,
        version: str | None = None,
    ) -> int:
        count = 0
        to_del_schema = []
        to_del_rows = []
        for (ws, tid, v) in list(self._schemas.keys()):
            if ws == workspace_id:
                doc_id = self._schemas[(ws, tid, v)][1]
                if doc_id == document_id and (version is None or v == version):
                    to_del_schema.append((ws, tid, v))
                    to_del_rows.append((ws, tid, v))
                    if self._active_versions.get((ws, tid)) == v:
                        del self._active_versions[(ws, tid)]
                        if (ws, tid) in self._active_meta:
                            del self._active_meta[(ws, tid)]
                        if ws in self._workspace_catalogue and tid in self._workspace_catalogue[ws]:
                            self._workspace_catalogue[ws].remove(tid)

        for k in to_del_schema:
            del self._schemas[k]
        for k in to_del_rows:
            if k in self._versioned_rows:
                count += len(self._versioned_rows[k])
                del self._versioned_rows[k]

        for (ws, did, v) in list(self._checkpoints.keys()):
            if ws == workspace_id and did == document_id and (version is None or v == version):
                del self._checkpoints[(ws, did, v)]

        for (ws, did, v) in list(self._manifests.keys()):
            if ws == workspace_id and did == document_id and (version is None or v == version):
                del self._manifests[(ws, did, v)]

        if version is None and (workspace_id, document_id) in self._latest_manifests:
            del self._latest_manifests[(workspace_id, document_id)]

        return count

    def delete_table(self, table_id: str, workspace_id: str) -> bool:
        key = (workspace_id, table_id)
        deleted = False
        if key in self._tables:
            del self._tables[key]
            deleted = True
        if key in self._active_versions:
            ver = self._active_versions.pop(key)
            if (workspace_id, table_id, ver) in self._schemas:
                del self._schemas[(workspace_id, table_id, ver)]
            if (workspace_id, table_id, ver) in self._versioned_rows:
                del self._versioned_rows[(workspace_id, table_id, ver)]
            if key in self._active_meta:
                del self._active_meta[key]
            deleted = True
        if workspace_id in self._workspace_catalogue and table_id in self._workspace_catalogue[workspace_id]:
            self._workspace_catalogue[workspace_id].remove(table_id)
            deleted = True
        return deleted
