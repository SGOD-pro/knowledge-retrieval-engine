# Handoff: Core Engine Walking Skeleton Ingestion and Retrieval Slices (0004)

**Date**: October 15 2026
**Branch**: re-factor
**Status**: 0004 In Progress (CSV and PDF Retrieval Slices Complete; Review Approved with Nits; Benchmarks Runner Pending)

## Overview

This document records the completion of both the CSV and text PDF vertical slices for specification 0004, along with the resolution of code review findings from the independent senior review.
We implemented real CSV and text PDF ingestion through the application API, persisted dataset and source version provenance in `DocumentSnapshot`, executed restricted deterministic aggregations over complete datasets without row truncation, wired narrative PDF chunk retrieval with typed `DocumentLocation` citations, enforced incomplete coverage barriers, verified workspace isolation, and resolved all blockers and major review findings.
Specification 0003 remains Proposed and deferred.
Specification 0004 remains in progress and is not marked complete until the reproducible benchmark runner is delivered.

## Working Tree State and Changed Files

The working tree incorporates the following modifications:

* [backend/src/schemas/contracts/boundary.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/boundary.py): Added [DocumentSnapshot](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/boundary.py#L92-L106) recording workspace identification, document identifier, source name, content version, format, incomplete coverage flag, exclusions, persisted rows count, and total rows count.
* [backend/src/schemas/contracts/query.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/query.py): Added `target_source_version` to [QueryContract](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/query.py#L240-L255) for explicit source pinning.
* [backend/src/schemas/contracts/evidence.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/evidence.py): Added `exclusion_flags` to [CanonicalEvidence](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/evidence.py#L56-L75).
* [backend/src/schemas/contracts/capability.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/capability.py): Added `structured_operations` to [CapabilityArtifact](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/capability.py#L185-L205).
* [backend/src/db/workspace_boundary_repo.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/workspace_boundary_repo.py): Added `save_document_snapshot`, `get_document_snapshot`, and `list_document_snapshots` across `InMemoryWorkspaceBoundaryRepository`, `SQLiteWorkspaceBoundaryRepository`, and `DynamoDBWorkspaceBoundaryRepository`.
* [backend/src/ingestion/csv_table_ingester.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/csv_table_ingester.py): Validated row column count against schema, tracked rejections, set `coverage_complete = (processing_finished and (rejected_rows == 0))`, and supported human readable `table_name` override from document filename.
* [backend/src/ingestion/adapters/pdf_adapter.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/adapters/pdf_adapter.py): Extracted PDF pages and metadata with fallback extraction, tracking skipped or unreadable pages into exclusions, and guaranteed cleanup of temporary S3 PDF extraction artifacts in a finally block.
* [backend/src/ingestion/parse_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/parse_service.py): Passed filename stem to table store ingestion for deterministic table discovery and readable citation locations.
* [backend/src/modules/documents/documents_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/modules/documents/documents_service.py): Created and saved [DocumentSnapshot](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/boundary.py#L92-L106) upon ingestion completion, registered sources in boundary repository, and verified content hash equality before skipping reingestion on existing documents.
* [backend/src/modules/documents/documents_repository.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/modules/documents/documents_repository.py): Confined in memory test dictionaries strictly within test environment checks to prevent production memory leaks, and raised explicit exceptions upon exhausted Qdrant retries.
* [backend/src/providers/embedding_provider.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/providers/embedding_provider.py): Removed broad exception swallowing in `embed_batch` and `embed_text` in adherence to architectural rule AE2.
* [backend/src/api/boundary_adapters.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/api/boundary_adapters.py): Targeted tombstone checks to specific candidate tables and source names using word boundary regex matching, and mapped storage failures cleanly.
* [backend/src/services/cache_delivery.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/cache_delivery.py): Persisted successful recomputed query outcomes into `cache_store` under canonical cache key descriptors with integer source versions and timestamps.
* [backend/src/modules/query/query_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/modules/query/query_service.py): Filtered tombstoned document IDs directly using `has_tombstone` to prevent deleted documents from remaining queryable.
* [backend/tests/test_csv_vertical_slice_0004.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_csv_vertical_slice_0004.py): Verified real CSV ingestion, 250 row un-truncated dataset aggregation, incomplete coverage barrier (HTTP 422), cross workspace access denial (HTTP 404), deleted source denial, and storage failure distinction (HTTP 503).
* [backend/tests/test_pdf_retrieval_slice_0004.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_pdf_retrieval_slice_0004.py): Verified real text PDF ingestion, DocumentSnapshot provenance, page level chunk citations, corrupt page exclusion handling, and workspace boundary enforcement.
* [verify_0004_live.py](file:///home/swyra/projects/knowledge-retrieval-engine/verify_0004_live.py): End to end live verification script driving 17 integration checks against the live running FastAPI server.
* [docs/reviews/2026-10-14-re-factor.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-14-re-factor.md): Independent senior code review report with status Approved with nits.

## Checks Run and Verification Evidence

We ran the complete suite of tests and live service verification:

* Independent Senior Code Review:
  * Verdict: Approve with nits (0 blockers, 0 majors, 2 minors, 2 nits).
  * Report: [docs/reviews/2026-10-14-re-factor.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-10-14-re-factor.md).

* Full Backend Test Suite:
  * Command: `backend/.venv/bin/pytest backend/tests/`
  * Outcome: 348 passed, 3 skipped, 2 warnings in 203 seconds. Zero failures across all 351 test cases.

* Targeted Vertical Slice Suite:
  * Command: `backend/.venv/bin/pytest backend/tests/test_csv_vertical_slice_0004.py backend/tests/test_pdf_retrieval_slice_0004.py backend/tests/test_workspace_boundary_isolation.py backend/tests/test_embedding_integrity.py backend/tests/test_no_in_memory_bypass.py backend/tests/test_dynamodb_boundary_and_execution.py backend/tests/test_structured_query_service.py`
  * Outcome: 57 passed in 26 seconds.

* Live FastAPI Integration Verification:
  * Command: `backend/.venv/bin/python verify_0004_live.py`
  * Outcome: All behaviors passed (17 of 17).

## Preserved Status and Deferred Work

* Specification 0003: Remains Proposed and deferred.
* Specification 0004: Remains in progress. Next step is delivering the reproducible benchmark runner (AC-9, AC-10).