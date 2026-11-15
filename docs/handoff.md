# Handoff: Core Engine Walking Skeleton and Benchmarks (0004)

**Date**: October 16 2026
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

* [backend/src/schemas/contracts/boundary.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/boundary.py): Added `DocumentSnapshot` and `BenchmarkRunMetadata` models, declared `token_usage`, `execution_type`, `generation_calls`, `result_value`, `secondary_value`, `operator`, and `structured_payload` on `QueryExecutionOutcome` and `CitationItem`, and documented counting units.
* [backend/src/api/boundary_adapters.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/api/boundary_adapters.py): Traced observed token usage, execution telemetry, and structured execution payloads from `query_service` into `QueryExecutionOutcome` and `CitationItem` without undeclared fields, removed query keyword assumptions from deterministic zero token evaluation, preserved missing generation telemetry as unknown, and implemented `execute_query` delegation.
* [backend/src/db/workspace_boundary_repo.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/workspace_boundary_repo.py): Added snapshot and benchmark metadata CRUD across SQLite, in memory, and DynamoDB repository implementations.
* [backend/src/services/benchmark/artifact_store.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/benchmark/artifact_store.py): Implemented `S3BenchmarkArtifactStore` uploading full versioned JSON benchmark reports.
* [backend/src/services/benchmark/persistence_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/benchmark/persistence_service.py): Implemented `BenchmarkPersistenceService` ensuring recoverable write sequencing (S3 first, then DynamoDB).
* [backend/src/services/benchmark/benchmark_runner.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/benchmark/benchmark_runner.py): Implemented `BenchmarkRunner` with adversarial scoring protection:
  * Infrastructure ERROR must fail partial coverage correctness and coverage scoring; credit is given only to agreed explicit coverage refusal.
  * Claim support is validated using existing evidence verification (`compute_faithfulness` and numeric grounding); matching citation identifiers alone do not earn support credit.
  * Numeric evidence acceptance replaces any number overlap; all claimed metric amounts must be grounded in cited evidence, and matching years with contradicting amounts fail evidence support.
  * Operation specific aggregate evidence support validation: validates claimed aggregate answers against their operation specific computed results (`result_value` and `secondary_value`). Selection count, total rows, predicate years, or unrelated snippet numbers are not treated as interchangeable with `result_value`. Valid count and min/max results are preserved using their corresponding computed values.
  * Complete dataset aggregates pass coverage scoring; incomplete dataset complete claims fail coverage scoring.
  * Token accounting traces provider tokens when available, prefers observed usage over query type or query keyword assumptions, preserves absent token usage and absent generation telemetry as unavailable rather than synthesized zero, reports zero only when execution telemetry explicitly establishes zero model generation (`generation_calls == 0`), and explicitly distinguishes estimated vs unavailable tokens.
