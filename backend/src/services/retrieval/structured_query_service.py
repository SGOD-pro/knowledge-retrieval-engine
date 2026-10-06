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
from schemas.structured_table import InferredDtype, TableSchema

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


class UnresolvedFilterError(StructuredExecutionError):
    """A required filter entity was not found in the table."""

    def __init__(self, table_id: str, filter_values: list[Any]) -> None:
        super().__init__(f"Required filter(s) {filter_values} not found in table '{table_id}'")
        self.table_id = table_id
        self.filter_values = filter_values


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

        # Check explicit table name or file in query (sort by length descending to match more specific names first)
        candidate_tables_sorted = sorted(candidate_tables, key=lambda x: len(x[0]), reverse=True)
        for tid, meta, ver in candidate_tables_sorted:
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
        # Identify target column: check if query specifically mentions a column name
        q_lower = query.lower()
        mentioned_col = None
        for col in schema.columns:
            if col.name.lower() in q_lower:
                mentioned_col = (col.col_index, col.name)
                break

        if mentioned_col:
            target_col_idx, target_col_name = mentioned_col
        else:
            target_col_idx, target_col_name = self._find_column_by_semantic_match(
                ["year", "date", "period", "time", "value", "amount", "score", "count"], schema
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
        year_idx, _ = self._find_column_by_semantic_match(["year", "date", "period"], schema)

        # Check if query mentions a specific metric column name
        q_lower = query.lower()
        mentioned_val_col = None
        # Prioritize numeric columns (decimal, integer, percentage, currency) mentioned in the query
        for col in schema.columns:
            if col.col_index == year_idx:
                continue
            if col.inferred_dtype in (InferredDtype.DECIMAL, InferredDtype.INTEGER, InferredDtype.PERCENTAGE, InferredDtype.CURRENCY, "decimal", "integer", "percentage", "currency"):
                if col.name.lower() in q_lower:
                    mentioned_val_col = (col.col_index, col.name)
                    break

        if not mentioned_val_col:
            for col in schema.columns:
                if col.col_index == year_idx:
                    continue
                if col.name.lower() in q_lower:
                    mentioned_val_col = (col.col_index, col.name)
                    break

        if mentioned_val_col:
            val_idx, val_col_name = mentioned_val_col
        else:
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
        #   c) Capitalized multi-word noun phrases
        #   d) Phrases between query intent verbs ("how did X change")
        #   e) Non-stopword n-grams and single identifier tokens (e.g. "total income", "StationDeep")
        candidates: list[str] = []

        # a) Parenthesized terms
        candidates += re.findall(r'\(([^)]+)\)', query)

        # b) Quoted phrases
        candidates += re.findall(r'"([^"]+)"', query)
        candidates += re.findall(r"'([^']+)'", query)

        # c) Capitalized multi-word noun phrases
        candidates += re.findall(r'\b([A-Z][a-z]+(?: [a-z]* ?[A-Z]?[a-z]+){1,3})\b', query)

        # d) Phrases between intent verbs
        candidates += re.findall(
            r'(?:how did|what did|change in|measure of|value of)\s+([a-zA-Z0-9_ ]+?)\s+(?:change|from|between)',
            query,
            re.IGNORECASE,
        )

        # Filter: skip year tokens, pure punctuation, and single-word stopwords
        skip_words = {
            "from", "between", "table", "rows", "records", "year", "years", "month",
            "how", "did", "change", "what", "the", "and", "for", "all", "give",
            "amount", "percentage", "to", "in", "of", "on", "at", "by", "with",
        }

        # e) Single identifier tokens (capitalized, alphanumeric, or leading-zero codes)
        candidates += [
            w for w in re.findall(r'\b[A-Za-z0-9_]{2,}\b', query)
            if w.lower() not in skip_words and not re.fullmatch(r'20\d{2}|19\d{2}', w)
        ]

        # f) Non-stopword bi-grams and tri-grams
        words = re.findall(r'\b[a-zA-Z0-9_]+\b', query)
        for i in range(len(words) - 1):
            w1, w2 = words[i], words[i + 1]
            if w1.lower() not in skip_words and w2.lower() not in skip_words:
                candidates.append(f"{w1} {w2}")
        for i in range(len(words) - 2):
            w1, w2, w3 = words[i], words[i + 1], words[i + 2]
            if w1.lower() not in skip_words and w3.lower() not in skip_words:
                candidates.append(f"{w1} {w2} {w3}")
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

        # Collect explicitly requested filter entities (e.g. "For <target>", quoted phrases, parenthesized terms)
        explicit_requirements: list[str] = []
        for m in re.finditer(r'\bfor\s+([A-Za-z0-9_]+(?:\s+[A-Za-z0-9_]+)?)\b', query, re.IGNORECASE):
            target = m.group(1).strip()
            if target.lower() not in skip_words and not re.fullmatch(r'20\d{2}|19\d{2}', target):
                if target.lower() != table_id.lower() and target.lower() not in (table_id.lower() + ".csv", table_id.lower() + ".xlsx"):
                    explicit_requirements.append(target)
        for q_str in re.findall(r'["\']([^"\']+)["\']', query):
            q_clean = q_str.strip()
            if q_clean.lower() not in skip_words and not re.fullmatch(r'20\d{2}|19\d{2}', q_clean):
                if q_clean.lower() != table_id.lower() and q_clean.lower() not in (table_id.lower() + ".csv", table_id.lower() + ".xlsx"):
                    explicit_requirements.append(q_clean)
        for p_str in re.findall(r'\(([^)]+)\)', query):
            p_clean = p_str.strip()
            if p_clean.lower() not in skip_words and not re.fullmatch(r'20\d{2}|19\d{2}', p_clean):
                if p_clean.lower() != table_id.lower() and p_clean.lower() not in (table_id.lower() + ".csv", table_id.lower() + ".xlsx"):
                    explicit_requirements.append(p_clean)

        # Check probe rows first (fast in-memory check)
        try:
            probe_rows = self.store.query_rows(
                table_id, workspace_id, predicates=[], limit=200, version=version
            )
        except Exception:
            probe_rows = []

        already_bound: set[int] = {year_idx, val_idx}
        for cand in unique_candidates:
            cand_lower = cand.lower().strip()
            bound = False
            for col in schema.columns:
                if col.col_index in already_bound:
                    continue
                # Check probe rows first (fast in-memory check)
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
                        bound = True
                        break
                if bound:
                    break

            # If not found in first 200 probe rows, query store directly with predicate
            # (supports categorical/entity values located outside the first 200 rows, e.g. COHORT_DEEP at row 250).
            # Restrict deep store search to explicit requirements; do NOT scan 60,000 rows for table names or non-entity tokens.
            if not bound and explicit_requirements:
                is_explicit = any(
                    cand_lower == req.lower()
                    for req in explicit_requirements
                )
                is_table_name = (
                    cand_lower == table_id.lower()
                    or cand_lower in (f"{table_id.lower()}.csv", f"{table_id.lower()}.xlsx")
                )
                if is_explicit and not is_table_name:
                    for col in schema.columns:
                        if col.col_index in already_bound:
                            continue
                        try:
                            val_pred = cand
                            if col.inferred_dtype in ("integer", "decimal") and cand.isdigit():
                                val_pred = int(cand)
                            matching = self.store.query_rows(
                                table_id,
                                workspace_id,
                                predicates=[Predicate(column_index=col.col_index, op=PredicateOp.EQ, value=val_pred)],
                                limit=1,
                                version=version,
                            )
                            if matching:
                                matched_val = str(matching[0].cells[col.col_index].normalized_value).strip()
                                pred_obj = matching[0].cells[col.col_index].normalized_value
                                predicates_start.append(Predicate(column_index=col.col_index, op=PredicateOp.EQ, value=pred_obj))
                                predicates_end.append(Predicate(column_index=col.col_index, op=PredicateOp.EQ, value=pred_obj))
                                pred_cols.append(col.name)
                                pred_vals.append(matched_val)
                                already_bound.add(col.col_index)
                                bound = True
                                break
                        except Exception:
                            pass

        # Check if an explicitly requested filter entity could not be resolved in the table
        if explicit_requirements:
            any_bound = False
            for req in explicit_requirements:
                req_lower = req.lower()
                for pv in pred_vals:
                    if req_lower in str(pv).lower() or str(pv).lower() in req_lower:
                        any_bound = True
                        break
                if any_bound:
                    break
            if not any_bound:
                raise UnresolvedFilterError(
                    table_id=table_id,
                    filter_values=explicit_requirements,
                )

        # Execute queries for period 1 and period 2 (limit=2 to detect duplicate matches)
        try:
            rows_start = self.store.query_rows(table_id, workspace_id, predicates=predicates_start, limit=2, version=version)
            rows_end = self.store.query_rows(table_id, workspace_id, predicates=predicates_end, limit=2, version=version)
        except Exception as e:
            raise StorageFailureError(f"Failed to query rows: {e}") from e

        if not rows_start or not rows_end:
            raise UnresolvedFilterError(
                table_id=table_id,
                filter_values=pred_vals,
            )

        if len(rows_start) > 1 or len(rows_end) > 1:
            raise AmbiguousBindingError(
                f"Duplicate matches found for period comparison in table '{table_id}' "
                f"({len(rows_start)} rows for year {year_start}, {len(rows_end)} rows for year {year_end}). "
                f"Query requires additional filter predicates to resolve uniquely."
            )

        r1, r2 = rows_start[0], rows_end[0]
        v1_raw = r1.cells[val_idx].normalized_value
        v2_raw = r2.cells[val_idx].normalized_value

        v1 = Decimal(str(v1_raw).replace(",", "").strip())
        v2 = Decimal(str(v2_raw).replace(",", "").strip())

        delta = v2 - v1  # e.g. 976077 - 980268 = -4191
        abs_delta = abs(delta)

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

        # Zero-baseline convention:
        # If initial baseline v1 == 0, percentage change (v2 - v1)/v1 is mathematically undefined (division by zero / 0/0).
        # We return the absolute delta, set secondary_value (percentage_change) to None,
        # and provide an explicit explanation for 0→positive, 0→negative, and 0→0.
        if v1 == 0:
            pct_change: Decimal | None = None
            if delta > 0:
                answer_text = (
                    f"It increased by {abs_delta:,}{unit_label}, from 0 to {v2:,}. "
                    f"Percentage change is undefined because the initial baseline value is 0."
                )
            elif delta < 0:
                answer_text = (
                    f"It fell by {abs_delta:,}{unit_label}, from 0 to {v2:,}. "
                    f"Percentage change is undefined because the initial baseline value is 0."
                )
            else:
                # 0 -> 0 convention:
                # Value remained unchanged at 0 (change of 0). Percentage change is indeterminate (0/0)
                # and mathematically undefined from a zero baseline; returned as null with explicit explanation.
                answer_text = (
                    f"It remained unchanged at 0{unit_label} (change of 0). "
                    f"Percentage change is undefined because the initial baseline value is 0."
                )
        else:
            pct_change = round(abs_delta / abs(v1) * Decimal("100"), 2)
            if delta < 0:
                answer_text = (
                    f"It fell by {abs_delta:,}{unit_label}, from {v1:,} to {v2:,}, "
                    f"a decline of approximately {pct_change:.2f}%."
                )
            elif delta > 0:
                answer_text = (
                    f"It increased by {abs_delta:,}{unit_label}, from {v1:,} to {v2:,}, "
                    f"an increase of approximately {pct_change:.2f}%."
                )
            else:
                answer_text = (
                    f"It remained unchanged at {v1:,}{unit_label} (change of 0, 0.00% change)."
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
            secondary_value=pct_change,
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
