# Verify: Core engine walking skeleton · spec 0004 · updated 2026-10-15
_Steps derived from spec 0004 acceptance criteria. `/check verify` runs these; `/test` locks the durable ones._

## Commands
- [x] `uv run pytest tests/test_csv_vertical_slice_0004.py` → 5 passed (CSV ingestion, large dataset, coverage, boundary denial, storage failure distinction) → AC-1, AC-2, AC-3, AC-4, AC-7, AC-8, AC-11
- [x] `uv run pytest tests/test_pdf_retrieval_slice_0004.py` → 6 passed (PDF ingestion with page chunking, dual fast and full embeddings, partial page skip coverage exclusions, text query with DocumentLocation citations, source supported fact verification, structured refusal with zero citations, tombstoned source citation denial) → AC-1, AC-2, AC-3, AC-5, AC-6, AC-8, AC-11
- [x] `python verify_0004_live.py` → 17 passed live over HTTP (CSV and PDF ingestion, boundary denial, deterministic query, coverage blocking, DocumentLocation citations, verified source supported fact assertion, tombstoning)
- [x] `uv run pytest tests/test_structured_query_service.py` → 7 passed (StructuredQueryService period comparison, normalization of InferredDtype enum and string dtypes) → AC-4, AC-11
- [x] `uv run pytest tests/test_fix1_async_ingestion.py tests/test_dynamodb_boundary_and_execution.py` → 10 passed (Async upload execution, direct ingestion without test mock machinery in production code) → AC-1, AC-2
- [x] `uv run pytest tests/test_benchmark_runner_and_persistence_0004.py` → 14 passed (BenchmarkRunner execution, recoverable persistence ordering S3 first then DynamoDB, failure handling, adversarial fabricated answer rejection, citation validation, coverage scoring, token accounting, infrastructure ERROR failure, claim support verification without identifier credit, production execution engine token trace without undeclared fields, and generic test double extraction) → AC-9, AC-10, AC-11

## Execution environment distinctions
- Local HTTP checks: 17 live requests executed over HTTP against local Uvicorn FastAPI server on port 8001 in `verify_0004_live.py`, verifying real document ingestion, 250 row calculation without truncation, cross workspace denial, and factual citation extraction.
- Mocks and mocked generation: isolated LLM completion generation via explicit generic test double without domain or PDF-specific logic (`MODEL_PROVIDER=test`), mock S3 artifact store, and mock DynamoDB table items in isolated repository unit tests; these checks verify pipeline contracts and do not assert real model accuracy.
- Real model execution: local embedding computation (MiniLM-L6-v2 384d and BGE 1024d) and local BM25 ranking run real local algorithm execution; remote AWS Bedrock invocations bypassed during automated tests.
- Live AWS verification: deferred; DynamoDB and S3 interactions verified through local SQLite, in memory substitutes, and botocore client mock structures; no active AWS cloud deployment performed.

## Acceptance criteria coverage
- AC-1 covered by boundary isolation, cross workspace 404 denial, and immediate tombstone filtering
- AC-2 covered by real CSV and text PDF ingestion with DocumentSnapshot provenance persistence
- AC-3 covered by malformed row and corrupted page rejection tracking with incomplete coverage flags
- AC-4 covered by full dataset streaming and typed deterministic aggregation without row truncation
- AC-5 covered by dual fast 384 and full 1024 embedding population, BM25 retrieval, and reranking
- AC-6 covered by narrative evidence verification, source supported fact assertions, and exact DocumentLocation page citations
- AC-7 covered by incomplete coverage strictly blocking complete aggregate queries with HTTP 422
- AC-8 covered by storage failure (503) remaining distinct from insufficient evidence refusal (200 with zero citations)
- AC-9 covered by BenchmarkRunner measuring token usage, latency, correctness, evidence support, and coverage, with adversarial regression protection
- AC-10 covered by S3 first then DynamoDB recoverable benchmark persistence and DocumentSnapshot models
- AC-11 covered by deterministic pytest suite (38 passed) and live HTTP verification script (17 passed)
