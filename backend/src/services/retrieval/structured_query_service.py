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

        # Filter by document_ids if provided and exclude tombstoned sources
        candidate_tables = []
        for tid in available_tables:
            ver = self.store.get_active_version(tid, workspace_id)
            t_meta = self.store.get_table(tid, workspace_id, version=ver)
            if t_meta:
                try:
                    from src.db.workspace_boundary_repo import get_workspace_boundary_repo
                    brepo = get_workspace_boundary_repo()
                    if brepo and (brepo.has_tombstone(workspace_id, "source", t_meta.document_id) or brepo.has_tombstone(workspace_id, "source", tid)):
                        continue
                except Exception:
                    pass
                if document_ids is None or t_meta.document_id in document_ids:
                    candidate_tables.append((tid, t_meta, ver))

        if not candidate_tables:
            logger.info("StructuredQueryService: no candidate tables match document scope %s", document_ids)
            return None

        # 2. Defensible table binding before execution
        bound_candidate = self._bind_table(candidate_tables, query, workspace_id)
        if not bound_candidate:
            return None

        selected_tid, selected_meta, selected_ver = bound_candidate

        # 3. Completeness check on bound table — require coverage_complete
        if not self.store.has_coverage_complete_table(selected_tid, workspace_id):
            manifest = self.store.get_ingestion_manifest(selected_meta.document_id, workspace_id, selected_ver)
            logger.error("StructuredQueryService: bound table %s is not coverage_complete", selected_tid)
            raise IncompleteDataError(selected_tid, manifest)

        schema = selected_meta.schema
        if not schema or not schema.columns:
            return None

        manifest = self.store.get_ingestion_manifest(selected_meta.document_id, workspace_id, selected_ver) or {}
        total_rows_in_table = int(manifest.get("persisted_rows", 0))

        # 4. Execute on bound table (never switches to another table)
        return self._execute_on_table(
            query=query,
            table_id=selected_tid,
            workspace_id=workspace_id,
            document_id=selected_meta.document_id,
            version=selected_ver,
            schema=schema,
            total_rows=total_rows_in_table,
        )

    def _bind_table(
        self,
        candidate_tables: list[tuple[str, Any, str]],
        query: str,
        workspace_id: str,
    ) -> tuple[str, Any, str] | None:
        q_lower = query.lower()

        # 1. Explicit table name or file in query
        candidate_tables_sorted = sorted(candidate_tables, key=lambda x: len(x[0]), reverse=True)
        for tid, meta, ver in candidate_tables_sorted:
            pattern = rf"(?:\b|_){re.escape(tid)}(?:\.csv|\.xlsx)?(?:\b|_)"
            if re.search(pattern, q_lower):
                return (tid, meta, ver)

        # 2. Single candidate in scope
        if len(candidate_tables) == 1:
            return candidate_tables[0]

        # 3. Multiple candidate tables: score and disambiguate
        scored = self._score_candidate_tables(candidate_tables, query, workspace_id)
        # Sort by score descending; break ties deterministically by table_id
        scored.sort(key=lambda x: (x[0], x[1][0]), reverse=True)

        if not scored:
            return None

        s1, top_item = scored[0]
        s2, second_item = scored[1]

        # Check for ambiguity: multiple tables remain plausibly compatible without sufficient disambiguation
        if s1 == s2 and s1 > 0:
            competing = [item[0] for s, item in scored if s == s1]
            raise AmbiguousBindingError(
                f"Ambiguous table binding: multiple equally compatible tables found in workspace ({', '.join(competing)}) "
                f"for query '{query[:60]}'. Please specify table name in query."
            )

        if s1 > 0 and s2 > 0 and (s1 - s2) < 2.0:
            raise AmbiguousBindingError(
                f"Ambiguous table binding: multiple compatible tables ({top_item[0]}, {second_item[0]}) "
                f"without sufficient disambiguation (scores {s1:.1f} vs {s2:.1f}). Please specify table name in query."
            )

        if s1 == 0:
            raise AmbiguousBindingError(
                f"Ambiguous table binding: multiple tables exist in workspace ({', '.join(t[0] for t in candidate_tables)}) "
                f"but none match query '{query[:60]}'."
            )

        return top_item

    def _score_candidate_tables(
        self,
        candidate_tables: list[tuple[str, Any, str]],
        query: str,
        workspace_id: str,
    ) -> list[tuple[float, tuple[str, Any, str]]]:
        q_lower = query.lower()
        q_words = [
            w for w in re.findall(r"\b[a-zA-Z0-9_]{2,}\b", q_lower)
            if w not in {
                "how", "did", "change", "from", "to", "what", "is", "the", "for", "give", "and", "in", "of", "between",
                "table", "csv", "xlsx", "rows", "records", "data"
            }
        ]
        explicit_terms: list[str] = []
        explicit_terms += re.findall(r"\(([^)]+)\)", query)
        explicit_terms += re.findall(r'["\']([^"\']+)["\']', query)
        for m in re.finditer(r"\bfor\s+([A-Za-z0-9_]+(?:\s+[A-Za-z0-9_]+)?)\b", query, re.IGNORECASE):
            explicit_terms.append(m.group(1).strip())
        explicit_terms = [t.strip() for t in explicit_terms if len(t.strip()) >= 2]

        scored: list[tuple[float, tuple[str, Any, str]]] = []
        for item in candidate_tables:
            tid, meta, ver = item
            score = 0.0
            schema = meta.schema if meta else None
            if not schema or not schema.columns:
                scored.append((score, item))
                continue

            for col in schema.columns:
                c_name = col.name.lower()
                for w in q_words:
                    if w in c_name or c_name in w:
                        score += 3.0
                    elif w.endswith("ies") and w[:-3] + "y" in c_name:
                        score += 3.0
                    elif w.endswith("s") and w[:-1] in c_name:
                        score += 3.0

                dtype = self._normalize_dtype(col.inferred_dtype)
                if dtype in {"decimal", "integer", "percentage", "currency"}:
                    score += 2.0

            if explicit_terms:
                try:
                    probe_rows = self.store.query_rows(tid, workspace_id, predicates=[], limit=50, version=ver)
                    for exp in explicit_terms:
                        exp_lower = exp.lower().strip()
                        found = False
                        for r in probe_rows:
                            for cell in r.cells:
                                val = str(cell.normalized_value or "").lower().strip()
                                if exp_lower == val or exp_lower in val:
                                    score += 10.0
                                    found = True
                                    break
                            if found:
                                break
                except Exception:
                    pass

            scored.append((score, item))

        return scored

    def _execute_on_table(
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

        # Check for Ambiguous Binding
        self._check_ambiguous_binding(query, schema)

        # 4. Pattern 1: Min and Max / Earliest and Latest
        if bool(re.search(r'\b(?:earliest|latest|min|max|minimum|maximum)\b', q_lower)):
            return self._execute_min_max_range(
                query=query,
                table_id=table_id,
                workspace_id=workspace_id,
                document_id=document_id,
                version=version,
                schema=schema,
                total_rows=total_rows,
            )

        # 5. Pattern 2: Entity Metric Delta / Change between two periods
        if bool(re.search(r'\b(?:how did .* change|change from \d{4} to \d{4})\b', q_lower)):
            return self._execute_metric_delta(
                query=query,
                table_id=table_id,
                workspace_id=workspace_id,
                document_id=document_id,
                version=version,
                schema=schema,
                total_rows=total_rows,
            )

        # 6. Pattern 3: Standard Aggregates (sum, avg, count)
        if bool(re.search(r'\b(?:total|sum|average|avg|count)\b', q_lower)):
            return self._execute_aggregate(
                query=query,
                table_id=table_id,
                workspace_id=workspace_id,
                document_id=document_id,
                version=version,
                schema=schema,
                total_rows=total_rows,
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
    @staticmethod
    def _normalize_dtype(dtype: Any) -> str:
        if isinstance(dtype, InferredDtype):
            return dtype.value.lower()
        if hasattr(dtype, "value"):
            return str(dtype.value).lower()
        return str(dtype).lower()

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

    def _find_target_metric_column(
        self,
        query: str,
        schema: TableSchema,
        exclude_col_indices: set[int] | None = None,
    ) -> tuple[int, str]:
        exclude = exclude_col_indices or set()
        q_lower = query.lower()

        # 1. Prioritize numeric columns whose name is mentioned in query
        for col in schema.columns:
            if col.col_index in exclude:
                continue
            dtype_str = self._normalize_dtype(col.inferred_dtype)
            if dtype_str in {"decimal", "integer", "percentage", "currency"}:
                if bool(re.search(rf"\b{re.escape(col.name.lower())}\b", q_lower)):
                    return col.col_index, col.name

        # 2. Any non-excluded column whose name is mentioned in query
        for col in schema.columns:
            if col.col_index in exclude:
                continue
            if bool(re.search(rf"\b{re.escape(col.name.lower())}\b", q_lower)):
                return col.col_index, col.name

        # 3. Numeric column matching typical metric names
        metric_candidates = [
            "value", "amount", "total", "sum", "count", "metric", "sales", "revenue", "income", "price"
        ]
        for cand in metric_candidates:
            cand_clean = cand.lower().strip()
            for col in schema.columns:
                if col.col_index in exclude:
                    continue
                if col.name.lower().strip() == cand_clean:
                    return col.col_index, col.name
        for cand in metric_candidates:
            cand_clean = cand.lower().strip()
            for col in schema.columns:
                if col.col_index in exclude:
                    continue
                if cand_clean in col.name.lower().strip():
                    return col.col_index, col.name

        # 4. Any numeric column
        for col in schema.columns:
            if col.col_index in exclude:
                continue
            dtype_str = self._normalize_dtype(col.inferred_dtype)
            if dtype_str in {"decimal", "integer", "percentage", "currency"}:
                return col.col_index, col.name

        # 5. Fallback to first non-excluded column
        for col in schema.columns:
            if col.col_index not in exclude:
                return col.col_index, col.name
        return 0, schema.columns[0].name

    def _extract_and_bind_predicates(
        self,
        query: str,
        table_id: str,
        workspace_id: str,
        version: str,
        schema: TableSchema,
        target_col_idx: int | None = None,
    ) -> tuple[list[Predicate], list[str], list[Any]]:
        predicates: list[Predicate] = []
        pred_cols: list[str] = []
        pred_vals: list[Any] = []
        already_bound: set[int] = set()
        if target_col_idx is not None:
            already_bound.add(target_col_idx)

        skip_words = {
            "from", "between", "table", "rows", "records", "year", "years", "month",
            "how", "did", "change", "what", "the", "and", "for", "all", "give",
            "amount", "percentage", "to", "in", "of", "on", "at", "by", "with",
            "sum", "total", "average", "avg", "count", "min", "max", "minimum", "maximum",
            "value", "is", "are", "was", "were", "where", "which", "present", "actually",
            "does", "do", "calculate", "compute", "find", "get", "show", "tell",
        }

        explicit_requirements: list[str] = []
        q_lower = query.lower()

        # 1. Direct column-value pair matching (e.g. "where Industry is tech", "Year = 2024", "for Industry Agriculture")
        for col in schema.columns:
            if col.col_index in already_bound:
                continue
            c_name_lower = col.name.lower()
            patterns = [
                rf"\b(?:where\s+)?{re.escape(c_name_lower)}\s*(?:is|=|==|:)\s*(?:['\"]([^'\"]+)['\"]|([A-Za-z0-9_.]+(?:\s+[A-Za-z0-9_.]+)*?))(?:\s+(?:\b(?:in|and|where|for|from|with)\b|\.csv|\.xlsx|\?|$)|$)",
                rf"\bfor\s+{re.escape(c_name_lower)}\s+(?:['\"]([^'\"]+)['\"]|([A-Za-z0-9_.]+(?:\s+[A-Za-z0-9_.]+)*?))(?:\s+(?:\b(?:in|and|where|for|from|with)\b|\.csv|\.xlsx|\?|$)|$)",
            ]
            for pat in patterns:
                m = re.search(pat, q_lower)
                if m:
                    raw_val = (m.group(1) or m.group(2) or "").strip().strip("\"' ")
                    if raw_val and raw_val.lower() not in skip_words:
                        dtype_str = self._normalize_dtype(col.inferred_dtype)
                        if dtype_str in {"integer", "decimal"} and raw_val.isdigit():
                            typed_val: Any = int(raw_val)
                        elif dtype_str == "decimal":
                            try:
                                typed_val = Decimal(raw_val)
                            except Exception:
                                typed_val = raw_val
                        else:
                            typed_val = raw_val
                        predicates.append(Predicate(column_index=col.col_index, op=PredicateOp.EQ, value=typed_val))
                        pred_cols.append(col.name)
                        pred_vals.append(typed_val)
                        already_bound.add(col.col_index)
                        break

        # 2. Year-based filter extraction
        years = sorted(list(set(int(y) for y in re.findall(r'\b(20\d{2}|19\d{2})\b', query))))
        if years:
            year_col = next(
                (c for c in schema.columns if c.name.lower() in ("year", "date", "period", "time") or "year" in c.name.lower()),
                None,
            )
            if year_col:
                if year_col.col_index not in already_bound:
                    dtype_str = self._normalize_dtype(year_col.inferred_dtype)
                    y_vals = [y if dtype_str in {"integer", "decimal"} else str(y) for y in years]
                    if len(y_vals) == 1:
                        predicates.append(Predicate(column_index=year_col.col_index, op=PredicateOp.EQ, value=y_vals[0]))
                        pred_cols.append(year_col.name)
                        pred_vals.append(y_vals[0])
                    else:
                        predicates.append(Predicate(column_index=year_col.col_index, op=PredicateOp.IN, value=set(y_vals)))
                        pred_cols.append(year_col.name)
                        pred_vals.append(y_vals)
                    already_bound.add(year_col.col_index)
            else:
                for y in years:
                    explicit_requirements.append(str(y))

        # 3. Explicit filter entity requirements from query keywords (e.g. "for <target>", quotes, parentheses)
        for m in re.finditer(r'\bfor\s+([A-Za-z0-9_]+(?:\s+[A-Za-z0-9_]+)?)\b', query, re.IGNORECASE):
            target = m.group(1).strip()
            tokens = target.split()
            if len(tokens) > 1 and tokens[-1].lower() in skip_words:
                target = " ".join(tokens[:-1]).strip()
            if len(tokens) > 1 and tokens[0].lower() in skip_words:
                target = " ".join(tokens[1:]).strip()
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

        where_matches = re.findall(r'\bwhere\s+([A-Za-z0-9_]+)\b', query, re.IGNORECASE)
        for wm in where_matches:
            wm_clean = wm.strip()
            if wm_clean.lower() not in skip_words and not re.fullmatch(r'20\d{2}|19\d{2}', wm_clean):
                col_found = any(c.name.lower() == wm_clean.lower() for c in schema.columns)
                if not col_found:
                    explicit_requirements.append(wm_clean)

        # 4. Probe-based candidate terms extraction
        candidates: list[str] = list(explicit_requirements)
        candidates += re.findall(r'\b([A-Z][a-z]+(?: [a-z]* ?[A-Z]?[a-z]+){1,3})\b', query)
        candidates += [
            w for w in re.findall(r'\b[A-Za-z0-9_]{2,}\b', query)
            if w.lower() not in skip_words
            and not re.fullmatch(r'20\d{2}|19\d{2}', w)
            and w.lower() != table_id.lower()
            and w.lower() not in (table_id.lower() + ".csv", table_id.lower() + ".xlsx")
            and not any(c.name.lower() == w.lower() for c in schema.columns)
        ]

        words = re.findall(r'\b[a-zA-Z0-9_]+\b', query)
        for i in range(len(words) - 1):
            w1, w2 = words[i], words[i + 1]
            if w1.lower() not in skip_words and w2.lower() not in skip_words:
                candidates.append(f"{w1} {w2}")
        for i in range(len(words) - 2):
            w1, w2, w3 = words[i], words[i + 1], words[i + 2]
            if w1.lower() not in skip_words and w3.lower() not in skip_words:
                candidates.append(f"{w1} {w2} {w3}")

        seen: set[str] = set()
        unique_candidates: list[str] = []
        for c in candidates:
            c = c.strip()
            if not c or c.lower() in skip_words or re.fullmatch(r'20\d{2}|19\d{2}', c):
                continue
            if c.lower() not in seen:
                seen.add(c.lower())
                unique_candidates.append(c)

        # 5. Bind candidate terms against table data (probe rows first, then store query)
        try:
            probe_rows = self.store.query_rows(
                table_id, workspace_id, predicates=[], limit=200, version=version
            )
        except Exception:
            probe_rows = []

        for cand in unique_candidates:
            cand_lower = cand.lower().strip()
            bound = False
            for col in schema.columns:
                if col.col_index in already_bound:
                    continue
                for row in probe_rows:
                    try:
                        cell_val = str(row.cells[col.col_index].normalized_value).strip()
                    except (IndexError, AttributeError):
                        continue
                    if cell_val.lower() == cand_lower or cell_val == cand:
                        typed_val = row.cells[col.col_index].normalized_value
                        predicates.append(Predicate(column_index=col.col_index, op=PredicateOp.EQ, value=typed_val))
                        pred_cols.append(col.name)
                        pred_vals.append(typed_val)
                        already_bound.add(col.col_index)
                        bound = True
                        break
                if bound:
                    break

            if not bound and explicit_requirements:
                is_explicit = any(cand_lower == req.lower() for req in explicit_requirements)
                is_table_name = (
                    cand_lower == table_id.lower()
                    or cand_lower in (f"{table_id.lower()}.csv", f"{table_id.lower()}.xlsx")
                )
                if is_explicit and not is_table_name:
                    for col in schema.columns:
                        if col.col_index in already_bound:
                            continue
                        try:
                            val_pred: Any = cand
                            dtype_str = self._normalize_dtype(col.inferred_dtype)
                            if dtype_str in {"integer", "decimal"} and cand.isdigit():
                                val_pred = int(cand)
                            matching = self.store.query_rows(
                                table_id,
                                workspace_id,
                                predicates=[Predicate(column_index=col.col_index, op=PredicateOp.EQ, value=val_pred)],
                                limit=1,
                                version=version,
                            )
                            if matching:
                                matched_val = matching[0].cells[col.col_index].normalized_value
                                predicates.append(Predicate(column_index=col.col_index, op=PredicateOp.EQ, value=matched_val))
                                pred_cols.append(col.name)
                                pred_vals.append(matched_val)
                                already_bound.add(col.col_index)
                                bound = True
                                break
                        except Exception:
                            pass

        # 6. Verify that all explicit filter requirements were resolved
        if explicit_requirements:
            unbound_requirements: list[str] = []
            for req in explicit_requirements:
                req_lower = req.lower()
                matched = any(
                    req_lower == str(pv).lower()
                    or req_lower in str(pv).lower()
                    or str(pv).lower() in req_lower
                    for pv in pred_vals
                )
                if not matched:
                    unbound_requirements.append(req)
            if unbound_requirements:
                raise UnresolvedFilterError(
                    table_id=table_id,
                    filter_values=unbound_requirements,
                )

        return predicates, pred_cols, pred_vals

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

        predicates, pred_cols, pred_vals = self._extract_and_bind_predicates(
            query=query,
            table_id=table_id,
            workspace_id=workspace_id,
            version=version,
            schema=schema,
            target_col_idx=target_col_idx,
        )

        try:
            min_val, count, min_ids, min_idx, digest_min = self.store.execute_single_pass_query(
                table_id=table_id,
                workspace_id=workspace_id,
                column_index=target_col_idx,
                op=AggregateOp.MIN,
                predicates=predicates,
                version=version,
            )
            max_val, _, max_ids, max_idx, digest_max = self.store.execute_single_pass_query(
                table_id=table_id,
                workspace_id=workspace_id,
                column_index=target_col_idx,
                op=AggregateOp.MAX,
                predicates=predicates,
                version=version,
            )
        except Exception as e:
            raise StorageFailureError(f"Failed to query table store: {e}") from e

        if min_val is None or max_val is None:
            if count == 0:
                answer_text = f"No rows matched the requested filters in table '{table_id}'."
                combined_ids = []
                combined_idx = []
                selection_hash = digest_min
                int_min = 0
                int_max = 0
            else:
                raise StorageFailureError(f"No valid numeric data found in column '{target_col_name}'")
        else:
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
            predicate_columns=pred_cols,
            predicate_values=pred_vals,
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
            dtype_str = self._normalize_dtype(col.inferred_dtype)
            if dtype_str in {"decimal", "integer", "percentage", "currency"}:
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
                            dtype_str = self._normalize_dtype(col.inferred_dtype)
                            if dtype_str in {"integer", "decimal"} and cand.isdigit():
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
        if bool(re.search(r'\bcount\b', q_lower)):
            op = AggregateOp.COUNT
        elif bool(re.search(r'\b(?:sum|total)\b', q_lower)):
            op = AggregateOp.SUM
        elif bool(re.search(r'\b(?:average|avg)\b', q_lower)):
            op = AggregateOp.AVG
        elif bool(re.search(r'\b(?:min|minimum)\b', q_lower)):
            op = AggregateOp.MIN
        elif bool(re.search(r'\b(?:max|maximum)\b', q_lower)):
            op = AggregateOp.MAX
        else:
            op = AggregateOp.SUM

        # Temporal columns excluded from target metric selection to prevent aggregating year/date values
        exclude_metric_cols = {
            c.col_index for c in schema.columns
            if c.name.lower() in ("year", "date", "period", "time") or "year" in c.name.lower()
        }

        col_idx, col_name = self._find_target_metric_column(
            query=query,
            schema=schema,
            exclude_col_indices=exclude_metric_cols,
        )

        predicates, pred_cols, pred_vals = self._extract_and_bind_predicates(
            query=query,
            table_id=table_id,
            workspace_id=workspace_id,
            version=version,
            schema=schema,
            target_col_idx=col_idx,
        )

        val, count, r_ids, r_idx, digest = self.store.execute_single_pass_query(
            table_id=table_id,
            workspace_id=workspace_id,
            column_index=col_idx,
            op=op,
            predicates=predicates,
            version=version,
        )

        res_val = val if val is not None else Decimal(0)
        if count == 0:
            answer_text = f"The {op.value} for {col_name} is 0 (0 matching rows)."
        else:
            answer_text = f"The {op.value} for {col_name} is {res_val}."

        return StructuredQueryResult(
            answer_text=answer_text,
            operator=op.value,
            target_column=col_name,
            predicate_columns=pred_cols,
            predicate_values=pred_vals,
            selection_count=count,
            total_rows_in_table=total_rows,
            result_value=res_val,
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
