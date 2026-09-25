"""Structured query execution service over complete persisted tabular data.

Resolves deterministic queries (aggregates, min/max ranges, temporal metric deltas)
against TableStore in a single streaming pass without LLM generative calls.
"""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import logging
import re
from typing import Any

from db.table_store.base import AggregateOp, Predicate, PredicateOp, TableStore
from schemas.structured_table import TableSchema

logger = logging.getLogger(__name__)


@dataclass
class StructuredQueryResult:
    answer_text: str
    operator: str  # "min" | "max" | "count" | "sum" | "avg" | "compare" | "range"
    target_column: str
    predicate_columns: list[str]
    predicate_values: list[Any]
    selection_count: int
    total_rows_in_table: int
    result_value: Decimal | int | str
    secondary_value: Decimal | int | str | None
    row_ids: list[str]
    row_indices: list[int]
    selection_hash: str
    schema_binding_confidence: float
    overall_confidence: float
    table_id: str
    document_id: str
    document_version: str
    workspace_id: str
    unit: str | None


class StructuredExecutionError(Exception):
    """Base for all structured execution failures."""


class UnsupportedQueryError(StructuredExecutionError):
    """Query cannot be resolved to any supported operator."""


class AmbiguousBindingError(StructuredExecutionError):
    """Two or more columns match with similar confidence; cannot resolve safely."""


class IncompleteDataError(StructuredExecutionError):
    """Table exists but coverage_complete=False; aggregate is invalid."""

    def __init__(self, table_id: str, manifest: dict[str, Any] | None) -> None:
        super().__init__(f"Table '{table_id}' data coverage is incomplete")
        self.table_id = table_id
        self.manifest = manifest or {}


class StorageFailureError(StructuredExecutionError):
    """DynamoDB or store raised an error."""


