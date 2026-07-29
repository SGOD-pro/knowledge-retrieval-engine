# MEMORY.md — State and Caching Strategy

## Redis Cache

```yaml
Key:   sha256(normalize(query) + doc_scope_hash)
Value: {answer, citations, retrieval_path, confidence, latency_breakdown}
TTL:   24 hours
Write: only when confidence >= MEDIUM (>= 0.50)
Read:  checked before any retrieval stage runs
```

### Do NOT Cache:
- Responses with `confidence < 0.50` (LOW)
- Responses where LLM returned `NOT_FOUND`
- Responses with `coverage_ratio < 0.6`

### Invalidation:
- On re-ingestion of document `D`: `DELETE` all cache keys where `doc_scope_hash` includes `D`.

## Semantic Cache — pgvector-backed

Table: cache_entries
  query_embedding  vector(1024)
  redis_key        text
  doc_scope_hash   text
  provider         text
  created_at       timestamptz

Lookup query logic matching rev 5, via RDS PostgreSQL.
Dev environment uses `floci` for emulation; prod uses real RDS.

## Vector Store — pgvector

All chunk embeddings stored in `chunks.embedding vector(1024)`.
HNSW index built on ingestion, incrementally maintained by Postgres.
No FAISS index is used.

## PageIndex
- **Storage:** PostgreSQL (persistent).

## OKF Graph — Postgres only

`relations` table queried directly via recursive CTE per request.

## Redis — ElastiCache Serverless

Same TTL, same write-guard conditions. ElastiCache Serverless used in cloud, `floci` in dev.

## What Is Not Cached
- Retrieval plans, Reranker scores, LOW confidence responses, `NOT_FOUND` responses.

## Feedback Storage (write now, act on it in v2)
Collect only. Do not wire to reranking weights.

## No Persistent User State in v1
No session store, conversation history, query logging to persistent storage.

---

## Session State — Phase 1 Backend (2026-07-24)

- Implemented the Phase 1 backend scaffold under `backend/src/kre`.
- Added format routing for PDF, DOCX, XLSX, and PPTX.
- Added DOCX paragraph/heading parsing, XLSX computed-value parsing, PPTX shape and speaker-notes parsing, and an opendataloader-pdf JSON batch adapter.
- Added the unified nullable-bounding-box `Chunk` schema and `Document` model.
- Added the PostgreSQL/pgvector schema and repository persistence layer.
- Added deterministic PageIndex structural scoring and ranking.
- Added `POST /ingest` and `GET /documents/{id}`.
- Added five focused Phase 1 tests; all passed.
- Source compilation passed.
- Phase 1 performance and PageIndex corpus exit gates remain unverified because the repository contains no benchmark corpus or PostgreSQL connection configuration.
- The pre-existing backend `.venv` is incomplete on Windows; verification used uv isolated execution without modifying or deleting that environment.

---

## Session State — Phase 2 Backend Fast Path (2026-07-27)

- Added `rank-bm25` dependency to `backend/pyproject.toml`.
- Implemented Phase 2 Fast Path retrieval stack under `backend/src/kre/retrieval`:
  - `bm25_retriever.py`: Stage 1 BM25Okapi keyword retrieval.
  - `page_index_retriever.py`: Stage 2 PageIndex structural ranking & candidate page scoping.
  - `vector_retriever.py`: Stage 3 pgvector similarity search scoped to candidate pages/chunks.
  - `planner.py`: Deterministic query complexity & rule-based planner (Rule 1 Fast Path, Rule 2 Relationship, Rule 3 Analytical, Rule 4 Full Path).
  - `response_builder.py`: Citation formatting (bounding_box for PDF, location_reference for DOCX/XLSX/PPTX/CSV), confidence scoring, and FastPathResponse builder with zero LLM calls.
- Updated `backend/src/kre/db/schema.sql` with pgvector HNSW index (`chunks_embedding_hnsw_idx`).
- Updated `backend/src/kre/db/postgres.py` with `search_vector` pgvector query method and in-memory fallback for test environments.
- Added `POST /query` fast path endpoint to `backend/src/kre/api/main.py`.
- Added comprehensive unit tests in `backend/tests/test_phase2.py` covering Rule 1 zero LLM calls, Stage ordering (BM25 -> PageIndex -> Vector), module logging, citation location references, and provider matching.
- **Verification:** Ran `pytest tests/test_phase2.py` and all 7 tests passed successfully.

---

## Session State — Phase 3 Backend Pipeline (2026-07-27)

- Implemented provider integration for API-first architecture:
  - `reranker_provider.py` & `reranker.py` using Cohere Rerank 3.5 on Bedrock (`prod`) and Nemotron (`dev`).
  - `embedding_provider.py` & `embed_service.py` using Amazon Titan V2 (`prod`) with 8k token truncation.
  - `llm_provider.py` & `llm_service.py` using Nova Lite v1 (`prod`) and Nemotron Nano (`dev`), enforcing T=0, 1200 max tokens, and markdown stripping.
- Implemented `concept_service.py` for OKF extraction utilizing Amazon Nova Micro in batch mode and regex patterns.
- Implemented `normalize_service.py` for entity clustering using cosine similarity.
- Implemented `okf_builder.py` and `okf_retriever.py` to extract and query OKF properties from Postgres.
- Implemented `graph_retriever.py` with recursive CTE graph traversal for complex queries.
- Implemented `compressor.py` and `fidelity_check.py` to ensure high entity coverage and low context size.
- Orchestrated the entire multi-path architecture in `langgraph_pipeline.py`.
- **Verification:** Ran `pytest tests/test_phase3.py tests/test_phase2.py` and all 19 combined tests passed successfully. Phase 3 is complete.

---

## Session State — Phase 3 Invalidation (2026-07-30)

Phase 3 completion status is INVALIDATED as of this revision. The 19 previously-passing tests were run against provider configuration and an ingestion architecture that predate the `odl-parser-lambda` integration, the `floci`-scoped dev/prod split, and the restored local BGE-small fast-path embedding. Phase 3 must be RE-RUN in full — including the Provider Configuration Checkpoint (8 live provider verification calls) — against the corrected architecture before its exit criteria can be considered met again. Do not treat the prior 19-test pass as current evidence of Phase 3 completion.

*Note: `embed_service.py` now has TWO distinct code paths: local BGE-small ONNX inference for fast-path queries inside Query Lambda, and API calls for full-path/ingestion-time embedding.*
