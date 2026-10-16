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

## Preserved Status and Deferred Work

* Specification 0003: Snapshot registry, publication, and atomic deletion remains Proposed and deferred.
* Specification 0004: Core engine walking skeleton and benchmarks is fully accepted and closed with all checks passing.