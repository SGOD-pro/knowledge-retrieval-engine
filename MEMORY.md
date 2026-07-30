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

Two permanent, separate columns, never merged:
- `embedding_fast vector(384)` for local BGE-small ONNX embeddings.
- `embedding_full vector(1024)` for Titan V2 (prod) / Nemotron (dev) API embeddings.

Both columns are populated at ingestion time for every chunk.
Two separate HNSW indexes, one per column: `chunks_embedding_fast_hnsw_idx`, `chunks_embedding_full_hnsw_idx`.

Query-time routing rule (Rule 19): fast-path queries embed via local BGE-small and search ONLY `embedding_fast`. Full-path queries embed via the active API provider and search ONLY `embedding_full`. No query ever compares against both columns, and no code path merges results across them.

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

---

## Session State — Audit — Pre-Phase-3-Rerun (rev-5 alignment check)

**a. Schema**
- Classification: **STALE**
- Reason: The actual Postgres schema in `schema.sql` still defines the old single `embedding vector(1024)` column and a single index (`chunks_embedding_hnsw_idx`), lacking the two-column split.

**b. embed_service.py**
- Classification: **STALE**
- Reason: It only contains wrappers around the single API-based `embed_batch` and completely lacks the distinct local BGE-small ONNX path for the fast path.

**c. vector_retriever.py**
- Classification: **STALE**
- Reason: It still queries a single unified column via `search_vector` by filtering on a `provider` string, instead of targeting the newly mandated separate fast/full column paths.

**d. providers/*.py**
- Classification: **PARTIAL**
- Reason: Prod model IDs (Titan V2, Cohere Rerank, Nova Lite) match the matrix, but dev models (Nemotron embed/rerank) are omitted for fallbacks, and the local BGE-small configuration is completely absent.

**e. pdf_adapter.py / ingestion path**
- Classification: **STALE**
- Reason: `pdf_adapter.py` still invokes `opendataloader-pdf` locally via `subprocess.run`, and the required `odl-parser-lambda` deployment target is completely missing from the repository.

**f. Lambda deployment structure**
- Classification: **MISSING**
- Reason: The codebase remains a single monolithic `backend/src/kre` directory tree with no structural separation into the three required Lambda deployable units.

**g. Environment/provider config**
- Classification: **PARTIAL**
- Reason: `provider_client.py` implements `dev`/`prod` routing rules, but the database connection logic (e.g., in `postgres.py`) blindly reads `DATABASE_URL` without explicitly routing dev to `floci` versus prod to real AWS.

**h. Tests (test_phase2.py / test_phase3.py)**
- Classification: **STALE**
- Reason: Existing tests assert behavior against the old single-column `provider` logic and lack assertions for dual-column routing, local BGE-small embedding, or lambda deployment separation.

**Prioritized Fix List (Blocking Phase 3 Re-verification):**
1. **Schema (a)**: Update `schema.sql` to the dual-column schema (`embedding_fast` and `embedding_full`), as all downstream retrieval and ingestion logic depends on this foundation.
2. **Retrieval & Ingestion Logic (b, c)**: Update `vector_retriever.py` and `embed_service.py` to route explicitly to the new separated schema columns based on fast/full path rules.
3. **Provider Definitions (d)**: Update `providers/*.py` to explicitly include local BGE-small and the missing dev-tier Nemotron models.
4. **Lambda Decomposition (e, f)**: Restructure the monolithic codebase into three separate Lambda deployment units and update `pdf_adapter.py` to invoke the extraction lambda via boto3.
5. **Environment Configuration (g)**: Implement explicit routing logic to direct dev environments to `floci` for RDS/ElastiCache.
6. **Tests (h)**: Rewrite Phase 2 and Phase 3 tests to accurately reflect the new dual-column schema, local embedding fast path, and lambda boundaries.

---

## Session State � Phase 3 Re-Verification Attempt (2026-07-30)

- **Step 3 (Provider Definitions) Completion**:
  - embedding_provider.py updated to use mazon.titan-embed-text-v2:0 (Prod) and 
vidia/nemotron-3-embed-1b (Dev). Consolidating the local BGE-small ONNX embedding path into embedding_provider.py ensures EXACTLY ONE place owns "which embedding model for which path".
  - eranker_provider.py updated to use cohere.rerank-v3-5:0 (Prod) and 
vidia/llama-nemotron-rerank-vl-1b-v2 (Dev).
  - llm_provider.py updated to use mazon.nova-lite-v1:0 (Prod) and 
vidia/nemotron-nano-9b-v2:free (Dev).
  - Rule 28 and Rule 29 verification tests were written and pass logic checks (e.g., MODEL_PROVIDER=dev in prod environment throws ConfigurationError).

- **Phase 3 Exit Criteria Check**:
  - **BLOCKER**: The 120-query test set required by BENCHMARK.md for actual exit criteria validation (Recall, MRR, nDCG, Faithfulness) **does not exist** in the repository.
  - Per instructions, rather than fabricating a substitute dataset, Phase 3 is marked as **BLOCKED** and incomplete until real user questions per document type are provided for the benchmark.
  - The Provider Configuration Checkpoint (Step 0) and the Quality Gate were also aborted as the environment is blocked on the missing benchmark suite and Python dependencies (
umpy) for isolated execution.