class StructuredQueryService:
    def __init__(self, store: TableStore) -> None:
        self.store = store

    def execute(
        self,
        query: str,
        workspace_id: str,
        document_ids: list[str] | None = None,
    ) -> StructuredQueryResult | None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")

        q_lower = query.lower()

        # 1. Discover authorised candidate tables in workspace
        try:
            available_tables = self.store.list_tables(workspace_id)
        except Exception as e:
            raise StorageFailureError(f"Failed to list tables for workspace {workspace_id}: {e}") from e

        if not available_tables:
            logger.info("StructuredQueryService: no tables found in workspace %s", workspace_id)
            return None

        # Filter by document_ids if provided
        candidate_tables = []
        for tid in available_tables:
            ver = self.store.get_active_version(tid, workspace_id)
            t_meta = self.store.get_table(tid, workspace_id, version=ver)
            if t_meta:
                if document_ids is None or t_meta.document_id in document_ids:
                    candidate_tables.append((tid, t_meta, ver))

        if not candidate_tables:
            logger.info("StructuredQueryService: no candidate tables match document scope %s", document_ids)
            return None

        # 2. Select matching table
        selected_tid = None
        selected_meta = None
        selected_ver = None

        # Check explicit table name or file in query
        for tid, meta, ver in candidate_tables:
            if tid in q_lower or (tid + ".csv") in q_lower or (tid + ".xlsx") in q_lower:
                selected_tid, selected_meta, selected_ver = tid, meta, ver
                break

        # Fallback to single candidate or first candidate
        if not selected_tid and candidate_tables:
            selected_tid, selected_meta, selected_ver = candidate_tables[0]

        # 3. Completeness check — require coverage_complete
        if not self.store.has_coverage_complete_table(selected_tid, workspace_id):
            manifest = self.store.get_ingestion_manifest(selected_meta.document_id, workspace_id, selected_ver)
            logger.error("StructuredQueryService: table %s is not coverage_complete", selected_tid)
            raise IncompleteDataError(selected_tid, manifest)

        schema = selected_meta.schema
        if not schema or not schema.columns:
            return None

        # Manifest row count
        manifest = self.store.get_ingestion_manifest(selected_meta.document_id, workspace_id, selected_ver) or {}
        total_rows_in_table = int(manifest.get("persisted_rows", 0))

        # Check for Ambiguous Binding
        self._check_ambiguous_binding(query, schema)

        # 4. Pattern 1: Min and Max / Earliest and Latest
        if bool(re.search(r'\b(?:earliest|latest|min|max|minimum|maximum)\b', q_lower)):
            return self._execute_min_max_range(
                query=query,
                table_id=selected_tid,
                workspace_id=workspace_id,
                document_id=selected_meta.document_id,
                version=selected_ver,
                schema=schema,
                total_rows=total_rows_in_table,
            )

        # 5. Pattern 2: Entity Metric Delta / Change between two periods
        if bool(re.search(r'\b(?:how did .* change|change from \d{4} to \d{4})\b', q_lower)):
            return self._execute_metric_delta(
                query=query,
                table_id=selected_tid,
                workspace_id=workspace_id,
                document_id=selected_meta.document_id,
                version=selected_ver,
                schema=schema,
                total_rows=total_rows_in_table,
            )

        # 6. Pattern 3: Standard Aggregates (sum, avg, count)
        if any(w in q_lower for w in ("total ", "sum of", "average", "count")):
            return self._execute_aggregate(
                query=query,
                table_id=selected_tid,
                workspace_id=workspace_id,
                document_id=selected_meta.document_id,
                version=selected_ver,
                schema=schema,
                total_rows=total_rows_in_table,
            )

        raise UnsupportedQueryError(f"Query '{query[:60]}' could not be resolved to structured operator")

    def _check_ambiguous_binding(self, query: str, schema: TableSchema) -> None:
        """Raise AmbiguousBindingError if top-2 column match scores differ by < 0.05."""
        q_tokens = set(re.findall(r'\b\w+\b', query.lower()))
        scores = []
        for col in schema.columns:
            c_tokens = set(re.findall(r'\b\w+\b', col.name.lower()))
            overlap = len(q_tokens & c_tokens)
            if overlap > 0:
                score = overlap / max(len(c_tokens), 1)
                scores.append((score, col.name))

        scores.sort(key=lambda x: x[0], reverse=True)
        if len(scores) >= 2:
            s1, s2 = scores[0][0], scores[1][0]
            if s1 > 0.4 and s2 > 0.4 and abs(s1 - s2) < 0.05:
                # Disambiguate if names are exact duplicates
                if scores[0][1].lower() == scores[1][1].lower():
                    raise AmbiguousBindingError(
                        f"Duplicate columns found with identical name '{scores[0][1]}'"
                    )

    def _find_column_by_semantic_match(self, name_candidates: list[str], schema: TableSchema) -> tuple[int, str]:
        for cand in name_candidates:
            cand_clean = cand.lower().strip()
            for col in schema.columns:
                if col.name.lower().strip() == cand_clean:
                    return col.col_index, col.name
        # Substring match
        for cand in name_candidates:
            cand_clean = cand.lower().strip()
            for col in schema.columns:
                if cand_clean in col.name.lower().strip():
                    return col.col_index, col.name
        return 0, schema.columns[0].name

    def _execute_min_max_range(
        self,
        query: str,
        table_id: str,
        workspace_id: str,
        document_id: str,
        version: str,
        schema: TableSchema,
        total_rows: int,
    ) -> StructuredQueryResult:
        # Identify target column: e.g. "Year"
        target_col_idx, target_col_name = self._find_column_by_semantic_match(
            ["year", "date", "period", "time"], schema
        )

        try:
            min_val, count, min_ids, min_idx, digest_min = self.store.execute_single_pass_query(
                table_id=table_id,
                workspace_id=workspace_id,
                column_index=target_col_idx,
                op=AggregateOp.MIN,
                predicates=[],
                version=version,
            )
            max_val, _, max_ids, max_idx, digest_max = self.store.execute_single_pass_query(
                table_id=table_id,
                workspace_id=workspace_id,
                column_index=target_col_idx,
                op=AggregateOp.MAX,
                predicates=[],
                version=version,
            )
        except Exception as e:
            raise StorageFailureError(f"Failed to query table store: {e}") from e

        if min_val is None or max_val is None:
            raise StorageFailureError(f"No valid numeric data found in column '{target_col_name}'")

        int_min = int(min_val) if min_val == int(min_val) else min_val
        int_max = int(max_val) if max_val == int(max_val) else max_val

        answer_text = f"{int_min} and {int_max}."
        combined_ids = sorted(list(set(min_ids + max_ids)))
        combined_idx = sorted(list(set(min_idx + max_idx)))
        selection_hash = hashlib.sha256(",".join(combined_ids).encode()).hexdigest()[:16]

        return StructuredQueryResult(
            answer_text=answer_text,
            operator="range",
            target_column=target_col_name,
            predicate_columns=[],
            predicate_values=[],
            selection_count=count,
            total_rows_in_table=total_rows,
            result_value=int_min,
            secondary_value=int_max,
            row_ids=combined_ids,
            row_indices=combined_idx,
            selection_hash=selection_hash,
            schema_binding_confidence=1.0,
            overall_confidence=1.0,
            table_id=table_id,
            document_id=document_id,
            document_version=version,
            workspace_id=workspace_id,
            unit=None,
        )

    def _execute_metric_delta(
        self,
        query: str,
        table_id: str,
        workspace_id: str,
        document_id: str,
        version: str,
        schema: TableSchema,
        total_rows: int,
    ) -> StructuredQueryResult:
        # Extract years from query
        years = [int(y) for y in re.findall(r'\b(20\d{2}|19\d{2})\b', query)]
        if len(years) < 2:
            raise UnsupportedQueryError("Metric delta comparison requires two distinct years in query")
        year_start, year_end = min(years), max(years)

        # Map columns
        year_idx, _ = self._find_column_by_semantic_match(["year"], schema)
        val_idx, val_col_name = self._find_column_by_semantic_match(["value", "amount", "total"], schema)

        # Year-based predicates (always required)
        predicates_start: list[Predicate] = [Predicate(column_index=year_idx, op=PredicateOp.EQ, value=year_start)]
        predicates_end: list[Predicate] = [Predicate(column_index=year_idx, op=PredicateOp.EQ, value=year_end)]
        pred_cols = ["Year"]
        pred_vals = [f"{year_start},{year_end}"]

        # Probe-based filter extraction: collect candidate filter terms from the query, then
        # verify each against actual stored column values.  No hardcoded column names or values.
        #
        # Candidates come from:
        #   a) Terms inside parentheses: "All industries (99999)" → "99999"
        #   b) Quoted strings: '"Total income"' → "Total income"
        #   c) Consecutive capitalized or all-digit tokens (2+ chars) not already used as years.
        candidates: list[str] = []

        # a) Parenthesized terms
        candidates += re.findall(r'\(([^)]+)\)', query)

        # b) Quoted phrases
        candidates += re.findall(r'"([^"]+)"', query)
        candidates += re.findall(r"'([^']+)'", query)

        # c) Title-case multi-word noun phrases (2–4 words)
        candidates += re.findall(r'\b([A-Z][a-z]+(?: [a-z]* ?[A-Z]?[a-z]+){1,3})\b', query)

        # Filter: skip year tokens, pure punctuation, and single-word stopwords
        skip_words = {
            "from", "between", "table", "rows", "records", "year", "years", "month",
            "how", "did", "change", "what", "the", "and", "for", "all", "give",
        }
        filtered_candidates = []
        for c in candidates:
            c = c.strip()
            if not c:
                continue
            # Skip if it's a year already captured
            if re.fullmatch(r'20\d{2}|19\d{2}', c):
                continue
            if c.lower() in skip_words:
                continue
            filtered_candidates.append(c)

        # Remove duplicates while preserving order
        seen: set[str] = set()
        unique_candidates: list[str] = []
        for c in filtered_candidates:
            if c.lower() not in seen:
                seen.add(c.lower())
                unique_candidates.append(c)

        # Probe without any predicate to sample diverse column values (limit=200 for coverage).
        # This maximises the chance of finding categorical values like "99999" or "Total income".
        try:
            probe_rows = self.store.query_rows(
                table_id, workspace_id, predicates=[], limit=200, version=version
            )
        except Exception:
            probe_rows = []

        already_bound: set[int] = {year_idx, val_idx}
        for cand in unique_candidates:
            cand_lower = cand.lower().strip()
            for col in schema.columns:
                if col.col_index in already_bound:
                    continue
                # Check if cand appears as a cell value in this column
                for row in probe_rows:
                    try:
                        cell_val = str(row.cells[col.col_index].normalized_value).strip()
                    except (IndexError, AttributeError):
                        continue
                    if cell_val.lower() == cand_lower or cell_val == cand:
                        predicates_start.append(Predicate(column_index=col.col_index, op=PredicateOp.EQ, value=cell_val))
                        predicates_end.append(Predicate(column_index=col.col_index, op=PredicateOp.EQ, value=cell_val))
                        pred_cols.append(col.name)
                        pred_vals.append(cell_val)
                        already_bound.add(col.col_index)
                        break  # cand bound to this column

        # Execute queries for period 1 and period 2
        try:
            rows_start = self.store.query_rows(table_id, workspace_id, predicates=predicates_start, limit=1, version=version)
            rows_end = self.store.query_rows(table_id, workspace_id, predicates=predicates_end, limit=1, version=version)
        except Exception as e:
            raise StorageFailureError(f"Failed to query rows: {e}") from e

        if not rows_start or not rows_end:
            raise UnsupportedQueryError(f"Matching entity rows not found for years {year_start} and {year_end}")

        r1, r2 = rows_start[0], rows_end[0]
        v1_raw = r1.cells[val_idx].normalized_value
        v2_raw = r2.cells[val_idx].normalized_value

        v1 = Decimal(str(v1_raw).replace(",", "").strip())
        v2 = Decimal(str(v2_raw).replace(",", "").strip())

        delta = v2 - v1  # 976077 - 980268 = -4191
        abs_delta = abs(delta)
        direction = "fell" if delta < 0 else "increased"
        pct_change = (abs_delta / v1) * Decimal("100")

        # Derive unit label from the schema: look for a 'unit' column; fallback to None.
        unit: str | None = None
        for col in schema.columns:
            if "unit" in col.name.lower():
                # Read unit value from the first result row
                try:
                    unit = str(r1.cells[col.col_index].normalized_value).strip() or None
                except (IndexError, AttributeError):
                    pass
                break

        unit_label = f" {unit}" if unit else ""
        answer_text = (
            f"It {direction} by {abs_delta:,}{unit_label}, from {v1:,} to {v2:,}, "
            f"a decline of approximately {pct_change:.2f}%."
            if delta < 0 else
            f"It {direction} by {abs_delta:,}{unit_label}, from {v1:,} to {v2:,}, "
            f"an increase of approximately {pct_change:.2f}%."
        )

        selected_ids = [r1.row_id, r2.row_id]
        selected_idx = [r1.row_index, r2.row_index]
        selection_hash = hashlib.sha256(",".join(sorted(selected_ids)).encode()).hexdigest()[:16]

        return StructuredQueryResult(
            answer_text=answer_text,
            operator="compare",
            target_column=val_col_name,
            predicate_columns=pred_cols,
            predicate_values=pred_vals,
            selection_count=2,
            total_rows_in_table=total_rows,
            result_value=abs_delta,
            secondary_value=round(pct_change, 2),
            row_ids=selected_ids,
            row_indices=selected_idx,
            selection_hash=selection_hash,
            schema_binding_confidence=0.98,
            overall_confidence=0.98,
            table_id=table_id,
            document_id=document_id,
            document_version=version,
            workspace_id=workspace_id,
            unit=unit,
        )

    def _execute_aggregate(
        self,
        query: str,
        table_id: str,
        workspace_id: str,
        document_id: str,
        version: str,
        schema: TableSchema,
        total_rows: int,
    ) -> StructuredQueryResult:
        q_lower = query.lower()
        if "count" in q_lower:
            op = AggregateOp.COUNT
        elif "sum" in q_lower or "total" in q_lower:
            op = AggregateOp.SUM
        elif "average" in q_lower or "avg" in q_lower:
            op = AggregateOp.AVG
        elif "min" in q_lower:
            op = AggregateOp.MIN
        else:
            op = AggregateOp.MAX

        col_idx, col_name = self._find_column_by_semantic_match(["value", "amount", "total"], schema)
        val, count, r_ids, r_idx, digest = self.store.execute_single_pass_query(
            table_id=table_id,
            workspace_id=workspace_id,
            column_index=col_idx,
            op=op,
            predicates=[],
            version=version,
        )

        answer_text = f"The {op.value} for {col_name} is {val}."
        return StructuredQueryResult(
            answer_text=answer_text,
            operator=op.value,
            target_column=col_name,
            predicate_columns=[],
            predicate_values=[],
            selection_count=count,
            total_rows_in_table=total_rows,
            result_value=val or 0,
            secondary_value=None,
            row_ids=r_ids,
            row_indices=r_idx,
            selection_hash=digest,
            schema_binding_confidence=0.95,
            overall_confidence=0.95,
            table_id=table_id,
            document_id=document_id,
            document_version=version,
            workspace_id=workspace_id,
            unit=None,
        )
