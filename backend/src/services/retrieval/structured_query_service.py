"""Structured query execution service over complete persisted tabular data.

Resolves deterministic queries (aggregates, min/max ranges, temporal metric deltas)
against TableStore in a single streaming pass without LLM generative calls.
"""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import logging
import re
import time
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
    result_value: Decimal | int | str | None
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
    stage_timings: dict[str, float] | None = None


class _TimedStoreProxy:
    """Lightweight TableStore wrapper that accumulates row read and aggregate timings."""

    def __init__(self, store: TableStore) -> None:
        self._store = store
        self.row_reads_ms: float = 0.0
        self.aggregate_ms: float = 0.0

    def query_rows(self, *args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return self._store.query_rows(*args, **kwargs)
        finally:
            self.row_reads_ms += (time.perf_counter() - t0) * 1000.0

    def aggregate(self, *args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return self._store.aggregate(*args, **kwargs)
        finally:
            self.aggregate_ms += (time.perf_counter() - t0) * 1000.0

    def execute_single_pass_query(self, *args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return self._store.execute_single_pass_query(*args, **kwargs)
        finally:
            self.aggregate_ms += (time.perf_counter() - t0) * 1000.0

    def stream_rows(self, *args: Any, **kwargs: Any) -> Any:
        it = iter(self._store.stream_rows(*args, **kwargs))
        while True:
            t0 = time.perf_counter()
            try:
                item = next(it)
            except StopIteration:
                self.row_reads_ms += (time.perf_counter() - t0) * 1000.0
                return
            except Exception:
                self.row_reads_ms += (time.perf_counter() - t0) * 1000.0
                raise
            self.row_reads_ms += (time.perf_counter() - t0) * 1000.0
            yield item

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


class StructuredExecutionError(Exception):
    """Base for all structured execution failures."""


class UnsupportedQueryError(StructuredExecutionError):
    """Query cannot be resolved to any supported operator."""


class AmbiguousBindingError(StructuredExecutionError):
    """Two or more columns match with similar confidence; cannot resolve safely."""


class UnresolvedFilterError(StructuredExecutionError):
    """A required filter entity was not found in the table."""

    def __init__(self, table_id: str, filter_values: list[Any], was_truncated: bool = False) -> None:
        msg = f"Required filter(s) {filter_values} not found in table '{table_id}'"
        if was_truncated:
            msg += " (scan was truncated at limit)"
        super().__init__(msg)
        self.table_id = table_id
        self.filter_values = filter_values
        self.was_truncated = was_truncated


class IncompleteDataError(StructuredExecutionError):
    """Table exists but coverage_complete=False; aggregate is invalid."""

    def __init__(self, table_id: str, manifest: dict[str, Any] | None) -> None:
        super().__init__(f"Table '{table_id}' data coverage is incomplete")
        self.table_id = table_id
        self.manifest = manifest or {}


class StorageFailureError(StructuredExecutionError):
    """DynamoDB or store raised an error."""


SKIP_FILTER_WORDS: set[str] = {
    "from", "between", "table", "rows", "records", "year", "years", "month",
    "how", "did", "change", "what", "the", "and", "for", "all", "give",
    "amount", "percentage", "to", "in", "of", "on", "at", "by", "with",
    "sum", "total", "average", "avg", "count", "min", "max", "minimum", "maximum",
    "value", "is", "are", "was", "were", "where", "which", "present", "actually",
    "does", "do", "calculate", "compute", "find", "get", "show", "tell",
}


class StructuredQueryService:
    def __init__(self, store: TableStore, boundary_repo: Any | None = None) -> None:
        self.store = store
        self._boundary_repo = boundary_repo
        self.last_stage_timings: dict[str, float] = {}

    def _get_boundary_repo(self) -> Any | None:
        if self._boundary_repo is not None:
            return self._boundary_repo
        try:
            from src.db.workspace_boundary_repo import get_workspace_boundary_repo
            return get_workspace_boundary_repo()
        except Exception:
            try:
                from db.workspace_boundary_repo import get_workspace_boundary_repo
                return get_workspace_boundary_repo()
            except Exception:
                return None

    def execute(
        self,
        query: str,
        workspace_id: str,
        document_ids: list[str] | None = None,
    ) -> StructuredQueryResult | None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")

        self.last_stage_timings = {}
        if hasattr(self.store, "reset_query_telemetry"):
            self.store.reset_query_telemetry()
        t_lookup_start = time.perf_counter()
        try:
            # 1. Discover authorised candidate tables in workspace
            try:
                available_tables = self.store.list_tables(workspace_id)
            except Exception as e:
                raise StorageFailureError(f"Failed to list tables for workspace {workspace_id}: {e}") from e

            if not available_tables:
                logger.info("StructuredQueryService: no tables found in workspace %s", workspace_id)
                return None

            # Filter by document_ids if provided and exclude tombstoned/stale sources
            candidate_tables = []
            brepo = self._get_boundary_repo()

            if brepo and brepo.has_tombstone(workspace_id, "workspace", workspace_id):
                logger.info("StructuredQueryService: workspace %s is tombstoned", workspace_id)
                return None

            for tid in available_tables:
                ver = self.store.get_active_version(tid, workspace_id)
                t_meta = self.store.get_table(tid, workspace_id, version=ver)
                if t_meta:
                    if brepo:
                        try:
                            if brepo.has_tombstone(workspace_id, "source", t_meta.document_id) or brepo.has_tombstone(workspace_id, "source", tid):
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
        finally:
            table_lookup_ms = (time.perf_counter() - t_lookup_start) * 1000.0
            self.last_stage_timings["table_lookup_ms"] = round(table_lookup_ms, 3)
            ddb_tel = getattr(self.store, "last_query_telemetry", None)
            if isinstance(ddb_tel, dict):
                self.last_stage_timings.update(ddb_tel)

        # 4. Execute on bound table (never switches to another table)
        t_exec_start = time.perf_counter()
        timed_store = _TimedStoreProxy(self.store)
        orig_store = self.store
        self.store = timed_store
        result: StructuredQueryResult | None = None
        try:
            result = self._execute_on_table(
                query=query,
                table_id=selected_tid,
                workspace_id=workspace_id,
                document_id=selected_meta.document_id,
                version=selected_ver,
                schema=schema,
                total_rows=total_rows_in_table,
            )
        finally:
            self.store = orig_store
            exec_total_ms = (time.perf_counter() - t_exec_start) * 1000.0
            row_reads_ms = timed_store.row_reads_ms
            aggregate_ms = timed_store.aggregate_ms
            computation_ms = max(0.0, exec_total_ms - row_reads_ms - aggregate_ms)
            self.last_stage_timings.update({
                "row_reads_ms": round(row_reads_ms, 3),
                "aggregate_ms": round(aggregate_ms, 3),
                "computation_ms": round(computation_ms, 3),
            })
            ddb_tel = getattr(orig_store, "last_query_telemetry", None)
            if isinstance(ddb_tel, dict):
                self.last_stage_timings.update(ddb_tel)
            if result is not None:
                result.stage_timings = dict(self.last_stage_timings)
        return result

    def _candidate_source_aliases(self, tid: str, meta: Any, ver: str, workspace_id: str) -> list[str]:
        aliases = [tid]
        sheet_name = getattr(meta, "sheet_name", None) if meta else None
        if sheet_name:
            aliases.append(str(sheet_name))
        try:
            brepo = self._get_boundary_repo()
            doc_id = getattr(meta, "document_id", None) if meta else None
            if brepo and doc_id:
                snap = brepo.get_document_snapshot(workspace_id, doc_id)
                if snap and getattr(snap, "source", None) and (not snap.version or not ver or str(snap.version) == str(ver)):
                    src = str(snap.source)
                    aliases.append(src)
                    stem = re.sub(r"\.(?:csv|xlsx?|tsv)$", "", src, flags=re.IGNORECASE)
                    if stem:
                        aliases.append(stem)
                        aliases.append(stem.replace("_", " ").replace("-", " "))
        except Exception:
            pass
        return [a.lower().strip() for a in aliases if a and a.strip()]

    def _bind_table(
        self,
        candidate_tables: list[tuple[str, Any, str]],
        query: str,
        workspace_id: str,
    ) -> tuple[str, Any, str] | None:
        q_lower = query.lower()

        # 1. Explicit table name or trusted active source filename/stem in query
        candidate_tables_sorted = sorted(candidate_tables, key=lambda x: len(x[0]), reverse=True)
        explicit_matches: list[tuple[str, Any, str]] = []
        alias_map: dict[str, list[str]] = {}
        for tid, meta, ver in candidate_tables_sorted:
            aliases = self._candidate_source_aliases(tid, meta, ver, workspace_id)
            alias_map[tid] = aliases
            matched = False
            for alias in sorted(aliases, key=len, reverse=True):
                pattern = rf"(?:\b|_){re.escape(alias)}(?:\.csv|\.xlsx?)?(?:\b|_)"
                if re.search(pattern, q_lower):
                    matched = True
                    break
            if matched:
                explicit_matches.append((tid, meta, ver))

        # 1b. If query names a specific source/file that did not resolve to any active table, never guess by shared columns
        # (Must run before returning a single explicit_match so known + missing multi-source queries fail safely.)
        generic_source_words = {
            "the", "this", "that", "uploaded", "attached", "selected", "current",
            "same", "given", "provided", "available", "each", "all", "any", "a", "an",
            "valid", "active", "complete", "full", "entire", "main", "primary",
            "csv", "xlsx", "xls", "tsv", "workbook", "spreadsheet", "dataset", "table", "file",
        }
        explicit_refs: list[str] = []
        explicit_refs.extend(re.findall(r"\b([a-z0-9_.\-]+\.(?:csv|xlsx?|tsv))\b", q_lower))
        for m in re.finditer(
            r"\b(?:in|from|across|between|using|and)\s+(?:the\s+)?([a-z0-9_\-]+(?:\s+(?!to\b|in\b|from\b|and\b|or\b)[a-z0-9_\-]+){0,2})\s+(?:csv|xlsx?|tsv|workbook|spreadsheet|dataset|table)\b",
            q_lower,
        ):
            explicit_refs.append(m.group(1).strip())

        for ref in explicit_refs:
            ref_tokens = [
                t for t in re.findall(r"[a-z0-9]{2,}", ref)
                if t not in generic_source_words
            ]
            if not ref_tokens:
                continue
            ref_matched = any(
                all(any(tok in al for al in alias_map.get(tid, [])) for tok in ref_tokens)
                for tid, _, _ in candidate_tables_sorted
            )
            if not ref_matched:
                raise AmbiguousBindingError(
                    f"Explicit source reference '{ref}' could not be resolved to an active table in workspace '{workspace_id}'."
                )

        if len(explicit_matches) == 1:
            return explicit_matches[0]
        if len(explicit_matches) > 1:
            competing = [item[0] for item in explicit_matches]
            raise AmbiguousBindingError(
                f"Ambiguous table binding: multiple active tables match explicit source reference ({', '.join(competing)}) "
                f"for query '{query[:60]}'."
            )

        # Document table indicators: references to numbered tables/figures/charts in narrative papers/reports
        is_tabular_explicit = bool(
            re.search(r'\.(?:csv|xlsx?|tsv)\b', q_lower)
            or re.search(r'\b(?:spreadsheet|survey|dataset|workbook|table|tabular|sheet)\b', q_lower)
        )
        is_doc_ref = bool(
            re.search(r'\b(?:table|figure|fig\.|chart)\s+\d+\b', q_lower)
            or re.search(r'\b(?:in the paper|in this paper|in the article|in this article|in the study|in the pdf)\b', q_lower)
        )
        is_internal_doc_table = is_doc_ref and not is_tabular_explicit

        # If query is targeting an internal document table/figure and does not name a spreadsheet,
        # never bind a tabular store table.
        if is_internal_doc_table:
            return None

        # 2. Single candidate in scope
        if len(candidate_tables) == 1:
            scored_single = self._score_candidate_tables(candidate_tables, query, workspace_id)
            if scored_single and scored_single[0][0] > 0:
                return candidate_tables[0]
            # If the single candidate has zero match to the query, do not bind it
            return None

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

            if tid.lower() in q_lower:
                score += 15.0

            has_temporal = False
            has_metric = False

            for col in schema.columns:
                c_name = col.name.lower()
                dtype = self._normalize_dtype(col.inferred_dtype)
                if c_name in ("year", "date", "period", "time") or "year" in c_name:
                    has_temporal = True
                elif dtype in {"decimal", "integer", "percentage", "currency"}:
                    has_metric = True

                for w in q_words:
                    if w in c_name or c_name in w:
                        score += 3.0
                    elif w.endswith("ies") and w[:-3] + "y" in c_name:
                        score += 3.0
                    elif w.endswith("s") and w[:-1] in c_name:
                        score += 3.0

            if has_temporal:
                score += 1.0
            is_count_query = bool(re.search(r"\bcount\b|\bhow many (?:rows|records|entries|items)\b", q_lower))
            if has_metric and not is_count_query:
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

        # 5b. Pattern 2b: Binary Formula / Premise Verification (e.g. "A minus B equals C")
        if bool(re.search(r'\b(?:minus|plus)\b', q_lower)):
            return self._execute_formula_verification(
                query=query,
                table_id=table_id,
                workspace_id=workspace_id,
                document_id=document_id,
                version=version,
                schema=schema,
                total_rows=total_rows,
            )

        if bool(re.search(r',\s*right\??', q_lower)) or "check against" in q_lower:
            raise UnsupportedQueryError(f"Premise verification query '{query[:60]}' cannot be answered by a blind aggregate")

        # 6. Pattern 3: Standard Aggregates (sum, avg, count)
        if bool(re.search(r'\b(?:total|sum|average|avg|count)\b|\bhow many (?:rows|records|entries|items)\b', q_lower)):
            return self._execute_aggregate(
                query=query,
                table_id=table_id,
                workspace_id=workspace_id,
                document_id=document_id,
                version=version,
                schema=schema,
                total_rows=total_rows,
            )

        # 7. Pattern 4: Column Ratio / Percentage (e.g. "percentage of <denom> ... <num>")
        if bool(re.search(r'\b(?:percentage|percent|ratio)\b', q_lower)):
            return self._execute_column_ratio(
                query=query,
                table_id=table_id,
                workspace_id=workspace_id,
                document_id=document_id,
                version=version,
                schema=schema,
                total_rows=total_rows,
            )

        # 7b. Pattern 4b: Exact Single-Row / Filtered Metric Lookup (e.g. "how many ... vacant in <entity>")
        if bool(re.search(r'\b(?:how many|how much|what is|what are|find|get|give)\b', q_lower)):
            filter_cands = self._extract_explicit_filter_requirements(query, table_id=table_id, schema=schema)
            if filter_cands:
                exclude_metric_cols = {
                    c.col_index for c in schema.columns
                    if c.name.lower() in ("year", "date", "period", "time") or "year" in c.name.lower()
                }
                try:
                    self._find_target_metric_column(
                        query=query,
                        schema=schema,
                        exclude_col_indices=exclude_metric_cols,
                    )
                    return self._execute_aggregate(
                        query=query,
                        table_id=table_id,
                        workspace_id=workspace_id,
                        document_id=document_id,
                        version=version,
                        schema=schema,
                        total_rows=total_rows,
                        is_lookup=True,
                    )
                except UnsupportedQueryError:
                    pass

        # 8. Pattern 5: Ambiguous "how many <metric>" without filter or all-rows qualifier
        if bool(re.search(r'\bhow many\b', q_lower)) and not bool(
            re.search(r'\b(?:total|sum|all|across|every|combined|overall|altogether)\b', q_lower)
        ):
            self._raise_if_ambiguous_scope(
                query=query,
                table_id=table_id,
                workspace_id=workspace_id,
                version=version,
                schema=schema,
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

    def _extract_explicit_filter_requirements(
        self,
        query: str,
        table_id: str | None = None,
        schema: TableSchema | None = None,
    ) -> list[str]:
        explicit_requirements: list[str] = []
        table_name = table_id.lower() if table_id else ""
        table_csv = f"{table_name}.csv" if table_name else ""
        table_xlsx = f"{table_name}.xlsx" if table_name else ""

        # 1. "for <target>"
        for m in re.finditer(r'\bfor\s+([A-Za-z0-9_]+(?:\s+[A-Za-z0-9_]+)?)\b', query, re.IGNORECASE):
            target = m.group(1).strip()
            tokens = target.split()
            if len(tokens) > 1 and tokens[0].lower() in {"the", "a", "an", "this", "that"}:
                target = " ".join(tokens[1:]).strip()
            if len(tokens) > 1 and tokens[-1].lower() in SKIP_FILTER_WORDS:
                target = " ".join(tokens[:-1]).strip()
            t_lower = target.lower().strip()
            if t_lower and t_lower not in SKIP_FILTER_WORDS and not re.fullmatch(r'20\d{2}|19\d{2}', target):
                if not table_name or (t_lower != table_name and t_lower not in (table_csv, table_xlsx)):
                    explicit_requirements.append(target.strip())

        # 1b. "in <Entity>" trailing entity phrase (e.g., "vacant in SIDDHA?", "in NATURE CURE & YOGA?")
        for m_in in re.finditer(r'\bin\s+([A-Z][A-Za-z0-9& -]{2,})\s*(?:\?|$)', query):
            t_in = m_in.group(1).strip()
            t_in_low = t_in.lower()
            if t_in_low not in SKIP_FILTER_WORDS and not re.fullmatch(r'20\d{2}|19\d{2}', t_in):
                if not table_name or (t_in_low != table_name and t_in_low not in (table_csv, table_xlsx)):
                    explicit_requirements.append(t_in)

        # 2. Quoted strings
        for q_str in re.findall(r'["\']([^"\']+)["\']', query):
            q_clean = q_str.strip()
            q_lower = q_clean.lower()
            if q_lower not in SKIP_FILTER_WORDS and not re.fullmatch(r'20\d{2}|19\d{2}', q_clean):
                if not table_name or (q_lower != table_name and q_lower not in (table_csv, table_xlsx)):
                    explicit_requirements.append(q_clean)

        # 3. Parenthesized terms
        for p_str in re.findall(r'\(([^)]+)\)', query):
            p_clean = p_str.strip()
            p_lower = p_clean.lower()
            if p_lower not in SKIP_FILTER_WORDS and not re.fullmatch(r'20\d{2}|19\d{2}', p_clean):
                if not table_name or (p_lower != table_name and p_lower not in (table_csv, table_xlsx)):
                    explicit_requirements.append(p_clean)

        # 4. "where <target>" if target is not a known schema column name
        if schema:
            where_matches = re.findall(r'\bwhere\s+([A-Za-z0-9_]+)\b', query, re.IGNORECASE)
            for wm in where_matches:
                wm_clean = wm.strip()
                wm_lower = wm_clean.lower()
                if wm_lower not in SKIP_FILTER_WORDS and not re.fullmatch(r'20\d{2}|19\d{2}', wm_clean):
                    col_found = any(c.name.lower() == wm_lower for c in schema.columns)
                    if not col_found:
                        if not table_name or (wm_lower != table_name and wm_lower not in (table_csv, table_xlsx)):
                            explicit_requirements.append(wm_clean)

        seen: set[str] = set()
        unique_reqs: list[str] = []
        for req in explicit_requirements:
            if req.lower() not in seen:
                seen.add(req.lower())
                unique_reqs.append(req)
        return unique_reqs

    def _extract_candidate_filter_terms(
        self,
        query: str,
        table_id: str | None = None,
        schema: TableSchema | None = None,
    ) -> list[str]:
        candidates: list[str] = []
        table_name = table_id.lower() if table_id else ""
        table_csv = f"{table_name}.csv" if table_name else ""
        table_xlsx = f"{table_name}.xlsx" if table_name else ""

        # 1. Phrases between intent verbs
        candidates += re.findall(
            r'(?:how did|what did|change in|measure of|value of)\s+([a-zA-Z0-9_ ]+?)\s+(?:change|from|between)',
            query,
            re.IGNORECASE,
        )

        # 2. Capitalized multi-word noun phrases
        candidates += re.findall(r'\b([A-Z][a-z]+(?: [a-z]* ?[A-Z]?[a-z]+){1,3})\b', query)

        # 3. Single identifier tokens (capitalized, alphanumeric, or leading-zero codes)
        candidates += [
            w for w in re.findall(r'\b[A-Za-z0-9_]{2,}\b', query)
            if w.lower() not in SKIP_FILTER_WORDS
            and not re.fullmatch(r'20\d{2}|19\d{2}', w)
            and (not table_name or (w.lower() != table_name and w.lower() not in (table_csv, table_xlsx)))
            and (not schema or not any(c.name.lower() == w.lower() for c in schema.columns))
        ]

        # 4. Non-stopword bi-grams and tri-grams
        words = re.findall(r'\b[a-zA-Z0-9_]+\b', query)
        for i in range(len(words) - 1):
            w1, w2 = words[i], words[i + 1]
            if w1.lower() not in SKIP_FILTER_WORDS and w2.lower() not in SKIP_FILTER_WORDS:
                candidates.append(f"{w1} {w2}")
        for i in range(len(words) - 2):
            w1, w2, w3 = words[i], words[i + 1], words[i + 2]
            if w1.lower() not in SKIP_FILTER_WORDS and w3.lower() not in SKIP_FILTER_WORDS:
                candidates.append(f"{w1} {w2} {w3}")

        # Deduplicate, longer phrases first
        seen: set[str] = set()
        filtered_candidates: list[str] = []
        for c in sorted(candidates, key=lambda x: -len(x)):
            c_clean = c.strip()
            c_lower = c_clean.lower()
            if not c_clean or c_lower in SKIP_FILTER_WORDS:
                continue
            if re.fullmatch(r'20\d{2}|19\d{2}', c_clean):
                continue
            if table_name and (c_lower == table_name or c_lower in (table_csv, table_xlsx)):
                continue
            if schema and any(col.name.lower() == c_lower for col in schema.columns):
                continue
            if c_lower not in seen:
                seen.add(c_lower)
                filtered_candidates.append(c_clean)

        return filtered_candidates

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
                    if raw_val and raw_val.lower() not in SKIP_FILTER_WORDS:
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
        explicit_requirements.extend(
            self._extract_explicit_filter_requirements(query, table_id=table_id, schema=schema)
        )

        # 4. Probe-based candidate terms extraction
        candidate_terms = self._extract_candidate_filter_terms(query, table_id=table_id, schema=schema)
        seen_cand: set[str] = set()
        unique_candidates: list[str] = []
        for c in list(explicit_requirements) + candidate_terms:
            if c.lower() not in seen_cand:
                seen_cand.add(c.lower())
                unique_candidates.append(c)

        # 5. Bind candidate terms against table data (probe rows first, then store query)
        try:
            probe_rows = self.store.query_rows(
                table_id, workspace_id, predicates=[], limit=500, version=version
            )
        except Exception:
            probe_rows = []

        truncated_cands: set[str] = set()

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
                        dtype_str = self._normalize_dtype(col.inferred_dtype)
                        is_numeric = dtype_str in {"integer", "decimal"}
                        val_pred: Any = cand
                        if is_numeric:
                            clean_cand = cand.replace(",", "").strip()
                            if clean_cand.lstrip("+-").isdigit():
                                val_pred = int(clean_cand)
                            else:
                                try:
                                    val_pred = Decimal(clean_cand)
                                except Exception:
                                    continue
                        try:
                            matching = self.store.query_rows(
                                table_id,
                                workspace_id,
                                predicates=[Predicate(column_index=col.col_index, op=PredicateOp.EQ, value=val_pred)],
                                limit=1,
                                version=version,
                                max_scan_items=500,
                            )
                            if matching:
                                matched_val = matching[0].cells[col.col_index].normalized_value
                                predicates.append(Predicate(column_index=col.col_index, op=PredicateOp.EQ, value=matched_val))
                                pred_cols.append(col.name)
                                pred_vals.append(matched_val)
                                already_bound.add(col.col_index)
                                bound = True
                                break
                            elif getattr(matching, "is_truncated", False):
                                truncated_cands.add(cand)
                        except Exception:
                            pass

        # 6. Verify that all explicit filter requirements were resolved
        if explicit_requirements:
            unbound_requirements: list[str] = []
            has_truncated = False
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
                    if req in truncated_cands or any(req.lower() == tc.lower() for tc in truncated_cands):
                        has_truncated = True
            if unbound_requirements:
                raise UnresolvedFilterError(
                    table_id=table_id,
                    filter_values=unbound_requirements,
                    was_truncated=has_truncated,
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
            c_name = col.name.lower()
            c_name_spaces = c_name.replace("_", " ")
            if (
                bool(re.search(rf"\b{re.escape(c_name)}\b", q_lower))
                or bool(re.search(rf"\b{re.escape(c_name_spaces)}\b", q_lower))
                or any(
                    bool(re.search(rf"\b{re.escape(part)}\b", q_lower))
                    for part in c_name.split("_")
                    if len(part) > 2 and part not in ("total", "sum", "avg", "min", "max", "all")
                )
            ):
                mentioned_col = (col.col_index, col.name)
                break

        if mentioned_col:
            target_col_idx, target_col_name = mentioned_col
        elif any(w in q_lower for w in ("earliest", "latest", "year", "date", "period")):
            target_col_idx, target_col_name = self._find_column_by_semantic_match(
                ["year", "date", "period", "time"], schema
            )
        else:
            target_col_idx, target_col_name = self._find_target_metric_column(query, schema)

        predicates, pred_cols, pred_vals = self._extract_and_bind_predicates(
            query=query,
            table_id=table_id,
            workspace_id=workspace_id,
            version=version,
            schema=schema,
            target_col_idx=target_col_idx,
        )

        try:
            rows = self.store.query_rows(
                table_id=table_id,
                workspace_id=workspace_id,
                predicates=predicates,
                limit=None,
                version=version,
            )
        except Exception as e:
            raise StorageFailureError(f"Failed to query table store: {e}") from e

        if getattr(rows, "is_truncated", False):
            raise StorageFailureError(f"Incomplete table read during range query on '{table_id}'")

        count = len(rows)
        min_val: Decimal | None = None
        max_val: Decimal | None = None
        combined_ids = sorted({r.row_id for r in rows})
        combined_idx = sorted({r.row_index for r in rows})
        selection_hash = hashlib.sha256(",".join(combined_ids).encode()).hexdigest()[:16]

        for r in rows:
            if target_col_idx < len(r.cells):
                val = r.cells[target_col_idx].normalized_value
                if val is not None:
                    d_val: Decimal | None = None
                    if isinstance(val, Decimal):
                        d_val = val
                    else:
                        try:
                            d_val = Decimal(str(val).replace(",", "").strip())
                        except Exception:
                            d_val = None
                    if d_val is not None:
                        if min_val is None or d_val < min_val:
                            min_val = d_val
                        if max_val is None or d_val > max_val:
                            max_val = d_val

        if min_val is None or max_val is None:
            if count == 0:
                answer_text = f"No rows matched the requested filters in table '{table_id}' (0 matching rows). Minimum and maximum are undefined."
                combined_ids = []
                combined_idx = []
                int_min = None
                int_max = None
            else:
                raise StorageFailureError(f"No valid numeric data found in column '{target_col_name}'")
        else:
            int_min = int(min_val) if min_val == int(min_val) else min_val
            int_max = int(max_val) if max_val == int(max_val) else max_val
            answer_text = f"{int_min} and {int_max}."

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

        # Resolve target metric column using word boundary matching and semantic fallback
        val_idx, val_col_name = self._find_target_metric_column(
            query=query,
            schema=schema,
            exclude_col_indices={year_idx},
        )

        # Year-based predicates (always required)
        predicates_start: list[Predicate] = [Predicate(column_index=year_idx, op=PredicateOp.EQ, value=year_start)]
        predicates_end: list[Predicate] = [Predicate(column_index=year_idx, op=PredicateOp.EQ, value=year_end)]
        pred_cols = ["Year"]
        pred_vals = [f"{year_start},{year_end}"]

        # Extract explicit filter requirements and candidate filter terms
        explicit_requirements = self._extract_explicit_filter_requirements(query, table_id=table_id, schema=schema)
        candidate_terms = self._extract_candidate_filter_terms(query, table_id=table_id, schema=schema)
        seen_cand: set[str] = set()
        unique_candidates: list[str] = []
        for c in list(explicit_requirements) + candidate_terms:
            if c.lower() not in seen_cand:
                seen_cand.add(c.lower())
                unique_candidates.append(c)

        # Check probe rows first (fast in-memory check)
        try:
            probe_rows = self.store.query_rows(
                table_id, workspace_id, predicates=[], limit=500, version=version
            )
        except Exception:
            probe_rows = []

        already_bound: set[int] = {year_idx, val_idx}
        truncated_cands_delta: set[str] = set()

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

            # If not found in first probe rows, query store directly with predicate
            # (supports categorical/entity values located outside the first 500 rows).
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
                        dtype_str = self._normalize_dtype(col.inferred_dtype)
                        is_numeric = dtype_str in {"integer", "decimal"}
                        val_pred: Any = cand
                        if is_numeric:
                            clean_cand = cand.replace(",", "").strip()
                            if clean_cand.lstrip("+-").isdigit():
                                val_pred = int(clean_cand)
                            else:
                                try:
                                    val_pred = Decimal(clean_cand)
                                except Exception:
                                    continue
                        try:
                            matching = self.store.query_rows(
                                table_id,
                                workspace_id,
                                predicates=[Predicate(column_index=col.col_index, op=PredicateOp.EQ, value=val_pred)],
                                limit=1,
                                version=version,
                                max_scan_items=500,
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
                            elif getattr(matching, "is_truncated", False):
                                truncated_cands_delta.add(cand)
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
                has_truncated = any(
                    req in truncated_cands_delta or any(req.lower() == tc.lower() for tc in truncated_cands_delta)
                    for req in explicit_requirements
                )
                raise UnresolvedFilterError(
                    table_id=table_id,
                    filter_values=explicit_requirements,
                    was_truncated=has_truncated,
                )

        # Execute a single traversal using shared non-year predicates, then partition by year_start / year_end
        pred_year_start = predicates_start[0]
        pred_year_end = predicates_end[0]
        shared_predicates = predicates_start[1:]
        try:
            all_rows = self.store.query_rows(
                table_id,
                workspace_id,
                predicates=shared_predicates,
                limit=None,
                version=version,
            )
        except Exception as e:
            raise StorageFailureError(f"Failed to query rows: {e}") from e

        if getattr(all_rows, "is_truncated", False):
            raise StorageFailureError(f"Incomplete table read during metric delta query on '{table_id}'")

        def _row_matches_year(row: Any, col_idx: int, target_val: Any) -> bool:
            if col_idx < 0 or col_idx >= len(row.cells):
                return False
            val = row.cells[col_idx].normalized_value
            if val is None:
                return False
            if val == target_val:
                return True
            try:
                return int(str(val).replace(",", "").strip()) == int(str(target_val).replace(",", "").strip())
            except Exception:
                return str(val).strip() == str(target_val).strip()

        rows_start = [r for r in all_rows if _row_matches_year(r, pred_year_start.column_index, pred_year_start.value)]
        rows_end = [r for r in all_rows if _row_matches_year(r, pred_year_end.column_index, pred_year_end.value)]

        if not rows_start or not rows_end:
            missing_period = year_start if not rows_start else year_end
            return StructuredQueryResult(
                answer_text=f"No rows matched the requested filters for year {missing_period} in table '{table_id}'.",
                operator="compare",
                target_column=val_col_name,
                predicate_columns=pred_cols,
                predicate_values=pred_vals,
                selection_count=0,
                total_rows_in_table=total_rows,
                result_value=None,
                secondary_value=None,
                row_ids=[],
                row_indices=[],
                selection_hash="0" * 16,
                schema_binding_confidence=1.0,
                overall_confidence=1.0,
                table_id=table_id,
                document_id=document_id,
                document_version=version,
                workspace_id=workspace_id,
                unit=None,
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
        is_lookup: bool = False,
    ) -> StructuredQueryResult:
        q_lower = query.lower()
        if is_lookup:
            op = AggregateOp.SUM
        elif bool(re.search(r'\bcount\b|\bhow many (?:rows|records|entries|items)\b', q_lower)):
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

        if is_lookup and count > 1:
            raise AmbiguousBindingError(
                f"Multiple matching rows ({count}) found in table '{table_id}' for filters {pred_vals}; "
                f"please specify a qualifier (e.g. year) or request an explicit aggregate."
            )

        col_unit = next((c.unit for c in schema.columns if c.col_index == col_idx), None)
        effective_op = "lookup" if is_lookup else op.value
        if count == 0:
            res_val = Decimal(0) if (not is_lookup and op in (AggregateOp.COUNT, AggregateOp.SUM)) else None
            if op == AggregateOp.COUNT:
                answer_text = f"No rows matched the requested filters in table '{table_id}' (0 matching rows). Count is 0."
            else:
                answer_text = f"The table '{table_id}' does not contain matching rows for the requested filters (0 matching rows)."
        else:
            res_val = val
            unit_suffix = f" {col_unit}" if col_unit else ""
            if is_lookup:
                answer_text = f"The {col_name} is {res_val}{unit_suffix}."
            else:
                answer_text = f"The {op.value} for {col_name} is {res_val}{unit_suffix}."

        return StructuredQueryResult(
            answer_text=answer_text,
            operator=effective_op,
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
            unit=col_unit,
        )

    def _execute_column_ratio(
        self,
        query: str,
        table_id: str,
        workspace_id: str,
        document_id: str,
        version: str,
        schema: TableSchema,
        total_rows: int,
    ) -> StructuredQueryResult:
        """Compute deterministic ratio or percentage between two numeric columns over filtered rows."""
        q_lower = query.lower()
        q_tokens = re.findall(r"[a-z0-9_]+", q_lower)
        numeric_dtypes = {"integer", "decimal", "percentage", "currency"}
        skip_names = {"year", "date", "period", "time", "sl", "sl. no.", "id", "index"}

        matched_cols: list[tuple[int, Any]] = []
        for col in schema.columns:
            dtype_str = self._normalize_dtype(col.inferred_dtype)
            c_lower = col.name.lower().strip()
            if dtype_str not in numeric_dtypes or c_lower in skip_names or "year" in c_lower:
                continue
            c_toks = [t for t in re.findall(r"[a-z0-9]+", c_lower) if len(t) >= 3]
            best_pos: int | None = None
            for idx, qt in enumerate(q_tokens):
                for ct in c_toks:
                    if qt == ct or (len(ct) >= 4 and qt.startswith(ct[:5])) or (len(qt) >= 4 and ct.startswith(qt[:5])):
                        best_pos = idx if best_pos is None else min(best_pos, idx)
            if best_pos is not None:
                matched_cols.append((best_pos, col))

        if len(matched_cols) < 2:
            raise UnsupportedQueryError(f"Query '{query[:60]}' could not bind two numeric columns for ratio/percentage")

        matched_cols.sort(key=lambda item: item[0])
        is_percent = bool(re.search(r"\b(?:percentage|percent)\b", q_lower))
        if is_percent and re.search(r"\b(?:percentage|percent)\s+of\b", q_lower) and "as a percentage of" not in q_lower:
            denom_col = matched_cols[0][1]
            num_col = matched_cols[1][1]
        else:
            num_col = matched_cols[0][1]
            denom_col = matched_cols[1][1]

        predicates, pred_cols, pred_vals = self._extract_and_bind_predicates(
            query=query,
            table_id=table_id,
            workspace_id=workspace_id,
            version=version,
            schema=schema,
            target_col_idx=None,
        )

        rows = self.store.query_rows(
            table_id=table_id,
            workspace_id=workspace_id,
            predicates=predicates,
            version=version,
        )
        r_ids = [r.row_id for r in rows]
        r_idx = [r.row_index for r in rows]
        digest = hashlib.sha256(f"{table_id}:{version}:{','.join(r_ids)}".encode("utf-8")).hexdigest()[:16]

        if not rows:
            return StructuredQueryResult(
                answer_text=f"The table '{table_id}' does not contain matching rows for the requested filters (0 matching rows).",
                operator="percentage" if is_percent else "ratio",
                target_column=f"{num_col.name}/{denom_col.name}",
                predicate_columns=pred_cols,
                predicate_values=pred_vals,
                selection_count=0,
                total_rows_in_table=total_rows,
                result_value=None,
                secondary_value=None,
                row_ids=[],
                row_indices=[],
                selection_hash=digest,
                schema_binding_confidence=0.95,
                overall_confidence=0.95,
                table_id=table_id,
                document_id=document_id,
                document_version=version,
                workspace_id=workspace_id,
                unit="percent" if is_percent else "ratio",
            )

        def _cell_decimal(row: Any, col_index: int) -> Decimal:
            raw = row.cells[col_index].normalized_value
            if raw is None:
                raw = row.cells[col_index].raw_value
            if isinstance(raw, Decimal):
                return raw
            return Decimal(str(raw).replace(",", "").strip())

        num_sum = sum((_cell_decimal(r, num_col.col_index) for r in rows), Decimal("0"))
        denom_sum = sum((_cell_decimal(r, denom_col.col_index) for r in rows), Decimal("0"))

        if denom_sum == 0:
            return StructuredQueryResult(
                answer_text=f"Cannot compute {'percentage' if is_percent else 'ratio'} of {num_col.name} over {denom_col.name}: denominator sum is 0.",
                operator="percentage" if is_percent else "ratio",
                target_column=f"{num_col.name}/{denom_col.name}",
                predicate_columns=pred_cols,
                predicate_values=pred_vals,
                selection_count=len(rows),
                total_rows_in_table=total_rows,
                result_value=None,
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
                unit="percent" if is_percent else "ratio",
            )

        dec_match = re.search(r"\bround\s+to\s+(\d+)\s+decimal", q_lower)
        places = int(dec_match.group(1)) if dec_match else (2 if is_percent else 4)
        raw_val = (num_sum / denom_sum) * (Decimal("100") if is_percent else Decimal("1"))
        quant = Decimal("1") if places == 0 else Decimal("1." + "0" * places).normalize()
        # Use fixed decimal places for clean rounding
        quant = Decimal("10") ** (-places)
        rounded_val = raw_val.quantize(quant)
        unit_suffix = "%" if is_percent else ""
        answer_text = f"The {'percentage' if is_percent else 'ratio'} of {num_col.name} to {denom_col.name} is {rounded_val}{unit_suffix} ({num_sum} / {denom_sum})."

        return StructuredQueryResult(
            answer_text=answer_text,
            operator="percentage" if is_percent else "ratio",
            target_column=f"{num_col.name}/{denom_col.name}",
            predicate_columns=pred_cols,
            predicate_values=pred_vals,
            selection_count=len(rows),
            total_rows_in_table=total_rows,
            result_value=rounded_val,
            secondary_value=num_sum,
            row_ids=r_ids,
            row_indices=r_idx,
            selection_hash=digest,
            schema_binding_confidence=0.95,
            overall_confidence=0.95,
            table_id=table_id,
            document_id=document_id,
            document_version=version,
            workspace_id=workspace_id,
            unit="percent" if is_percent else "ratio",
        )

    def _raise_if_ambiguous_scope(
        self,
        query: str,
        table_id: str,
        workspace_id: str,
        version: str,
        schema: TableSchema,
    ) -> None:
        """Raise AmbiguousBindingError when an aggregate/count query omits both filter and all-row scope."""
        predicates, _, _ = self._extract_and_bind_predicates(
            query=query,
            table_id=table_id,
            workspace_id=workspace_id,
            version=version,
            schema=schema,
            target_col_idx=None,
        )
        if predicates:
            return
        # If the query names a specific capitalized filter entity (e.g. "in SIDDHA"), it is not an omitted scope
        col_words = {w for c in schema.columns for w in re.findall(r"[a-z0-9]+", c.name.lower())}
        for m in re.finditer(r"\b(?:in|for|from)\s+([A-Z][A-Za-z0-9_&\-]{2,})\b", query):
            ent = m.group(1).strip()
            if ent.lower() not in col_words and ent.lower() not in {"the", "all", "each", "every"}:
                return
        probe_rows = self.store.query_rows(
            table_id=table_id,
            workspace_id=workspace_id,
            predicates=[],
            limit=500,
            version=version,
        )
        for col in schema.columns:
            dtype_str = self._normalize_dtype(col.inferred_dtype)
            c_lower = col.name.lower().strip()
            if dtype_str != "string" or c_lower in {"id", "index", "sl. no."}:
                continue
            distinct_vals: list[str] = []
            seen: set[str] = set()
            for r in probe_rows:
                val = str(r.cells[col.col_index].normalized_value or r.cells[col.col_index].raw_value or "").strip()
                if val and val.lower() not in seen:
                    seen.add(val.lower())
                    distinct_vals.append(val)
            if len(distinct_vals) > 1:
                raise AmbiguousBindingError(
                    f"Query does not specify which {col.name} ({', '.join(distinct_vals[:10])}) or whether you want the total across all rows."
                )

    def _execute_formula_verification(
        self,
        query: str,
        table_id: str,
        workspace_id: str,
        document_id: str,
        version: str,
        schema: TableSchema,
        total_rows: int,
    ) -> StructuredQueryResult:
        """Bind row-keyed operands, compute binary arithmetic deterministically, and verify against referenced cell."""
        q_lower = query.lower()
        op_match = re.search(r"\b(minus|plus)\b", q_lower)
        if not op_match:
            raise UnsupportedQueryError(f"Query '{query[:60]}' has no supported binary arithmetic operator")
        op_word = op_match.group(1)
        op_pos = op_match.start()
        is_minus = op_word == "minus"

        predicates, pred_cols, pred_vals = self._extract_and_bind_predicates(
            query=query,
            table_id=table_id,
            workspace_id=workspace_id,
            version=version,
            schema=schema,
            target_col_idx=None,
        )
        rows = self.store.query_rows(
            table_id=table_id,
            workspace_id=workspace_id,
            predicates=predicates,
            version=version,
        )
        if not rows:
            raise UnsupportedQueryError(f"No rows matched filters for formula verification in '{table_id}'")

        numeric_dtypes = {"integer", "decimal", "percentage", "currency"}
        skip_names = {"year", "date", "period", "time", "sl", "sl. no.", "id", "index"}
        val_col = next(
            (
                c for c in schema.columns
                if self._normalize_dtype(c.inferred_dtype) in numeric_dtypes
                and c.name.lower().strip() not in skip_names
                and "year" not in c.name.lower()
            ),
            None,
        )
        if val_col is None:
            raise UnsupportedQueryError(f"No numeric value column found in '{table_id}'")

        # Find the string label column whose values match operands in the query
        bound_col_names = {pc.lower() for pc in pred_cols}
        label_col = None
        matched_vars: list[tuple[int, str, Any, Decimal]] = []
        for col in schema.columns:
            if self._normalize_dtype(col.inferred_dtype) != "string" or col.name.lower() in bound_col_names:
                continue
            cand_matches: list[tuple[int, str, Any, Decimal]] = []
            seen_labels: set[str] = set()
            for r in rows:
                lbl = str(r.cells[col.col_index].normalized_value or r.cells[col.col_index].raw_value or "").strip()
                if not lbl or lbl.lower() in seen_labels:
                    continue
                pos = q_lower.find(lbl.lower())
                if pos != -1:
                    raw_num = r.cells[val_col.col_index].normalized_value
                    if raw_num is None:
                        raw_num = r.cells[val_col.col_index].raw_value
                    try:
                        dec_num = raw_num if isinstance(raw_num, Decimal) else Decimal(str(raw_num).replace(",", "").strip())
                    except Exception:
                        continue
                    seen_labels.add(lbl.lower())
                    cand_matches.append((pos, lbl, r, dec_num))
            if len(cand_matches) >= 2:
                label_col = col
                matched_vars = cand_matches
                break

        if label_col is None or len(matched_vars) < 2:
            raise UnsupportedQueryError(f"Could not bind binary arithmetic operands in '{table_id}'")

        before_op = [m for m in matched_vars if m[0] < op_pos]
        after_op = [m for m in matched_vars if m[0] > op_pos]
        if not before_op or not after_op:
            raise UnsupportedQueryError(f"Could not align operands around '{op_word}' in '{query[:60]}'")

        left = max(before_op, key=lambda m: m[0])
        right = min(after_op, key=lambda m: m[0])
        others = [m for m in matched_vars if m[1] not in (left[1], right[1])]
        target = others[0] if len(others) == 1 else None

        computed = (left[3] - right[3]) if is_minus else (left[3] + right[3])
        used_rows = [left[2], right[2]] + ([target[2]] if target else [])
        r_ids = [r.row_id for r in used_rows]
        r_idx = [r.row_index for r in used_rows]
        digest = hashlib.sha256(f"{table_id}:{version}:{','.join(r_ids)}".encode("utf-8")).hexdigest()[:16]

        if target is not None:
            diff = abs(target[3] - computed)
            if computed == target[3]:
                answer_text = (
                    f"Yes: {left[1]} ({left[3]}) {'minus' if is_minus else 'plus'} {right[1]} ({right[3]}) "
                    f"equals {computed}, matching {target[1]} ({target[3]})."
                )
            else:
                answer_text = (
                    f"No: {left[1]} ({left[3]}) {'minus' if is_minus else 'plus'} {right[1]} ({right[3]}) "
                    f"is {computed}, whereas {target[1]} reports {target[3]} (difference: {diff})."
                )
            target_col_label = f"{left[1]} {'-' if is_minus else '+'} {right[1]} vs {target[1]}"
            secondary_val = target[3]
        else:
            answer_text = (
                f"{left[1]} ({left[3]}) {'minus' if is_minus else 'plus'} {right[1]} ({right[3]}) equals {computed}."
            )
            target_col_label = f"{left[1]} {'-' if is_minus else '+'} {right[1]}"
            secondary_val = None

        return StructuredQueryResult(
            answer_text=answer_text,
            operator="formula_verification",
            target_column=target_col_label,
            predicate_columns=pred_cols,
            predicate_values=pred_vals,
            selection_count=len(used_rows),
            total_rows_in_table=total_rows,
            result_value=computed,
            secondary_value=secondary_val,
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
