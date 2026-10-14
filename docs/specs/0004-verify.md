# Verify: Core engine walking skeleton · spec 0004 · updated 2026-10-14
_Steps derived from spec 0004 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones._

## Commands
- [x] `uv run pytest tests/test_csv_vertical_slice_0004.py` → 5 passed (CSV ingestion, large dataset, coverage, boundary denial, storage failure distinction) → AC-1, AC-2, AC-3, AC-4, AC-7, AC-8, AC-11
- [x] `uv run pytest tests/test_pdf_retrieval_slice_0004.py` → 6 passed (PDF ingestion with page chunking, dual fast and full embeddings, partial page skip coverage exclusions, text query with DocumentLocation citations, structured refusal with zero citations, tombstoned source citation denial) → AC-1, AC-2, AC-3, AC-5, AC-6, AC-8, AC-11
- [x] `python verify_0004_live.py` → 17 passed live over HTTP (CSV and PDF ingestion, boundary denial, deterministic query, coverage blocking, DocumentLocation citations, tombstoning)
- [x] `uv run pytest tests/test_structured_query_service.py` → 7 passed (StructuredQueryService period comparison, normalization of InferredDtype enum and string dtypes) → AC-4, AC-11
- [x] `uv run pytest tests/test_fix1_async_ingestion.py tests/test_dynamodb_boundary_and_execution.py` → 10 passed (Async upload execution, direct ingestion without test mock machinery in production code) → AC-1, AC-2
- [x] `uv run pytest tests/test_benchmark_runner_and_persistence_0004.py` → 6 passed (BenchmarkRunner execution, recoverable persistence ordering S3 first then DynamoDB, failure handling) → AC-9, AC-10, AC-11

## Acceptance criteria coverage
- AC-1 covered by boundary isolation, cross workspace 404 denial, and immediate tombstone filtering
- AC-2 covered by real CSV and text PDF ingestion with DocumentSnapshot provenance persistence
- AC-3 covered by malformed row and corrupted page rejection tracking with incomplete coverage flags
- AC-4 covered by full dataset streaming and typed deterministic aggregation without row truncation
- AC-5 covered by dual fast 384 and full 1024 embedding population, BM25 retrieval, and reranking
- AC-6 covered by narrative evidence verification and exact DocumentLocation page citations
- AC-7 covered by incomplete coverage strictly blocking complete aggregate queries with HTTP 422
- AC-8 covered by storage failure (503) remaining distinct from insufficient evidence refusal (200 with zero citations)
- AC-9 covered by BenchmarkRunner measuring token usage, latency, correctness, evidence support, and coverage
- AC-10 covered by S3 first then DynamoDB recoverable benchmark persistence and DocumentSnapshot models
- AC-11 covered by deterministic pytest suite and live HTTP verification script
