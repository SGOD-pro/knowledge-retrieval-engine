# Review, re-factor, 2026-10-31

**Reviewed by**: Senior Code Reviewer
**Scope**: 13 files, commit 2d7ed816642aa4c4124e0bf3f08b3e70ec3776f1 against 10c8c15600ed5d3899739734f766d0656ea4da15
**Verdict**: Changes requested

## Summary

This review inspects commit [`2d7ed816642aa4c4124e0bf3f08b3e70ec3776f1`](file:///home/swyra/projects/knowledge-retrieval-engine) on branch `re-factor`. The changes address structured aggregate routing, deep probe scan bounds, and reranker telemetry reporting. The commit successfully resolves the benchmark telemetry inflation where 60 reranker successes were reported despite only 41 remote calls. It also adds bounded pagination to table stores, reducing probe scans that previously stalled DynamoDB.

However, two major issues require remediation before production merge. First, deep probe table scans terminate silently at 500 items without exposing a truncation flag. If a filter entity resides past row 500, the system misclassifies the filter as absent, raises an unresolved filter error, and terminates execution without falling back to text retrieval. Second, reranker telemetry relies on a global variable protected by a lock, which introduces race conditions under concurrent workloads where concurrent requests can overwrite each other's reported mode. Several minor semantic inconsistencies around empty aggregates and memory store implementation are also noted.

All 79 relevant offline regression tests pass cleanly. Scorer rules and benchmark ground truth remain unmodified.

## Major

### Truncated scan in TableStore deep probe produces false unresolved filter errors and stops pipeline without fallback, [`backend/src/services/retrieval/structured_query_service.py:727`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/structured_query_service.py#L721-L757)

**Problem**: In [`StructuredQueryService._extract_and_bind_predicates`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/structured_query_service.py#L721) and line 957, candidate filter binding executes [`store.query_rows`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/base.py#L52) with `max_scan_items = 500`. In [`DynamoDBTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/dynamodb_store.py#L671) and [`SQLiteTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/sqlite_store.py#L936), reaching 500 items terminates pagination and returns an empty list without indicating that truncation occurred. The service infers that the requested filter value does not exist in the table and raises [`UnresolvedFilterError`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/structured_query_service.py#L52). In [`langgraph_pipeline.py:1397`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L1397), [`route_after_structured`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L1390) routes `structured_aggregate_unresolved_filter` straight to `END` instead of falling back to vector retrieval.

**Why it matters**: In large tables such as `survay.csv` containing 60,255 rows, any categorical entity appearing after row 500 cannot be bound. The pipeline halts with an error instead of retrieving relevant passages or completing the query. Callers receive a false refusal even though the requested data exists in the table.

**Suggested fix**: Return a truncation indicator from [`TableStore.query_rows`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/base.py#L52) so callers know whether zero matches means the table was exhausted or truncated. When an explicit filter cannot be resolved due to a truncated scan, [`route_after_structured`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L1390) should fall back to general retrieval (`run_okf_router`) rather than halting execution.

### Global reranker telemetry state causes race conditions during concurrent requests, [`backend/src/providers/reranker_provider.py:37`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/providers/reranker_provider.py#L37-L58)

**Problem**: The last reranker mode is stored in module global variable `_last_reranker_mode` guarded by `_mode_lock`. When multiple requests run concurrently in FastAPI worker threads or async tasks, one request can overwrite `_last_reranker_mode` between the time another request calls [`rerank_documents`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/providers/reranker_provider.py#L324) and the time it reads [`get_last_reranker_mode`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L738) in [`langgraph_pipeline.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L738).

**Why it matters**: Under concurrent traffic, a query whose reranker call succeeded can report `bypassed` or `circuit_open_lexical_fallback`. Conversely, a query that fell back can falsely report `remote_success`.

**Suggested fix**: Eliminate global variable mutation for request telemetry. Pass request telemetry via a `contextvars.ContextVar` or return the telemetry mode tuple directly from [`rerank_documents`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/providers/reranker_provider.py#L324) so each pipeline execution manages its own state independently.

## Minor

### Empty aggregate results return misleading zero values and citations for statistical operators, [`backend/src/services/retrieval/structured_query_service.py:1171`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/structured_query_service.py#L1171)

**Problem**: In [`StructuredQueryService._execute_aggregate`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/structured_query_service.py#L1171), when `count == 0`, `res_val` defaults to `Decimal(0)`. For statistical operations like `MIN`, `MAX`, and `AVG`, the value of an empty set is undefined, not zero. Returning zero implies that zero was present in the data. Furthermore, [`run_structured_aggregate.py:127`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/run_structured_aggregate.py#L127) constructs an evidence item citing `0 row(s) from table` alongside `is_refusal = True`.

**Why it matters**: Presenting zero for minimum or maximum values produces mathematically incorrect results when all values are non zero. Attaching citations that reference zero rows contradicts the refusal state.

**Suggested fix**: Set `result_value = None` when `count == 0` for operations other than `COUNT`. Omit evidence citations when `selection_count == 0`.

### MemoryTableStore ignores max_scan_items parameter, [`backend/src/db/table_store/memory_store.py:241`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/memory_store.py#L241)

**Problem**: [`MemoryTableStore.query_rows`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/memory_store.py#L234) accepts `max_scan_items` in its signature but does not pass it to [`_execute_pass`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/memory_store.py#L296), which scans all rows unconditionally.

**Why it matters**: Tests running on [`MemoryTableStore`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/memory_store.py#L12) behave differently from production stores, creating blind spots where tests pass while DynamoDB or SQLite truncate.

**Suggested fix**: Update [`MemoryTableStore._execute_pass`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/memory_store.py#L296) to accept and respect `max_scan_items`.

### Hardcoded benchmark typo and broad keyword exclusion in query routing, [`backend/src/services/retrieval/planner.py:294`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/planner.py#L294)

**Problem**: The regex `re.search(r'(?:spreadsheet|survay|dataset)', q_lower)` in [`Planner`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/planner.py#L284) and [`StrategyRouter`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/strategy_router.py#L68) hardcodes the benchmark typo `survay`, omitting the correct spelling `survey`. Additionally, `is_document_content_ref` matches common business terms such as `report`, `study`, and `document`.

**Why it matters**: A question like "What is the total revenue in the annual report table?" is excluded from structured aggregate routing unless a file extension like `.csv` is written. Questions using the word `survey` rather than `survay` miss the explicit tabular indicator.

**Suggested fix**: Include `survey` alongside `survay`. Require structural qualifiers like `table \d+` or `figure \d+` rather than generic words like `report` before excluding queries from structured aggregation.

## Nits

* Nit: In [`backend/src/services/retrieval/structured_query_service.py:720`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/structured_query_service.py#L720), integer casting uses `int(cand)` without handling commas or whitespace.
* Nit: In [`backend/src/services/retrieval/run_structured_aggregate.py:173`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/run_structured_aggregate.py#L173), hardcoded string literal `bypassed` should reference an enumeration or shared constant.

## Scorer Analysis: Percentage Signs and Metric Integrity

The proposed modification to strip percentage signs from benchmark answers was evaluated and rejected:

* Semantic distinction between ratios and counts: A value of 50 percent denotes a relative ratio equal to 0.50. In contrast, the integer 50 denotes a discrete quantity such as 50 dollars, 50 bills passed, or 50 records. Stripping percent signs collapses this semantic boundary, allowing a model that outputs an integer count to receive credit for a ratio question or vice versa.
* Financial and demographic precision: In regulatory filings and demographic surveys, distinguishing percentage values from raw currencies or headcounts is critical. Conflating the two degrades benchmark sensitivity to unit hallucination.
* Root cause of question Q149 failure: Investigation shows question Q149 was not marked incorrect due to a percent symbol difference. The model output was the bare string `99.58`. In [`benchmark_scorer.py:8`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/evaluation/benchmark_scorer.py#L8), [`tokenize`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/evaluation/benchmark_scorer.py#L8) keeps alphanumeric tokens with length greater than 2. For `99.58`, the extracted numeric parts have length 2, resulting in an empty token set. This caused token recall to evaluate to 0.0, which failed the guard requiring token recall of at least 0.25.
* Benchmark comparability: Preserving historical comparisons requires keeping `scorer_version = "2.1.0"` unchanged. Modifying scoring rules or ground truth during code fixes violates benchmark integrity rules.

## Verification and Test Evidence

Offline test execution completed successfully across all relevant suites with zero network calls:

* [`backend/tests/test_routing_and_telemetry_defects.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_routing_and_telemetry_defects.py): 10 passed
* [`backend/tests/test_structured_query_service.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_structured_query_service.py): 21 passed
* [`backend/tests/test_canonical_benchmark_runner_settings.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_canonical_benchmark_runner_settings.py): 6 passed
* [`backend/tests/test_provider_routing.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_provider_routing.py): 5 passed
* [`backend/tests/test_table_store.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_table_store.py): 6 passed
* [`backend/tests/test_csv_ingestion_complete.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_csv_ingestion_complete.py): 7 passed
* [`backend/tests/test_mandatory_audit_guards.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_mandatory_audit_guards.py): 10 passed
* [`backend/tests/test_benchmark_guard.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_benchmark_guard.py): 4 passed
* [`backend/tests/test_benchmark_integrity_defects.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_benchmark_integrity_defects.py): 10 passed

Total: 79 passed tests, 0 failures.

Workspace isolation is preserved across all stores by enforcing workspace scoping on primary keys and queries. Complete table reads for aggregation remain unbounded, while probe limits protect against latency stalls. Historical workspace `ws_fresh_benchmark` and baseline artifacts remain intact.

## Strengths

* Safe probe bounds: Adding `max_scan_items` protects DynamoDB and SQLite from unbounded 60,000 item table scans during candidate binding.
* Defensible telemetry: Reranker reporting now defaults to `bypassed` and records remote success only when the provider call actually succeeds.
* Thorough test additions: 10 new regression tests thoroughly exercise mixed sources, ambiguous bindings, storage errors, and telemetry fallback.
