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
  * Q153 (table binding and year delta column mapping) remains a separate pending finding.
  * Q159 (missing filter handling) remains a separate pending finding.
  * Specification 0004 remains locally Accepted.
  * Specification 0003 remains Proposed and deferred.
  * No claims of independent live AWS cloud readiness or benchmark accuracy improvements.
  * Paid canonical 60 question benchmark was not rerun.


