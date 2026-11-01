# Review, re-factor, 2026-11-01

**Reviewed by**: Senior Code Reviewer
**Scope**: 15 files, commit [`1a0f2252bbfee061033e98b9d04d2bd50839b6d2`](file:///home/swyra/projects/knowledge-retrieval-engine) against [`2d7ed816642aa4c4124e0bf3f08b3e70ec3776f1`](file:///home/swyra/projects/knowledge-retrieval-engine)
**Verdict**: Approve

## Summary

This review inspects commit [`1a0f2252bbfee061033e98b9d04d2bd50839b6d2`](file:///home/swyra/projects/knowledge-retrieval-engine) on branch `re-factor`. The change addresses all items flagged in the previous review [`docs/reviews/2026-10-31-re-factor-routing-and-telemetry-review.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-31-re-factor-routing-and-telemetry-review.md).

Bounded table scans across all three store implementations ([`DynamoDBTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/dynamodb_store.py#L32), [`SQLiteTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/sqlite_store.py#L123), and [`MemoryTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/memory_store.py#L12)) now return [`TableRowList`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/base.py#L38) with explicit truncation metadata. When a scan hits the 500 item bound before locating a requested filter entity, [`StructuredQueryService`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/structured_query_service.py#L76) raises [`UnresolvedFilterError(was_truncated=True)`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/structured_query_service.py#L55). The pipeline node [`run_structured_aggregate`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/run_structured_aggregate.py#L25) routes this to `structured_aggregate_fallback`, and [`route_after_structured`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L1387) continues into general document retrieval via `run_okf_router`. Partial scans cannot produce confident absence claims or false refusals.

Shared global reranker telemetry has been eliminated. The pipeline now relies on `contextvars.ContextVar` in [`providers.reranker_provider`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/providers/reranker_provider.py#L38) alongside direct mode passing from [`rerank`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/reranker.py#L10) to [`run_reranker`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L714). Telemetry modes remain isolated across concurrent threads, asynchronous tasks, and worker threads.

Empty aggregate semantics have been corrected. Statistical operations (`AVG`, `MIN`, `MAX`) return `None` rather than `Decimal(0)` on empty sets, while `COUNT` and `SUM` return `Decimal(0)`. Evidence citations are omitted on zero match refusal responses. Document content references have been scoped to numbered paper structures, preserving legitimate report table queries.

All 85 offline regression tests pass cleanly. Benchmark ground truth and scorer rules remain untouched with `scorer_version = "2.1.0"`.

## Detailed Verification of Focus Areas

### 1. Bounded Scans and Pagination on DynamoDB, SQLite, and Memory
* **Zero rows**: In [`MemoryTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/memory_store.py#L12), [`SQLiteTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/sqlite_store.py#L123), and [`DynamoDBTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/dynamodb_store.py#L32), executing [`query_rows`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/base.py#L53) with `max_scan_items = 500` returns `TableRowList([], is_truncated=False, scanned_count=0)`.
* **Exactly 500 rows**: All three stores scan exactly 500 rows. At row 500, candidate checks confirm that no further rows exist in the partition or cursor. `is_truncated` evaluates to `False`, and `scanned_count` is 500.
* **More than 500 rows (501 rows)**: At row 500, [`MemoryTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/memory_store.py#L354) observes `idx + 1 < len(candidate_rows)`. [`SQLiteTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/sqlite_store.py#L942) observes `cur.fetchone() is not None`. [`DynamoDBTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/dynamodb_store.py#L675) observes remaining page items or `LastEvaluatedKey`. All three stores set `is_truncated = True` and return exactly 500 rows with `scanned_count = 500`.

### 2. Truncated Filter Fallback through Final Answer
* **Trace through the graph**: When a query filters on an entity past row 500 (such as row 550 of 600), candidate probing truncates at item 500. The candidate is marked in `truncated_cands`. Because the entity was truncated and unresolved, [`StructuredQueryService._extract_and_bind_predicates`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/structured_query_service.py#L771) raises [`UnresolvedFilterError(was_truncated=True)`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/structured_query_service.py#L55).
* **Safe fallback routing**: In [`run_structured_aggregate.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/run_structured_aggregate.py#L121), catching `was_truncated=True` returns `{"executed_path": "structured_aggregate_fallback", "reranker_mode": "bypassed"}`. In [`langgraph_pipeline.py:1387`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L1387), [`route_after_structured`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L1387) routes `structured_aggregate_fallback` to `run_okf_router`. The query continues into BM25, vector search, reranking, and generation.
* **Absence claims guarded**: When an entity is genuinely absent in a completely searched table (such as a 100 row table), `was_truncated` evaluates to `False`. The node returns `structured_aggregate_unresolved_filter`, which routes to `__end__`. A partial scan can never trigger this termination.
* **Aggregation integrity**: Aggregate computation in [`execute_single_pass_query`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/base.py#L75) remains unbounded and consumes all table pages. It never computes numbers over a truncated row slice.

### 3. Reranker Telemetry Isolation Across Threads and Async Tasks
* **Thread safety**: Tested with 30 concurrent worker threads executing remote success, circuit breaker fallback, and bypassed paths simultaneously. Each thread observed its own mode via `ContextVar` without race conditions or overwrites.
* **Async task safety**: Tested with 30 concurrent `asyncio` tasks running on the same event loop. Each task retained independent context without cross task leakage.
* **Direct propagation**: [`rerank`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/reranker.py#L10) now supports `return_mode = True`, allowing [`run_reranker`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L714) to capture the mode directly from function output, bypassing shared module reads.

### 4. Empty Aggregates, Citations, Routing, Workspace Isolation, and Complete Reads
* **Empty aggregate semantics**:
  * `COUNT` on zero matching rows evaluates to `Decimal(0)`.
  * `SUM` on zero matching rows evaluates to `Decimal(0)`.
  * `AVG` on zero matching rows evaluates to `None` with `Average is undefined.` in answer text.
  * `MIN` on zero matching rows evaluates to `None` with `Min is undefined.` in answer text.
  * `MAX` on zero matching rows evaluates to `None` with `Max is undefined.` in answer text.
* **Citations suppression**: In [`run_structured_aggregate.py:138`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/run_structured_aggregate.py#L138), `is_refusal = (result.selection_count == 0)`. When true, `citations` and `retained_evidence_items` are empty lists, preventing the refusal from citing nonexistent matching rows.
* **Mixed source routing**:
  * "In Table 1 of the interpretability paper, what are the attention values?" routes to `use_structured_aggregate = False`.
  * "What is the total expenditure in the annual financial report table?" routes to `use_structured_aggregate = True`.
* **Workspace isolation**: A table stored in workspace `ws_alpha` cannot be accessed, listed, or aggregated from workspace `ws_beta`. `StructuredQueryService.execute` returns `None` when no tables exist in the requested workspace.
* **Complete TableStore reads**: Aggregation across 1,500 rows in [`SQLiteTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/sqlite_store.py#L123) and [`DynamoDBTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/dynamodb_store.py#L32) consumes all rows without bounding, confirming that `max_scan_items` applies only to candidate filter probing.

## Findings by Severity

### Blockers
None.

### Major
None.

### Minor
None.

### Nits
None.

## Limitations and Considerations

1. **Unindexed filter probes beyond row 500**: When a categorical filter value appears only past row 500 in an unindexed column, bounded probing terminates and falls back to text retrieval. This prevents DynamoDB and SQLite from stalling on 60,000 item scans. If the document chunks do not contain the answer, the text retrieval path may still produce an empty response.
2. **ContextVar propagation across custom thread pools**: The isolated reranker mode relies on Python `contextvars`. If worker threads are spawned in the future without copying context (via `contextvars.copy_context().run`), telemetry defaults safely to `bypassed`.

## Verification and Test Evidence

Runtime verification scripts and full offline test suites were executed with zero network calls and zero paid provider usage:

* [`backend/tests/test_routing_and_telemetry_defects.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_routing_and_telemetry_defects.py): 16 passed
* [`backend/tests/test_structured_query_service.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_structured_query_service.py): 21 passed
* [`backend/tests/test_canonical_benchmark_runner_settings.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_canonical_benchmark_runner_settings.py): 6 passed
* [`backend/tests/test_provider_routing.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_provider_routing.py): 5 passed
* [`backend/tests/test_table_store.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_table_store.py): 6 passed
* [`backend/tests/test_csv_ingestion_complete.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_csv_ingestion_complete.py): 7 passed
* [`backend/tests/test_mandatory_audit_guards.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_mandatory_audit_guards.py): 10 passed
* [`backend/tests/test_benchmark_guard.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_benchmark_guard.py): 4 passed
* [`backend/tests/test_benchmark_integrity_defects.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_benchmark_integrity_defects.py): 10 passed

Total: 85 passed tests, 0 failures.

Workspace isolation is preserved across all stores by enforcing workspace scoping on primary keys and queries. Historical workspace `ws_fresh_benchmark` and baseline artifacts under `backend/reports/` remain intact.
