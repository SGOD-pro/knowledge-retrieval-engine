# Handoff: Core Engine Walking Skeleton and Benchmarks (0004)

**Date**: October 15 2026
**Branch**: re-factor
**Status**: 0004 Complete (Core Engine Walking Skeleton and Benchmarks Accepted; Spec 0003 Preserved as Proposed and Deferred)

## Overview

This document records the full completion and acceptance of specification 0004 (Core engine walking skeleton and benchmarks).
The milestone proves the vertical slice through all application layers:
1. Document ingestion: real CSV and text PDF files with provenance recorded in `DocumentSnapshot`.
2. Structured execution: full tabular dataset processing without row truncation, deterministic single pass math.
3. Hybrid retrieval: BM25 baseline retrieval, dual embeddings (fast 384d and full 1024d), and bounded reranking.
4. Verified answer assembly: exact `DocumentLocation` and `TableLocation` citations, source supported fact verification, and aggregate blockers on incomplete coverage.
5. Insufficient evidence handling: structured HTTP 200 refusal with empty citations, distinct from HTTP 503 infrastructure errors.
6. Reproducible benchmark runner: `BenchmarkRunner` measuring correctness, evidence support, coverage, token usage, and latency with adversarial regression protection.
7. Recoverable metric persistence: `BenchmarkPersistenceService` enforcing AC-10 S3 first, then DynamoDB metadata commit ordering.

Specification 0003 remains Proposed and deferred.

## Working Tree State and Key Additions

* [backend/src/schemas/contracts/boundary.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/boundary.py): Added `DocumentSnapshot` and `BenchmarkRunMetadata` models, `CitationItem` location bindings, and counting unit documentation.
* [backend/src/db/workspace_boundary_repo.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/workspace_boundary_repo.py): Added snapshot and benchmark metadata CRUD across SQLite, in memory, and DynamoDB repository implementations.
* [backend/src/services/benchmark/artifact_store.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/benchmark/artifact_store.py): Implemented `S3BenchmarkArtifactStore` uploading full versioned JSON benchmark reports.
* [backend/src/services/benchmark/persistence_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/benchmark/persistence_service.py): Implemented `BenchmarkPersistenceService` ensuring recoverable write sequencing (S3 first, then DynamoDB).
* [backend/src/services/benchmark/benchmark_runner.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/benchmark/benchmark_runner.py): Implemented `BenchmarkRunner` with adversarial scoring protection:
  * Fabricated positive answers with 0 citations fail insufficient evidence evaluation.
  * Wrong source, wrong version, or unsupported citations fail evidence support evaluation.
  * Complete dataset aggregates pass coverage scoring; incomplete dataset complete claims fail coverage scoring.
  * Token accounting uses provider tokens when available, does not invent model tokens on deterministic execution (reports 0), and explicitly distinguishes estimated vs unavailable tokens.