* [backend/src/providers/llm_provider.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/providers/llm_provider.py): Explicit generic test double extracting grounded context excerpts without domain or PDF-specific logic.
* [backend/tests/test_benchmark_runner_and_persistence_0004.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_benchmark_runner_and_persistence_0004.py): 18 unit and integration tests covering S3 artifact storage, DynamoDB persistence, recoverable write ordering, orphan S3 tolerance, token tracing without undeclared fields, boundary contract declarations and adapter mapping edge cases, operation specific aggregate computed result validation (selection count 250 with sum 1000 where "sum is 250" fails and "sum is 1000" passes, preserving count and min/max results), contradictory aggregate answer failure, aggregate query with missing usage and missing telemetry remaining unavailable, absent token preservation, and focused adversarial scoring regression cases.
* [backend/tests/test_pdf_retrieval_slice_0004.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_pdf_retrieval_slice_0004.py): PDF verification labeled as mocked generation verifying pipeline grounding and DocumentLocation citations through generic test double.
* [verify_0004_live.py](file:///home/swyra/projects/knowledge-retrieval-engine/verify_0004_live.py): 17 live HTTP integration checks driving real workspace creation, 250 row calculation, boundary denial, tombstoning, and factual PDF extraction against the running FastAPI service.

## Execution Environment Distinctions

* Local HTTP checks: 17 live end to end requests executed over HTTP against local Uvicorn FastAPI server on port 8001 in `verify_0004_live.py`, verifying real document ingestion, 250 row calculation without truncation, cross workspace denial, and factual citation extraction.
* Mocks and mocked generation: isolated LLM completion generation in unit tests via explicit generic test double without domain or PDF-specific logic (`MODEL_PROVIDER=test`), mock S3 artifact store, and mock DynamoDB table items in isolated repository unit tests; these checks verify pipeline contracts and do not assert real model accuracy.
* Real model execution: local embedding computation (MiniLM-L6-v2 384d and BGE 1024d) and local BM25 ranking run real local algorithm execution; remote AWS Bedrock invocations bypassed during automated tests.
* Live AWS verification: deferred; DynamoDB and S3 interactions verified through local SQLite, in memory substitutes, and botocore client mock structures; no active AWS cloud deployment performed.

## Checks Run and Verification Evidence

* Tested State: Working tree on branch `re-factor`, base commit `8161e8d` (`8161e8dbf0bc54fe95eeb94290ff89e31700edd9`)
* Uncommitted Files in Working Tree:
  * `.gitignore`
  * `backend/AGENTS.md`
  * `backend/data/workspace_boundary.db`
  * `backend/src/api/boundary_adapters.py`
  * `backend/src/schemas/contracts/boundary.py`
  * `backend/src/services/benchmark/benchmark_runner.py`
  * `backend/tests/test_benchmark_runner_and_persistence_0004.py`
  * `docs/handoff.md`
  * `docs/reviews/2026-10-16-re-factor.md`
  * `docs/specs/0004-verify.md`
  * `verify_0004_live.py`
* Full 0004 Test Suite:
  * Command: `uv run pytest tests/test_csv_vertical_slice_0004.py tests/test_pdf_retrieval_slice_0004.py tests/test_structured_query_service.py tests/test_fix1_async_ingestion.py tests/test_dynamodb_boundary_and_execution.py tests/test_benchmark_runner_and_persistence_0004.py`
  * Outcome: 46 passed, 0 failed in 24.62s.
* Full Repository Test Suite:
  * Command: `uv run pytest`
  * Outcome: 366 passed, 0 failed, 3 skipped in 74.96s.
  * Skipped Test Reasons:
    1. `tests/test_api_endpoints.py:test_workspaces_crud_endpoints`: marked skip because it was obsoleted by specification 0002 boundary tests in `test_workspace_boundary_isolation.py`.
    2. `tests/test_end_to_end_structured.py:test_localstack_dynamodb_e2e`: marked skip because the local LocalStack emulator on port 4566 is unavailable in the automated test environment.
    3. `tests/test_fix5_workspace_isolation_and_deletion.py:test_workspace_deletion_and_isolation`: marked skip because it was obsoleted by specification 0002 boundary tests in `test_workspace_boundary_isolation.py`.
* Live FastAPI Integration Verification:
  * Command: `uv run --project backend python verify_0004_live.py`
  * Outcome: All behaviors passed (17 of 17).
* Independent Senior Code Review:
  * Model: `gemini-2.5-pro` (author on `gemini 3.8 flash`)
  * Scope: 33 changed files across branch `re-factor` vs `main`
  * Verdict: Approve with nits (0 blockers, 0 majors, 0 minors, 1 nit resolved in `.gitignore`)
  * Report: [docs/reviews/2026-10-16-re-factor.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-16-re-factor.md)
* Canonical 60 Question Baseline Command:
  * Command: `cd backend && uv run python scripts/run_canonical_60_benchmark.py --workspace-id ws_fresh_benchmark` (add `--allow-dirty` when evaluating a modified working tree).

## Fast Path Fallback Resolution and Q167 Fix (October 17 2026)

### Core Changes

* Resolved Q167 `UnboundLocalError`: defined `existing = state.get("stage_timings", {})` at the top of [`end_fast_path`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L689) before verified fact returns.
* Removed arbitrary first chunk and first CSV row fallback: removed arbitrary selection of first non heading chunk and fabricated confidence. When fast path sentence extraction fails to find a supported answer, it refuses to fabricate an answer and marks `executed_path = "full"`.
* Escalated to full retrieval path: added [`route_after_fast_path`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L894) conditional routing edge from `end_fast_path`. Supported fast path extractions terminate at `END`, while unsupported attempts route into `run_okf_router_post` for full candidate retrieval, neural reranking, context compression, fidelity scoring, and at most one model generation.
* Structured refusal on insufficient evidence: when the full path also lacks evidence, returns structured HTTP 200 refusal with empty citations and zero confidence.
* Accurate pipeline telemetry: derived `self.fast_path = (self.executed_path == "fast")` in [`Pipeline.run`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L1139) to preserve true execution path telemetry.

### Focused Regressions in [`backend/tests/test_fast_path_fallback_regressions.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_fast_path_fallback_regressions.py)

* `test_q167_no_longer_crashes_with_unbound_local_error`: verifies verified fact extraction completes cleanly on fast path without error.
* `test_failed_extraction_cannot_return_unrelated_first_csv_row`: verifies unsupported extraction rejects unrelated first CSV rows and escalates.
* `test_escalation_executes_existing_full_retrieval_path`: verifies escalation runs full retrieval and executes exactly one model generation call.
* `test_supported_fast_extraction_still_works_without_generation`: verifies supported fast extraction succeeds with zero model generation calls and zero tokens.
* `test_unsupported_evidence_produces_refusal_with_empty_citations`: verifies query service returns structured refusal and empty citations when neither path finds evidence.

### Separate Pending Findings (Not Claimed Resolved)

* Q152 TableStore coverage: `survay.csv` in TableStore holds only 5000 rows (all year 2025), missing years 2013 through 2024 present in the raw corpus.
* Q153 router misbinding: question text lacks explicit table name "survay", causing router to misbind to `rs_status_bill_passed_assent_1952_2016` where filter columns fail.

### Verification Evidence

* Tested Commit: `c44a478` (`c44a47857fa45d1610c21c2cda83670c43272c11`) on branch `re-factor`
* Uncommitted files in working tree:
  * [`backend/src/services/langgraph_pipeline.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py)
  * [`backend/tests/test_fix4_fast_path_confidence.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_fix4_fast_path_confidence.py)
  * [`backend/tests/test_fast_path_fallback_regressions.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_fast_path_fallback_regressions.py)
  * [`docs/handoff.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/handoff.md)
* Focused Regressions:
  * Command: `uv run pytest tests/test_fast_path_fallback_regressions.py`
  * Outcome: 5 passed in 3.94s.
* Fast Path plus 0004 Test Suite:
  * Command: `uv run pytest tests/test_fast_path_fallback_regressions.py tests/test_benchmark_runner_and_persistence_0004.py tests/test_csv_vertical_slice_0004.py tests/test_pdf_retrieval_slice_0004.py tests/test_structured_query_service.py tests/test_dynamodb_boundary_and_execution.py tests/test_fix4_fast_path_confidence.py`
  * Outcome: 50 passed in 32.68s.
* Full Repository Test Suite:
  * Command: `uv run pytest`
  * Outcome: 371 passed, 0 failed, 3 skipped in 358.46s.
  * Skipped Test Reasons:
    1. `tests/test_api_endpoints.py:test_workspaces_crud_endpoints`: marked skip because it was obsoleted by specification 0002 boundary tests in `test_workspace_boundary_isolation.py`.
    2. `tests/test_end_to_end_structured.py:test_localstack_dynamodb_e2e`: marked skip because the local LocalStack emulator on port 4566 is unavailable in the automated test environment.
    3. `tests/test_fix5_workspace_isolation_and_deletion.py:test_workspace_deletion_and_isolation`: marked skip because it was obsoleted by specification 0002 boundary tests in `test_workspace_boundary_isolation.py`.

## Preserved Status and Deferred Work

* Specification 0003: Snapshot registry, publication, and atomic deletion remains Proposed and deferred.
* Specification 0004: Core engine walking skeleton and benchmarks is fully accepted and closed with all checks passing.
* Paid benchmark evaluation runs were not re executed during this slice.

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
1. Typed contracts and conservative entity identity: immutable Pydantic models for `KnowledgeGraphEntity`, `KnowledgeGraphRelationship`, `KnowledgeGraphManifest`, `KnowledgeGraphLedgerEntry`, and `KnowledgeGraphLookupRequest`. Exact identity hashing combines entity type and canonical name, separating distinct entities while resolving case insensitive homonyms.
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
  2. `backend/src/services/retrieval/knowledge_graph_retriever.py:746` (TableStore row evidence hydration loop deadline check): Individual row lookups take under 1 millisecond, so the loop rarely risks deadline overrun; an explicit deadline check inside the synthesis loop is scheduled for Slice 2.
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

### Project Status and Bounded Next Slice 2 Task

* Specification 0007: Slice 1 is complete and verified. Specification 0007 as a whole remains In Progress.
* Specification 0003: Snapshot registry, publication, and atomic deletion remains Proposed and deferred.
* Invariant reminder: Zero paid benchmarks run, zero re ingestion, zero re embedding, and historical benchmark artifacts preserved.
* Bounded Next Slice 2 Task:
  1. Implement Bedrock Claude 3 Haiku converse prompt templates and output parsers in `KnowledgeGraphBuilder`, extracting canonical entity relationship triples from PDF text chunks with verbatim directional syntax validation (AC-1, AC-2, AC-3).
  2. Implement durable budget accounting, fencing, and mandatory Bedrock `count_tokens` validation on `BUDGET#LEDGER`, with 15 second lease expiries, monotonic fence tokens, mandatory non empty worker identity (`worker_id`), and stale worker fencing (AC-3, AC-4, AC-5).
  3. Implement Slice 2 unit and regression test suite in `backend/tests/test_knowledge_graph_slice2.py` verifying PDF extraction, verbatim grounding, Bedrock count_tokens validation, budget ledger leasing, stale worker fencing, crash recovery, and throttling retry ceilings (AC-2, AC-3, AC-4, AC-5).
