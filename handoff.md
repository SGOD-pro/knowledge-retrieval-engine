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

* Base commit: `d665e4afb7d53ca9ea6ed993741c4f3813f7d3be`
* Working tree changes:
  * [backend/src/services/langgraph_pipeline.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py): Implemented `escalate_to_full_retrieval` node, transitioning failed fast extractions to genuine full retrieval (`fast_path=False`, `top_k=40`, candidate scoping, 1024 dimension embedding), refreshing candidate dependent state to prevent stale fast candidate contamination. Empty context in fidelity returns structured refusal. Fixed Q167 `UnboundLocalError`.
  * [backend/src/providers/embedding_provider.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/providers/embedding_provider.py): Strictly test scoped fallback to deterministic vector in test environment.
  * [backend/tests/test_fast_path_fallback_regressions.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_fast_path_fallback_regressions.py): Added 10 passing regressions covering Q167 repair, full escalation routing, adversarial evidence recovery outside fast top 10, cross workspace exclusion, source version isolation, zero generation on fast extraction, and single generation on escalation.
  * [backend/tests/test_pipeline_structured_path.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_pipeline_structured_path.py): Guaranteed manifest coverage complete idempotency across test executions.
  * [docs/specs/0004-verify.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0004-verify.md): Recorded fast path fallback regression verification steps and execution environment distinctions.
  * [docs/reviews/2026-10-17-re-factor.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-17-re-factor.md): Independent senior review report.

* Executed Commands and Verification Outcomes:
  * Fast path fallback regressions: `uv run pytest tests/test_fast_path_fallback_regressions.py` (10 passed in 6.14s).
  * Core 0004 suite: `uv run pytest tests/test_benchmark_runner_and_persistence_0004.py tests/test_csv_vertical_slice_0004.py tests/test_dynamodb_boundary_and_execution.py tests/test_pdf_retrieval_slice_0004.py tests/test_structured_query_service.py tests/test_fast_path_fallback_regressions.py` (54 passed in 37.70s).
  * Repository wide pytest suite: `uv run pytest` (376 passed, 3 documented skips, 0 failed in 49.33s).
  * Live HTTP checks: 17 passed in `verify_0004_live.py`.

* Independent Code Review and Post Review Nit Fixes:
  * Model: `gemini-2.5-pro` (author on `gemini 3.8 flash`).
  * Scope: 37 files on branch `re-factor` vs `main`.
  * Verdict: Approve with nits (0 blockers, 0 majors, 0 minors, 2 nits resolved).
  * Report: [docs/reviews/2026-10-17-re-factor.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-17-re-factor.md).
  * Nit 1 resolved: Consolidated duplicate `replace` import in `backend/src/services/langgraph_pipeline.py:894`.
  * Nit 2 resolved: Removed redundant `from config import settings` import in `backend/src/providers/embedding_provider.py:124`.

* Embedding Compatibility Distinction:
  * In `escalate_to_full_retrieval`, vector length check validates the 1024 vector dimension, rejecting 384 dimension embeddings.
  * Floating point vectors in state carry no attached model or provider metadata envelope attributes.
  * Dimension validation alone cannot establish model or provider provenance.
  * Model provenance compatibility remains unresolved in code rather than verified.

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