* [backend/src/providers/llm_provider.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/providers/llm_provider.py): Fast test environment check extracting factual context answers during tests.
* [backend/tests/test_benchmark_runner_and_persistence_0004.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_benchmark_runner_and_persistence_0004.py): 10 unit and integration tests covering S3 artifact storage, DynamoDB persistence, recoverable write ordering, orphan S3 tolerance, and the 4 adversarial scoring regression cases.
* [backend/tests/test_pdf_retrieval_slice_0004.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_pdf_retrieval_slice_0004.py): Strengthened PDF verification asserting the expected source supported fact ("wealth creator").
* [verify_0004_live.py](file:///home/swyra/projects/knowledge-retrieval-engine/verify_0004_live.py): 17 live HTTP integration checks driving real workspace creation, 250 row calculation, boundary denial, tombstoning, and factual PDF extraction against the running FastAPI service.

## Execution Environment Distinctions

* Local HTTP checks: 17 live end to end requests executed over HTTP against local Uvicorn FastAPI server on port 8001 in `verify_0004_live.py`, verifying real document ingestion, 250 row calculation without truncation, cross workspace denial, and factual citation extraction.
* Mocks: isolated LLM completion generation in unit tests (`MODEL_PROVIDER=test`), mock S3 artifact store, and mock DynamoDB table items in isolated repository unit tests.
* Real model execution: local embedding computation (MiniLM-L6-v2 384d and BGE 1024d) and local BM25 ranking run real local algorithm execution; remote AWS Bedrock invocations bypassed during automated tests.
* Live AWS verification: deferred; DynamoDB and S3 interactions verified through local SQLite, in memory substitutes, and botocore client mock structures; no active AWS cloud deployment performed.

## Checks Run and Verification Evidence

* Full 0004 Test Suite:
  * Command: `uv run pytest tests/test_csv_vertical_slice_0004.py tests/test_pdf_retrieval_slice_0004.py tests/test_structured_query_service.py tests/test_fix1_async_ingestion.py tests/test_dynamodb_boundary_and_execution.py tests/test_benchmark_runner_and_persistence_0004.py`
  * Outcome: 38 passed, 0 failed in 29.20s.
* Live FastAPI Integration Verification:
  * Command: `uv run python ../verify_0004_live.py`
  * Outcome: All behaviors passed (17 of 17).
* Independent Senior Code Review:
  * Model: `gemini-2.5-pro` (author on `gemini 3.8 flash`)
  * Scope: 33 changed files across branch `re-factor` vs `main`
  * Verdict: Approve with nits (0 blockers, 0 majors, 0 minors, 2 nits resolved)
  * Report: [docs/reviews/2026-10-15-re-factor.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-15-re-factor.md)

* Specification 0003: Snapshot registry, publication, and atomic deletion remains Proposed and deferred.
* Specification 0004: Core engine walking skeleton and benchmarks is fully accepted and marked done.

## Fast to Full Escalation Repair Verification (October 17 2026)

* Base commit: `e86d30b050c77fc8e411ea21a603953aa1e5aa9a`
* Working tree changes:
  * [backend/src/services/langgraph_pipeline.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py): Enforced model provenance validation in `escalate_to_full_retrieval`. Untrusted 1024 dimensional vectors lacking reliable provenance are replaced through the configured full embedding path. Preserved single generation limits and refreshed candidate state.
  * [backend/tests/test_fast_path_fallback_regressions.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_fast_path_fallback_regressions.py): Added 3 regressions (13 total) asserting that untrusted 1024 dimensional state vectors are replaced through the configured full embedding path in unit and pipeline executions, and verifying that legitimate vectors with matching model provenance are reused.
  * [backend/AGENTS.md](file:///home/swyra/projects/knowledge-retrieval-engine/backend/AGENTS.md): Updated gotcha regarding model provenance requirement for escalation query embedding reuse.
  * [docs/specs/0004-verify.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0004-verify.md): Recorded updated fast path regression verification steps and execution environment distinctions (57 core tests passed).

* Executed Commands and Verification Outcomes:
  * Fast path fallback regressions: `uv run pytest tests/test_fast_path_fallback_regressions.py` (13 passed in 3.77s).
  * Core 0004 suite: `uv run pytest tests/test_benchmark_runner_and_persistence_0004.py tests/test_csv_vertical_slice_0004.py tests/test_dynamodb_boundary_and_execution.py tests/test_pdf_retrieval_slice_0004.py tests/test_structured_query_service.py tests/test_fast_path_fallback_regressions.py` (57 passed in 34.52s).

* Embedding Compatibility Resolution:
  * In `escalate_to_full_retrieval`, query vector reuse is no longer permitted solely because an existing vector has 1024 dimensions.
  * Reuse requires verified model provenance matching the configured full retrieval model (`get_embedding_model()`).
  * Any vector with missing, null, or mismatched provenance is replaced with a fresh compatible query embedding from the configured full embedding provider (`embed_text(query, provider=active_provider)` or `repo.generate_query_embedding(query)`).
  * Document re embedding was not performed and no new metadata framework was introduced.

* Pending Diagnostic Findings Preserved:
  * Q152: Incomplete `survay.csv` partition coverage in DynamoDB TableStore remains a pending correctness finding.
  * Q153: Table binding and year delta column mapping remains a pending correctness finding.
  * Q159: Missing filter handling remains a pending correctness finding.
  * Preserved original canonical 60 question baseline artifacts (`manifest.json`, `query_details.json`, `summary.md`) and mixed reranker baseline description (remote Cohere reranker with local lexical fallback mode).

* Scope and Status Boundaries:
  * Specification 0004 remains locally Accepted.
  * Specification 0003 remains Proposed and deferred.
  * No claims of independent live AWS readiness or benchmark accuracy improvements.
  * Paid canonical 60 question benchmark was not rerun.

## TableStore Multiyear Coverage Investigation and Verification (October 17 2026)

* Base commit: `5da60942cd52be201b66c8599736e70d056d395e`
* Working tree changes:
  * [backend/src/db/table_store/memory_store.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/memory_store.py): Streamlined [`execute_single_pass_query`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/table_store/memory_store.py#L273) to extract row indices directly from matched rows rather than executing an extra scan over candidate rows.
  * [backend/src/modules/documents/documents_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/modules/documents/documents_service.py): Hardened [`DocumentSnapshot`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/boundary.py#L38) construction to safely inspect TableStore manifest coverage when present, falling back accurately to document chunks when absent.
  * [backend/tests/test_tablestore_multiyear_coverage_regressions.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_tablestore_multiyear_coverage_regressions.py): Added offline regression suite with 9 passing tests covering datasets above 5000 rows across multiple years, complete range and sum calculations, temporal metric deltas, rejection tracking, failure on incomplete coverage, boundary isolation, DynamoDB paginated reads over multiple pages via `LastEvaluatedKey`, and complete `survay.csv` validation.
  * [backend/AGENTS.md](file:///home/swyra/projects/knowledge-retrieval-engine/backend/AGENTS.md): Recorded the distinction between TableStore full row persistence and the 5000 chunk cap for vector search.
  * [docs/specs/0004-verify.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0004-verify.md): Updated acceptance criteria verification records (66 core tests passed).
  * [docs/reviews/2026-10-17-re-factor.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-17-re-factor.md): Recorded senior code review findings.

* Investigation and Disproof Findings:
  * Raw source data inspection: [`data/non_pdf_formats/survay.csv`](file:///home/swyra/projects/knowledge-retrieval-engine/data/non_pdf_formats/survay.csv) and [`backend/tests/data/survay.csv`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/data/survay.csv) contain exactly 60255 data rows and 1 header row, with 13 distinct years from 2013 through 2025, having exactly 4635 rows per year in reverse chronological order (rows 1 to 4635 are year 2025, rows 4636 to 9270 are year 2024, down to rows 55621 to 60255 for year 2013).
  * Disproved row cap in TableStore code: [`backend/src/ingestion/csv_table_ingester.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/csv_table_ingester.py) streams all rows in batches of at most 99 rows without any 5000 row cap or year filter. Full ingestion supports all 60255 rows across 2013 through 2025.
  * Structured coverage distinction: Vector chunk counts cannot establish structured TableStore coverage. The vector adapter caps at `VECTOR_MAX_CHUNKS = 5000`, so `DocumentSnapshot` records 5000 persisted chunks for vector indexing; TableStore streams and persists the complete 60255 row dataset independently.
  * Root cause of the reported 5000 row or all 2025 discrepancy:
    1. Vector parser cap: [`backend/src/ingestion/adapters/csv_adapter.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/adapters/csv_adapter.py#L46) took the first 5000 rows (4635 rows of 2025 and 365 rows of 2024), recorded as `persisted_rows: 5000` on the vector snapshot.
    2. Stale benchmark workspace data: in workspace `ws_fresh_benchmark`, TableStore held an earlier 4 row mock test fixture `test_doc` from year 2025 (`1db813afd34f9410a818de3aeb9d3133`). That explains why Q152 answered "2025 and 2025." during the benchmark run.
    3. Proof of complete TableStore storage: in workspace `ws_survay_verify_3797a7b9`, `survay.csv` was ingested into DynamoDB TableStore with all 60255 rows across all 13 years (2013 to 2025), and query execution answered `2013 and 2025.` with selection count 60255.

* Comparison Workspace Procedure (Pending):
  * Historical preservation: `ws_fresh_benchmark` and its existing document versions remain preserved intact. In place recovery, version pruning, and deletion of existing data are prohibited.
  * Isolated comparison procedure:
    * Workspace identifier: dedicated comparison workspace such as `ws_survay_compare` or `ws_benchmark_compare`
    * Source document path: `data/non_pdf_formats/survay.csv`
    * Target incoming version: `c029d234eefd3fee2563b9c0929946ca`
    * Table name: `survay`
    * Target manifest: `coverage_complete=True, source_rows=60255, persisted_rows=60255, rejected_rows=0`
  * Execution status: Kept strictly pending as a recommended comparison procedure. Do not execute recovery, in place mutation, or deletion of existing data.

* Canonical Baseline Artifacts and Reconciliation:
  * Exact artifact paths:
    * Manifest: [`backend/reports/c44a47857fa45d1610c21c2cda83670c43272c11/20261017_001258/manifest.json`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/c44a47857fa45d1610c21c2cda83670c43272c11/20261017_001258/manifest.json)
    * Per query details: [`backend/reports/c44a47857fa45d1610c21c2cda83670c43272c11/20261017_001258/query_details.json`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/c44a47857fa45d1610c21c2cda83670c43272c11/20261017_001258/query_details.json)
    * Summary markdown: [`backend/reports/c44a47857fa45d1610c21c2cda83670c43272c11/20261017_001258/summary.md`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/c44a47857fa45d1610c21c2cda83670c43272c11/20261017_001258/summary.md)
  * Tested commit: `c44a47857fa45d1610c21c2cda83670c43272c11` (timestamp `20261017_001258`, cold benchmark, workspace `ws_fresh_benchmark`)
  * Test suite SHA256: `233bbd9dd19d02c0055453e9bcc40a878a8398bfd4a274b263b19dc11f194058` (`data/test.json`, `RAG_Evaluation_Suite_New_60_Q136_Q195`)
  * Corpus manifest SHA256: `ac193bc5bea7bfcc547e39924118fdd469385ceb528d14d5528f0464f74c959d`
  * Accuracy reconciliation (23 of 60 versus 24 of 60):
    * The machine artifact records report `overall_accuracy: 0.3833` in [`manifest.json`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/c44a47857fa45d1610c21c2cda83670c43272c11/20261017_001258/manifest.json) and `38.3%` in [`summary.md`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/c44a47857fa45d1610c21c2cda83670c43272c11/20261017_001258/summary.md).
    * Inspection of [`query_details.json`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/c44a47857fa45d1610c21c2cda83670c43272c11/20261017_001258/query_details.json) confirms exactly 23 passing queries where `is_correct` is true out of 60 questions (23 of 60 equals 0.383333).
    * The earlier mention of 24 of 60 (0.400) was an informal narrative count in preliminary notes that conflicted with the machine artifact.
    * The true canonical benchmark score recorded in the original baseline artifacts is 23 of 60 (0.3833). Offline regression results must not be substituted for historical benchmark evidence.
    * Per query diagnoses remain explicitly unverified unless verified by focused test.

* Executed Test Commands and Outcomes:
  * Multiyear coverage regressions: `uv run pytest tests/test_tablestore_multiyear_coverage_regressions.py` (9 passed in 10.50s, post review nit fix confirmed 9 passed in 12.74s).
  * Full 0004 test suite: `uv run pytest tests/test_tablestore_multiyear_coverage_regressions.py tests/test_benchmark_runner_and_persistence_0004.py tests/test_csv_vertical_slice_0004.py tests/test_dynamodb_boundary_and_execution.py tests/test_pdf_retrieval_slice_0004.py tests/test_structured_query_service.py tests/test_fast_path_fallback_regressions.py` (66 passed in 48.13s).
  * Live HTTP verification: `uv run python ../verify_0004_live.py` (17 of 17 passed).

* Independent Senior Code Review:
  * Reviewer model: `gemini-2.5-pro` (author on `gemini 3.8 flash`).
  * Scope: 38 files on branch `re-factor` vs `main`.
  * Verdict: Approve with nits (0 blockers, 0 majors, 0 minors, 1 nit).
  * Post review nit resolution: Aligned import style in `test_tablestore_multiyear_coverage_regressions.py` to direct package style.
  * Report: [`docs/reviews/2026-10-17-re-factor.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-17-re-factor.md).

* Scope and Status Boundaries:
  * Q153 (table binding and metric delta computation) is resolved with defensible source binding and ambiguity error handling.
  * Q159 (missing filter handling) remains a separate pending finding.
  * Specification 0004 remains locally Accepted.
  * Specification 0003 remains Proposed and deferred.
  * No claims of independent live AWS cloud readiness or benchmark accuracy improvements.
  * Paid canonical 60 question benchmark was not rerun.

## Table Binding Ambiguity Closure (October 18 2026)

* Base commit: `96d9d67c134cab831f05e921fb4a41e96030d28f`
* Working tree changes:
  * [backend/src/services/retrieval/structured_query_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/structured_query_service.py): Replaced candidate retry iteration loop with defensible source binding via `_bind_table`. Enforced completeness check on the bound candidate before execution: raises `IncompleteDataError` immediately without skipping to competing tables. In `_bind_table`, explicit table names bind directly, single candidates bind directly, and multiple candidates are scored via `_score_candidate_tables`. If top two candidates have equal positive scores, or score gap is under 2.0, or all candidates score zero, raises `AmbiguousBindingError`. Preserved explicit table and document scoping so explicit selection never switches tables upon filter failure.
  * [backend/tests/test_structured_query_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_structured_query_service.py): Added offline regressions covering two equally compatible tables raising `AmbiguousBindingError`, table insertion order invariance, incomplete intended source raising `IncompleteDataError` without fallback, Q153 table binding without filename selecting `survay` and computing 4,191 and 0.43%, explicit table binding retention on filter failure, and unresolved filter error across all candidates.
  * [backend/tests/test_pipeline_structured_path.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_pipeline_structured_path.py): Added fallback synthetic distractor table and verified structured pipeline execution without generative fallthrough.
  * [docs/specs/0004-verify.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0004-verify.md): Updated acceptance criteria verification records (72 core tests passed).
  * [docs/reviews/2026-10-18-re-factor.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-18-re-factor.md): Recorded senior code review findings (Approve with nits; 1 minor and 2 nits resolved).

* Table Binding Ambiguity and Defensible Binding Resolution:
  * Defensible binding before execution: Table binding is completed as a discrete step prior to completeness checks or query execution. Candidate retry loops that return the first candidate that executes successfully are removed.
  * Ambiguity rejection contract: When multiple tables remain plausibly compatible without sufficient disambiguation (equal positive scores or score gap under 2.0), the engine raises `AmbiguousBindingError`. The pipeline translates this into `executed_path="structured_aggregate_ambiguous"` and `status="clarification_needed"`.
  * Insertion order invariance: Listing or insertion order does not affect ambiguity outcomes. Equal ranked compatible tables raise ambiguity regardless of order.
  * Completeness enforcement: If the bound intended source is incomplete (`coverage_complete=False`), the engine raises `IncompleteDataError` on that table immediately and does not silently fall back to an unintended complete table.
  * Scoping preservation: Explicit table names or document filters bind only to the requested table and never switch to another table on filter failure.
  * Q153 resolution: For the query "For All industries (99999), how did total income change from 2024 to 2025? Give the amount and percentage change.", scoring assigns 36.0 points to `survay` and 2.0 points to distractor tables based on column matches and probe rows, binding `survay` deterministically and computing `4,191` and `0.43%`.

* Executed Test Commands and Outcomes:
  * Core 0004 test suite: `uv run pytest tests/test_structured_query_service.py tests/test_tablestore_multiyear_coverage_regressions.py tests/test_benchmark_runner_and_persistence_0004.py tests/test_csv_vertical_slice_0004.py tests/test_dynamodb_boundary_and_execution.py tests/test_pdf_retrieval_slice_0004.py tests/test_fast_path_fallback_regressions.py` (72 passed in 120.44s).
  * Pipeline structured path tests: `uv run pytest tests/test_pipeline_structured_path.py` (5 passed).
  * Live HTTP verification: `uv --directory backend run python ../verify_0004_live.py` (17 of 17 passed).

* Independent Senior Code Review:
  * Reviewer model: `gemini-2.5-pro` (author on `gemini 3.8 flash`).
  * Scope: 42 files on branch `re-factor` vs `main`.
  * Verdict: Approve with nits (0 blockers, 0 majors, 1 minor resolved, 2 nits resolved).
  * Resolved findings: Guarded probe matching against empty string inflation, applied word boundary matching for explicit table identifiers, and added fallback synthetic distractor table in pipeline tests.
  * Report: [`docs/reviews/2026-10-18-re-factor.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-18-re-factor.md).

* Remaining Limitations and Scope Boundaries:
  * Q153 table binding ambiguity is resolved.
  * Q159 missing filter handling remains a separate pending finding.
  * Isolated comparison workspace procedure for `ws_fresh_benchmark` remains pending; no in place mutation or deletion executed.
  * Specification 0004 remains locally Accepted.
  * Specification 0003 remains Proposed and deferred.
  * Paid canonical 60 question benchmark was not rerun.

## Structured Aggregate Filter Binding Repair (October 18 2026 / October 22 2026)

* Base commit: `5082b30754c9e525813c8fdbb124756ac4ac2904`
* Defect resolved: `_execute_aggregate` previously passed `predicates=[]` ignoring year and entity filters, computing unconstrained whole table totals when constrained aggregates were requested.
* Working tree changes:
  * [backend/src/services/retrieval/structured_query_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/structured_query_service.py):
    * Added `_find_target_metric_column`: identifies numeric metric columns with word boundary matching while excluding temporal columns.
    * Added `_extract_and_bind_predicates`: binds direct column value pairs (`where <col> is <val>`, `<col> = <val>`), four digit year constraints (using equality for single year and `PredicateOp.IN` for multiple years), and explicit entity requirements (`for <target>`, quoted phrases, parenthesized codes) against table schema and actual stored rows (probe rows first, then single pass store query).
    * Error handling / clarification contract: if an explicitly requested filter entity or unknown column cannot be resolved in schema or data, raises `UnresolvedFilterError` immediately; never silently computes an unconstrained whole table aggregate.
    * Preserves legitimate unfiltered aggregates: returns empty predicates when query requests no filters.
    * Distinguishes valid filter matching zero rows (`selection_count=0`, `result_value=Decimal(0)`) from binding failure (which raises `UnresolvedFilterError`).
    * Populates `predicate_columns` and `predicate_values` on `StructuredQueryResult` and citation evidence payloads.
    * Updated `_execute_min_max_range` to bind requested predicates and populate predicate evidence.
    * Updated `_execute_on_table` pattern 3 regex to match word boundaries for standard aggregates (`\b(?:total|sum|average|avg|count)\b`).
  * [backend/src/services/retrieval/planner.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/planner.py): Added `\b(?:total|sum|average|avg|count)\b` to `is_structured_candidate` in `route()`, routing table aggregate queries to the deterministic zero generation path.
  * [backend/tests/test_structured_query_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_structured_query_service.py): Added 6 offline regressions:
    * `test_filtered_and_whole_table_totals_differ_and_filtered_answer_correct`: verifies whole table sum produces 600.00 while filtered tech sum produces 300.00.
    * `test_multiple_requested_filters_applied_together`: verifies multiple filters (tech and 2024) apply conjunctively and produce 200.00.
    * `test_unsupported_filters_cannot_produce_whole_table_totals`: verifies unknown entities and unknown columns raise `UnresolvedFilterError` and never compute whole table aggregates.
    * `test_valid_filter_matching_zero_rows_distinguished_from_binding_failure`: verifies valid filters matching zero rows return count 0 and total 0 without error, preserving predicate evidence.
    * `test_result_evidence_records_applied_predicates_and_selected_rows`: verifies citation evidence records predicate columns, typed values, selection count, and row identifiers.
    * `test_multiple_years_bind_with_in_predicate`: verifies multi year filters bind with `PredicateOp.IN` and compute correct sums across multiple years.
  * [docs/specs/0004-verify.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0004-verify.md): Updated acceptance criteria verification records (78 core tests passed).
  * [docs/reviews/2026-10-18-re-factor.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-18-re-factor.md): Recorded senior code review findings (Approve with nits; all 3 minors and 1 nit resolved).

* Executed Test Commands and Outcomes:
  * Core 0004 test suite: `uv run pytest tests/test_csv_vertical_slice_0004.py tests/test_pdf_retrieval_slice_0004.py tests/test_structured_query_service.py tests/test_dynamodb_boundary_and_execution.py tests/test_benchmark_runner_and_persistence_0004.py tests/test_fast_path_fallback_regressions.py tests/test_tablestore_multiyear_coverage_regressions.py` (78 passed).
  * Structured query service tests: `uv run pytest tests/test_structured_query_service.py` (19 passed in 26.46s).
  * Pipeline structured path tests: `uv run pytest tests/test_pipeline_structured_path.py` (5 passed).
  * Live HTTP verification: `uv run --project backend python verify_0004_live.py` (17 of 17 passed).

* Independent Senior Code Review:
  * Reviewer model: `gemini-2.5-pro` (author on `gemini 3.8 flash`).
  * Scope: 42 files on branch `re-factor` vs `main`.
  * Verdict: Approve with nits (0 blockers, 0 majors, 1 minor resolved, 1 nit resolved).
  * Resolved findings:
    1. Replaced substring containment column matching in `_execute_metric_delta` with `_find_target_metric_column`, enforcing word boundary regular expressions and eliminating substring false positives on short column names. Also standardized word boundary regular expressions in `_execute_min_max_range`.
    2. Factored shared candidate phrase extraction and explicit filter requirement parsing into `_extract_candidate_filter_terms` and `_extract_explicit_filter_requirements`, eliminating over 80 lines of duplicated regex logic between `_execute_metric_delta` and `_extract_and_bind_predicates`.
    3. Added 2 new offline regressions: `test_metric_delta_word_boundary_column_matching` and `test_min_max_range_word_boundary_column_matching`. All 21 tests in `test_structured_query_service.py` pass and all 85 tests across 0004 suites pass.
  * Report: [`docs/reviews/2026-10-22-re-factor.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-22-re-factor.md).

* Scope Boundaries and Remaining State:
  * Q159 structured aggregate filter binding defect is resolved.
  * Q153 table binding ambiguity remains resolved.
  * Metric delta and min/max range column resolution word boundary matching is verified.
  * Candidate extraction duplication is eliminated.
  * Specification 0004 remains locally Accepted.
  * Specification 0003 remains Proposed and deferred.
  * No claims of independent live AWS cloud readiness or benchmark accuracy improvements.
  * Paid canonical 60 question benchmark was not rerun.

## Bounded Open Knowledge Framework (OKF) Integration and Slice 3.1 Hardening (Spec 0006)

* **Date**: November 15 2026
* **Branch**: `re-factor`
* **Final implementation commit**: `04cdc6938f9b18eaebb2b5921d49654b96f93e8f`
* **Review commit**: `e4307091b08b9e7d44289f2ad47f11b00f155369`
* **Status**: Specification 0006 locally Accepted; Specification 0003 Proposed and deferred; Specification 0007 Proposed and unimplemented.

### Overview

This milestone completes and hardens specification 0006 (Bounded Open Knowledge Framework Integration) across all layers:
1. Grounded entity extraction: typed `OkfEntityRecord` and `OkfPropertyAssertion` contracts per document version, verbatim chunk grounding validation for PDF text, and curated table schema extraction for CSV files with zero model calls and zero token expense.
2. Partitioned persistence: workspace scoped partition keys (`WORKSPACE#{workspace_id}`) and secondary indexes (`GSI1` for entity lookup, `GSI2` for readiness discovery) across SQLite, in memory, and DynamoDB Local stores, preventing cross tenant data leakage.
3. Durable budget ledger coordination (Slice 3.1): atomic reservations and token ceiling enforcement (`max_calls = 2`, `2000` input tokens per call ceiling, `4000` total input token ceiling, `600` output tokens per call ceiling, `1200` total output token ceiling, and 10 second timeout) in dedicated ledger items (`BUDGET#DOC#{doc}#VER#{ver}`).
4. Authoritative request token bounding: official Bedrock Runtime `count_tokens` API integration bounding converse requests before dispatch, failing closed on missing, malformed, or zero token responses.
5. Large publication staging: payloads exceeding 99 items stage entities and assertions into distinct attempt publication keys (`PUB#{pub_id}`) before committing visibility atomically via a two item transaction, ensuring staging failures leave the manifest uncommitted and unready.
6. Same version republication isolation: sort keys and query prefixes embed publication identifiers, and retrieval filters candidate assertions strictly by the active publication identifier discovered from the ready manifest, preventing stale records from diluting bounded query limits.
7. Publication authority and optimistic ordering: optimistic concurrency against base publication identifiers prevents stale unfenced attempts from replacing newer committed publications. Fenced publication requires existing ledger items, current fence tokens, unexpired leases, and mandatory non empty `worker_id` values.
8. SQLite publication authority across separate connections: `SQLiteOkfRepository.save_manifest` combines base publication validation and active budget ledger lease protections into a single atomic conditional SQL statement, preventing race conditions between query pre checks and manifest writes across separate database connections.
9. Ready PDF publication preservation: extraction attempts preserve existing ready manifests during process crashes, provider call timeouts, budget reconciliation errors, and storage write failures, keeping budget reservations durable separately in the budget ledger and ensuring prior ready assertions remain retrievable.
10. Soft candidate pooling: verified entity assertions pool softly into reciprocal rank fusion with a calibrated weight of 1.15 without hard exclusions or page pruning, while fast path queries bypass OKF lookups.

### Independent Senior Code Review

* Reviewer: Staff Reviewer on contrasting model `claude-3-5-sonnet` (commit [`e430709`](file:///home/swyra/projects/knowledge-retrieval-engine)).
* Scope: 5 changed files on branch `re-factor` (`backend/src/db/okf_repo.py`, `backend/tests/test_okf_slice3_1.py`, `docs/specs/0006-okf-integration.md`, `docs/specs/0006-verify.md`, `docs/reviews/2026-11-14-re-factor-okf-publication-races-resolved.md`).
* Verdict: Approve with nits (0 blockers, 0 majors, 0 minors, 2 nits).
* Findings resolution:
  * SQL condition documentation alignment: In [`docs/reviews/2026-11-14-re-factor-okf-publication-races-resolved.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-14-re-factor-okf-publication-races-resolved.md#L41-L42), aligned the summary SQL condition to reflect the exact implementation in `SQLiteOkfRepository.save_manifest` (`json_extract(data_json, '$.lease_owner_id') IS NOT NULL AND CAST(json_extract(data_json, '$.lease_expires_at') AS REAL) > ?`).
  * Test budget note: Documented that manual budget pre reservation consumes one call permit, which is valid for single chunk tests but requires accounting awareness for multi chunk tests.
* Review report: [`docs/reviews/2026-11-15-re-factor-0006-slice3-1-final-repairs.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-15-re-factor-0006-slice3-1-final-repairs.md).

### Verification Evidence and Test Suite Summary

* Focused Publication Authority and Preservation Tests:
  * Command: `cd backend && PYTHONPATH=src uv run pytest tests/test_okf_slice3_1.py -k "test_sqlite_publication_ordering_atomic_across_separate_connections or test_sqlite_unfenced_publication_ordering_blocked_by_concurrent_lease_across_separate_connections or test_lease_expiry_and_fence_advancement_during_publication_interval or test_preserve_ready_pdf_publication_during_new_attempt_failures" -v`
  * Outcome: 4 passed in 4.99s.
* Full OKF Slice 3.1 Suite:
  * Command: `cd backend && PYTHONPATH=src uv run pytest tests/test_okf_slice3_1.py -v`
  * Outcome: 57 passed in 10.38s.
* Complete OKF Regression Suite:
  * Command: `cd backend && PYTHONPATH=src uv run pytest tests/test_okf_slice1.py tests/test_okf_ablation_slice2.py tests/test_okf_correctness_regressions.py tests/test_okf_graph_contract.py tests/test_contracts_and_identity.py -v`
  * Outcome: 112 passed in 12.29s (19 in `test_okf_slice1.py`, 11 in `test_okf_ablation_slice2.py`, 29 in `test_okf_correctness_regressions.py`, 4 in `test_okf_graph_contract.py`, 49 in `test_contracts_and_identity.py`).
* Total OKF Test Coverage:
  * 169 passed, 0 failed, 0 skipped across all test suites in 27.66s.

### Operational Boundaries and Limitations

1. Local acceptance versus live AWS endpoints: All behaviors were verified using local SQLite with WAL mode, in memory repositories, and local Docker DynamoDB Local (`test_ddb_local` on port 8000). Live AWS Bedrock provider calls and live AWS DynamoDB cloud transactions remain unverified in local continuous integration without cloud credentials.
2. Delayed DynamoDB commit limitations: Client socket timeouts (such as a 40ms read timeout) terminate the client HTTP connection but do not cancel or bound server side transaction processing in DynamoDB. A client timeout produces an uncertain outcome that must be verified by a strongly consistent read. If a commit arrives after lease duration expires but before a competitor worker advances the fence, DynamoDB commits the write because condition checks compare client supplied timestamp constants. Strict rejection after lease expiration is guaranteed once another worker advances the monotonic fence token (`fence_token > expected_fence`).
3. Deferred DynamoDB record cleanup: Staged and superseded entity and assertion records remain inert in DynamoDB to maintain reader isolation without non atomic cross partition deletes; physical garbage collection remains deferred to background maintenance.

### Project Status and Next Task

* Specification 0006: Locally Accepted and marked done in [`docs/scope/scope.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/scope/scope.md).
* Specification 0003: Snapshot registry, publication, and atomic deletion remains Proposed and deferred.
* Specification 0007: Knowledge graph enrichment remains Proposed and unimplemented.
* Next Task: Review the existing specification 0007 design ([`docs/specs/0007-knowledge-graph-enrichment.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0007-knowledge-graph-enrichment.md)).
* Invariant reminder: Zero paid benchmarks run, zero re ingestion, zero re embedding, and historical benchmark artifacts preserved.

## Bounded Knowledge Graph Enrichment (0007 Slice 1)

* **Date**: November 16 2026
* **Branch**: `re-factor`
* **Final implementation commit**: `11bf828287f6e276317b0186e64745d40fb99c75`
* **Status**: Specification 0007 Slice 1 locally completed and reviewed: Approve with nits. Specification 0007 remains In Progress. Specification 0003 remains Proposed and deferred. Slices 2 and 3 remain unimplemented. `GRAPH_ENABLED=false` remains the default.

### Overview

This milestone completes and hardens Slice 1 of specification 0007 (Bounded Knowledge Graph Enrichment and Multi Hop Relationship Traversal) across all core layers:
1. Typed contracts and conservative entity identity: immutable Pydantic models for `KnowledgeGraphEntity`, `KnowledgeGraphRelationship`, `KnowledgeGraphManifest`, `KnowledgeGraphLedgerEntry`, and `KnowledgeGraphLookupRequest`. Default identity includes workspace, document, entity type, and normalized name. Name and type alone do not establish that two entities are identical, preventing homonym false joins across documents. Cross document identity requires a verified disambiguation token.
2. Partitioned persistence and publication staging isolation: workspace scoped partition keys (`WORKSPACE#{workspace_id}`) across memory, SQLite WAL, and DynamoDB Local stores. Staged records write under distinct attempt publication sort keys (`PUB#{pub_id}`) before committing visibility atomically via a two item transaction or atomic SQLite conditional commit, ensuring reader isolation and uncommitted attempt invisibility.
3. Deterministic CSV relationship extraction: directional schema extraction from verified foreign keys and primary keys with zero language model calls and zero token expense. Verbatim grounding validation enforces strict word boundary matching (`\b` + escaped entity + `\b`), rejecting reversed, negated, and arbitrary co occurrence claims.
4. Fail closed table evidence binding: evidence binding proves exact workspace, table, document, and version association using authoritative storage keys and metadata. Missing requested versions return no result without falling back to legacy rows or schemas. Missing rows, missing cells, or incomplete bindings fail closed with no candidate, eliminating substituted graph edge text for missing source data.
5. Bounded table hydration and chunk specific version provenance: targeted row and cell lookups use explicit limits (`LIMIT 1`) without full table scans or in memory searches. When targeted interfaces are absent, fallback behavior disables safely. Chunks require durable bindings between the exact chunk and requested version, rejecting unversioned chunks even if document level snapshots exist.
6. Bounded multi hop traversal: expansion limits depth to at most 2 hops with deterministic frontier sorting, cycle detection, and a 25 node ceiling. Active deadline budget checks halt traversal immediately if remaining time expires.
7. Targeted chunk hydration: resolves at most 15 chunks via exact point lookups (`DOC#{did}`, `CHUNK#{cid}`) through `DocumentsRepository.get_chunks_by_ids`, completely eliminating calls to `get_all_chunks`.
8. Concurrency admission and capacity retention: `GraphConcurrencyController` bounds worker concurrency to at most 4 workers and 8 queued requests, retaining admission capacity until underlying storage calls finish. Dynamic source document and workspace deletion tombstones halt traversal and hydration immediately fail closed.

### Independent Senior Code Review

* Reviewer: Gemini Pro (author on Gemini Flash)
* Scope: 19 files on branch `re-factor` vs base commit `0c6bf35`
* Verdict: Approve with nits (0 blockers, 0 majors, 0 minors, 3 nits)
* Review report: [`docs/reviews/2026-11-16-re-factor-0007-slice1-review.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-16-re-factor-0007-slice1-review.md)
* Disposition of the three review nits:
  1. `backend/src/db/knowledge_graph_repo.py:540` (`WORKSPACE_SQLITE_PATH` fallback for `GRAPH_SQLITE_PATH`): Harmless in local development; flagged for cleaner dedicated configuration separation during Slice 2.
  2. `backend/src/services/retrieval/knowledge_graph_retriever.py:746` (TableStore row evidence hydration loop deadline check): The TableStore evidence hydration loop lacks an inner deadline check, deferred to Slice 2. While caller timeout protection guards the caller from waiting indefinitely, it does not stop underlying execution or storage work once started.
  3. `backend/src/db/knowledge_graph_repo.py:982` (hardcoded dummy credentials `aws_access_key_id="test"` for DynamoDB Local): Standard local test convention; live AWS execution uses IAM roles without static keys. Clarifying comments will be maintained.

### Verification Evidence and Test Suite Summary

* Focused Knowledge Graph Slice 1 Suite:
  * Command: `cd backend && uv run pytest tests/test_knowledge_graph_slice1.py -v`
  * Outcome: 65 passed in 3.99s.
  * Coverage: Typed contracts, conservative entity identity, publication isolated graph storage, deterministic CSV relationships from verified directional schema, deeply immutable request local document bindings, bounded traversal, targeted chunk hydration with grounded TableStore row and cell locators, dynamic source and workspace tombstone enforcement, deadline and concurrency retention, separate connection SQLite publication race and pause atomicity, DynamoDB local atomic authority fencing validation and rejection, chunk specific version provenance with fail closed rejection of unversioned chunks, fail closed missing table row and cell evidence without edge text substitution, bounded targeted table reads without full table scans, safe fallback disabling when targeted interfaces are absent, absent requested version returning None without legacy substitution across Memory, SQLite, and DynamoDB Local, incomplete manifest and schema identity failing closed, correct document with wrong table or version failing closed, builder to retriever output containing no falsely versioned evidence, candidate entity mention lookup resolving entities past 30 alphabetical items, test environment factory isolation, and word boundary grounding validation.
* Live FastAPI Integration Verification:
  * Command: `cd backend && uv run python ../verify_0007_live.py`
  * Outcome: 18 passed live over HTTP against local Uvicorn server on port 8001.
* Regression Test Suites:
  * Command: `python verify_0004_live.py` (17 passed live over HTTP).
  * Command: `python verify_0006_live.py` (15 passed live over HTTP).
  * Command: `cd backend && uv run pytest tests/test_table_store.py tests/test_tablestore_multiyear_coverage_regressions.py tests/test_csv_ingestion_complete.py tests/test_csv_vertical_slice_0004.py tests/test_deterministic_executor.py tests/test_contracts_and_identity.py tests/test_workspace_boundary_isolation.py tests/test_structured_query_service.py` (128 passed in 19.82s).

### Operational Boundaries and Limitations

1. Local acceptance versus live AWS endpoints: All behaviors were verified using local SQLite with WAL mode, in memory repositories, and local Docker DynamoDB Local (`test_ddb_local` on port 8000). Live AWS Bedrock provider calls and live AWS DynamoDB cloud transactions remain unverified in local continuous integration without cloud credentials.
2. Inactive retrieval integration: `GRAPH_ENABLED=false` remains the default configuration. Query retrieval pipeline routing and reciprocal rank fusion candidate pooling remain inactive until Slice 3.
3. Unimplemented Slices: Slice 2 (LLM entity and relationship extraction, Bedrock budgeting ledger, and verbatim grounding validation) and Slice 3 (Pipeline reciprocal rank fusion, snapshot metadata, telemetry trace, and offline ablation) remain unimplemented.
4. Deferred specifications: Specification 0003 (Snapshot registry, publication, and atomic deletion) remains Proposed and deferred. Specification 0007 remains In Progress.

### Project Status and Subsequent Slices Completion

* Specification 0007: Slice 1 is complete and verified.

## Bounded Knowledge Graph Enrichment (0007 Final Acceptance and Reconciliation)

* **Date**: November 18 2026
* **Branch**: `re-factor`
* **Final implementation commit**: `029ff20c789fb4aea24e80592efcb04f1c2dd103`
* **Status**: Specification 0007 Slices 1, 2, and 3 fully completed, verified, and approved. Implementation is complete. Specification 0003 remains Proposed and deferred. `GRAPH_ENABLED=false` preserved by default.

### Overview

This milestone records the full completion and acceptance of specification 0007 (Bounded Knowledge Graph Enrichment and Multi Hop Relationship Traversal) across all three slices:
1. **Slice 1 (Deterministic Tabular Graph and Traversal)**: Partitioned persistence across Memory, SQLite WAL, and DynamoDB Local stores. Deterministic CSV relationship extraction from foreign keys and primary keys. Deeply immutable PinnedSnapshotContext request local document bindings. Bounded 2 hop multi hop traversal with 25 node ceiling. Targeted chunk hydration with zero calls to get_all_chunks. Dynamic source and workspace deletion tombstones failing closed. GraphConcurrencyController with 4 workers and 8 queue slots.
2. **Slice 2 (Narrative PDF Extraction and Token Budgeting)**: Verbatim directional syntax grounding, rejecting reversed voice, sentence negation, disjoint clauses, and co occurrence alone. Source context preservation rejecting stripped conditional, negated, or reported speech framing. Syntactic binding respecting corporate abbreviations, numeric decimals, modal auxiliary verbs, and modal passive constructions. Bedrock token budgeting on BUDGET#LEDGER with 15 second worker leases, monotonic fencing, CountTokens validation, per call caps (2000 prompt tokens, 800 output tokens), and cumulative document ceilings (4000 prompt tokens, 1600 output tokens, 5600 total tokens).
3. **Slice 3 (Query Pipeline Fusion, TableStore Wiring, and Ablation Harness)**:
   * Query pipeline integration: `run_graph` node wired into `langgraph_pipeline.py` after OKF and before reranking. Soft candidate pooling into reciprocal rank fusion with calibrated weights (1.10, halved to 0.55 on ambiguity or conflict). Fast path query bypass.
   * Decoupled baseline ingestion: Asynchronous background enrichment pass per ADR-001 with DocumentSnapshot graph metadata. Baseline documents remain available and retrievable if graph enrichment fails or times out.
   * Production ingestion wiring: Exact version CSV schema authority resolving relations only from authoritative TableStore schema records. Missing provider status recording `unavailable` or `failed` (never a false empty). Distinguishes legitimate empty publications from historical stubs, providing bounded safe recovery via `recover_stub_manifest`.
   * Pipeline TableStore wiring: `run_graph` resolves shared TableStore via `get_shared_table_store()` and injects it into KnowledgeGraphRetriever, hydrating stored table cells into Chunk objects that survive into retained evidence items with strategy `knowledge_graph`.
   * Exception fallback contract enforcement: `run_graph` catches expand exceptions cleanly and sets valid `storage_failed` status without raising Pydantic validation errors.
   * Offline ablation comparison harness: 34 tests in `test_knowledge_graph_ablation_slice3.py` verifying recall, precision, latency, and counterexample safety (an incorrect graph expansion never displaces factual baseline chunks).

### Reconciled Verification Evidence

* Newly Executed Runs (DynamoDB Local Active on Port 8000):
  * Slice 1 (`tests/test_knowledge_graph_slice1.py`): 65 passed, 0 skipped in 4.57s
  * Slice 2 (`tests/test_knowledge_graph_slice2.py`): 64 passed, 0 skipped in 4.96s
  * Slice 3 (`tests/test_knowledge_graph_ablation_slice3.py`): 34 passed, 0 skipped in 12.44s
  * Boundary & CSV (`tests/test_workspace_boundary_isolation.py` & `tests/test_csv_ingestion_complete.py`): 32 passed, 0 skipped in 11.49s
  * OKF & TableStore (`tests/test_okf_slice3_1.py` & `tests/test_table_store.py`): 63 passed, 0 skipped in 11.14s
  * Total newly executed across KG + Boundary/CSV: **195 passed, 0 skipped** (65 + 64 + 34 + 32).
  * Total combined: **258 passed, 0 skipped** (195 + 63).
* Historical Runs (DynamoDB Local Daemon Offline):
  * Combined regression total: 186 passed, 34 skipped (with 28 Slice 3 tests) or 192 passed, 34 skipped (with 34 Slice 3 tests).
  * 8 tests skipped in Slices 1 and 2 when daemon was offline (producing 62 passed in Slice 1 and 59 passed in Slice 2; 62 + 59 = 121 passed, 8 skipped).
* Live HTTP Verification:
  * 18 passed live over HTTP in `verify_0007_live.py` (18 of 18).

### Operational Boundaries and Limitations

1. **Implementation Acceptance versus Live Deployment and Benchmark Readiness**:
   * Implementation is complete and locally accepted.
   * Live AWS deployment and production cloud readiness remain unverified; local verification ran on SQLite WAL, in memory stores, and containerized DynamoDB Local (port 8000). Zero calls were made to live AWS Bedrock or live AWS DynamoDB.
   * Benchmark accuracy readiness is guarded by `validate_benchmark_comparison_preconditions`, blocking claims on non canonical workspaces. Paid benchmark execution on `ws_canonical_comparison_0004` was not run and requires explicit authorization.
2. **Configuration Invariant**: `GRAPH_ENABLED=false` remains preserved by default.
3. **Deferred Specifications**: Specification 0003 (Snapshot registry, publication, and atomic deletion) remains Proposed and deferred.
4. **Preserved Artifacts**: Historical benchmark workspaces (`ws_fresh_benchmark`), ground truth, and comparison artifacts preserved untouched. Zero paid provider calls, zero corpus re ingestion, zero re embedding.

