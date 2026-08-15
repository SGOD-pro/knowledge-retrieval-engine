# MEMORY.md — State and Caching Strategy

## Benchmark Data Incident (2026-08-10)
- **RETRACTION**: The data previously listed in `benchmark_results.md` is fully retracted as a mock-data incident (similar to the prior mocked-embeddings failure). It did not reflect any real script run and is false.
- **SOURCE OF TRUTH**: The ONLY valid benchmark data source is the `backend/tmp/benchmark_results.json` file. All UI rendering of benchmark metrics (KPIs) must read exactly from this JSON payload, rendering the actual (and currently failing) system values (e.g., `14616.6ms` p95 latency) with no sanitization or fabricated values. Cache Hit Rate and Hallucination Rate have no valid fields in this JSON and must be rendered as missing/not-live.

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

## Semantic Cache — QdrantDB-backed

Collections: `kre_cache_fast` (384-dim) and `kre_cache_full` (1024-dim)
  query_embedding  vector
  redis_key        payload field
  doc_scope_hash   payload field
  provider         payload field

Lookup: cosine similarity threshold >= 0.95 against the appropriate collection.

## Vector Store — QdrantDB

Collection `kre_chunks` with two named vectors, never merged:
- `embedding_fast` (384-dim) for BGE-small microservice ONNX embeddings.
- `embedding_full` (1024-dim) for Titan V2 API embeddings.

Both vectors populated at ingestion time for every chunk.
Query-time routing rule (Rule 19): fast-path queries embed via BGE-small microservice and search ONLY `embedding_fast`. Full-path queries embed via the active API provider and search ONLY `embedding_full`. No query ever compares against both vectors, and no code path merges results across them.

No FAISS index is used. No PostgreSQL/pgvector is used.

## PageIndex
- **Storage:** DynamoDB (persistent), stored as chunk metadata.

## OKF Knowledge Graph — DynamoDB

OKF entities, properties, and relations stored in DynamoDB tables (`okf_entities`, `okf_properties`).
Graph traversal executes against DynamoDB adjacency entries.
Zero LLM calls at query time — pure database lookup.

## Redis — ElastiCache Serverless

Same TTL, same write-guard conditions. ElastiCache Serverless used in cloud, local Redis in dev.

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
- Added DynamoDB document/chunk storage and QdrantDB vector persistence layer.
- Added deterministic PageIndex structural scoring and ranking.
- Added `POST /ingest` and `GET /documents/{id}`.
- **Verification:** All 10 Phase 1 tests passed. Ingestion format logic verified robustly.
- Source compilation passed.

---

## Session State — Phase 2 Backend Fast Path (2026-07-27)

- Added `rank-bm25` dependency to `backend/pyproject.toml`.
- Implemented Phase 2 Fast Path retrieval stack:
  - `bm25_retriever.py`: Stage 1 BM25Okapi keyword retrieval.
  - `page_index_retriever.py`: Stage 2 PageIndex structural ranking & candidate page scoping.
  - `vector_retriever.py`: Stage 3 QdrantDB similarity search scoped to candidate pages/chunks.
  - `planner.py`: Deterministic query complexity & rule-based planner (Rule 1 Fast Path, Rule 2 Relationship, Rule 3 Analytical, Rule 4 Full Path).
  - `response_builder.py`: Citation formatting (bounding_box for PDF, location_reference for DOCX/XLSX/PPTX/CSV), confidence scoring, and FastPathResponse builder with zero LLM calls.
- Updated QdrantDB collection with dual named vectors (`embedding_fast`, `embedding_full`).
- Added `POST /query` fast path endpoint.
- **Verification:** All 12 Phase 2 tests passed. Fixed Semantic Cache logic to guarantee zero network calls on fast path (Rule 19), and restored PageIndex component in the pipeline.

---

## Session State — Phase 3 Backend Pipeline (2026-07-27)

- Implemented provider integration for API-first architecture:
  - `reranker_provider.py` & `reranker.py` using NVIDIA Nemotron Rerank via NIM API.
  - `embedding_provider.py` & `embed_service.py` using Amazon Titan V2 for full-path.
  - `llm_provider.py` & `llm_service.py` using Nova Lite v1 for queries, enforcing T=0, 1200 max tokens, and markdown stripping.
- Implemented `concept_service.py` for OKF extraction utilizing Amazon Nova Micro in batch mode.
- Implemented `normalize_service.py` for entity clustering using cosine similarity.
- Implemented `okf_builder.py` and `okf_retriever.py` to extract and query OKF properties from DynamoDB.
- Implemented `graph_retriever.py` with DynamoDB adjacency traversal for complex queries.
- Implemented `compressor.py` and `fidelity_check.py` to ensure high entity coverage and low context size.
- Orchestrated the entire multi-path architecture in `langgraph_pipeline.py`.

---

## Session State — Phase 3 Invalidation (2026-07-30)

Phase 3 completion status was INVALIDATED. The prior tests were run against provider configuration that predated the NVIDIA NIM reranker integration and BGE-small microservice extraction. Phase 3 required RE-RUN in full.

---

## Session State — Audit — Pre-Phase-3-Rerun (rev-5 alignment check)

**a. Schema**
- Classification: **RESOLVED** — Migrated from PostgreSQL/pgvector to DynamoDB + QdrantDB. Dual named vectors (`embedding_fast` 384-dim, `embedding_full` 1024-dim) in QdrantDB.

**b. embed_service.py**
- Classification: **RESOLVED** — Two distinct code paths: BGE-small microservice for fast-path, Titan API for full-path.

**c. vector_retriever.py**
- Classification: **RESOLVED** — Routes to QdrantDB named vectors (`embedding_fast` or `embedding_full`) based on path.

**d. providers/*.py**
- Classification: **RESOLVED** — All providers wired to Bedrock (Titan V2, Nova Lite, Nova Micro) + NVIDIA NIM (Nemotron Rerank).

**e. Reranker Architecture Change (2026-08-07)**
- Swapped Cohere out for `nvidia/llama-nemotron-rerank-1b-v2` via NVIDIA NIM as a permanent architectural change.

---

## Session State — Phase 3 Completion (Rev 5 Alignment)

- Documented final verified metrics:
  - **[RETRACTED]** Recall@5: 0.908
  - **[RETRACTED]** Faithfulness: 0.894 (89.4%)
  - **[RETRACTED]** LLM Activation: 0.55 (55%)
  - **[RETRACTED]** p95 Latency: <4000ms (3678ms final logged)
  *(Note: Metrics retracted on 2026-08-12 because they contradict benchmark_results.json showing scored_queries: 0. See kre_phase1_audit.md for full details.)*
- Documented that the Zero Hallucination guardrail (Fidelity check + ruthless system prompt) successfully forces `NOT_FOUND` on incomplete context.

---

## Session State — Phase 4 API & Frontend Development (2026-08-07)

- **API Contract**: Created `api.md` establishing the strict API contract for Query Lambda endpoints.
- **Frontend Initialization**: Initialized a Vite + React + TypeScript frontend in `frontend/`.
- **Design System**: Installed Tailwind CSS v4.1, `tailwindcss-animate`, and Shadcn UI.
- **3-Pane UI Layout**: Implemented the workspace using CSS Grid in `App.tsx`.
- **Dummy Auth Layer**: Created `AuthContext` to default to an unauthenticated state.
- **Testing**: Vitest and Playwright tests passed.
- **Status**: Phase 4 is complete.

---

## Session State — Phase 4 E2E Integration (2026-08-07)

- E2E flow works end-to-end with real data ingestion.
- CSVs/DOCX successfully index to Qdrant/DynamoDB.
- PDF ingestion can run locally via `odl_main.py` or via deployed Lambda.

---

## Session State — Phase 4 & Phase 5 Hardening & Exit Verification (2026-08-10)

- **Backend Deliverables Completed**:
  - `Mangum` ASGI handler integrated (`main.handler`) for AWS Lambda packaging.
  - 50MB upload file size limit enforced at API layer with `413 Payload Too Large`.
  - Auth token middleware added (`verify_auth`) returning `401 Unauthenticated` when auth is required.
  - CORS middleware configured for FastAPI (dev: `http://localhost:5173`, prod: configured frontend URL only — no wildcard `*`).
- **Test Suites Verified**:
  - Backend Pytest: Phase 4 and Phase 5 tests passed.
  - Frontend Vitest: Unit tests pass.
  - Playwright E2E: End-to-end tests pass.

---

## Session State — Architecture Refactoring (2026-08-10)

- **Codebase Restructured**: Flattened `shared/` and `query_lambda/` into flat `src/` directory.
- **BGE-Small Microservice**: Extracted as a separate deployment unit (`bge_microservice/`). Model file (`model.onnx`) to be provided by user.
- **OKF Layer**: Confirmed as DynamoDB-backed typed fact store. NOT file-based markdown. OKF follows the Open Knowledge Format philosophy for extraction, but stores structured data in DynamoDB for <10ms lookup latency.
- **Postgres Removal**: All PostgreSQL/pgvector references removed from codebase and documentation. Storage is exclusively DynamoDB (metadata) + QdrantDB (vectors) + Redis (cache).

---

## Session State — Phase 3 Full Build (2026-08-11)

- **BGE Microservice Rewrite**: `bge_microservice/main.py` rewritten as a pure Lambda handler — no FastAPI, no Mangum. `lambda_handler(event, context)` handles single (`{"text":...}`) and batch (`{"texts":...}`). Batch uses `ProcessPoolExecutor(max_workers=6)`. Model files live in `bge-onnx/` subfolder.
- **Embed Service Routing**: `ingestion/embed_service.py` updated — prod mode calls `bge-embedding-lambda` via boto3, dev uses local ONNX, test uses deterministic SHA-256 fallback. Zero ONNX weights bundled in the query Lambda.
- **OKF Builder Full Write**: `ingestion/okf_builder.py` fully implemented — Tier 1 regex + Tier 3 Nova Micro extraction, entity clustering via `normalize_service`, DynamoDB writes to `okf_entities` + `okf_properties`. Token usage tracked with pre-aggregated SUMMARY item using DynamoDB `ADD` (atomic, never re-aggregate on read).
- **Parse Service Split**: `parse_service.py` split into `parse_file()` (lean, for unit tests) and `ingest_document()` (full pipeline: parse → embed → OKF). `/ingest` route updated to use `ingest_document()`.
- **AWS Infra Profile Routing**: `aws/infra.py` fully documented — dev+local services → `profile=local+localhost:4566`, dev+cloud services → `profile=aws+real AWS`, prod → IAM role. BGE microservice marked as TODO pending Lambda deploy.
- **Benchmark Script**: `tests/benchmark_comparison.py` created — runs Traditional RAG, PageIndex, OKF-only, and KRE against same 12 queries over 5 real documents. Run-once guard. No sugarcoating in output.
- **Verification**: All 30 Phase 1+2+3 tests passed (1 skipped: pdf test requires hdfc.pdf on real AWS).
  - Phase 1: 9 passed, 1 skipped
  - Phase 2: 12 passed
  - Phase 3: 8 passed (BGE Lambda routing, OKF writes, token tracking, OKF zero LLM, full path 1 LLM call, BFS MAX_HOPS=2, Nova Micro zero query calls)

---

## Session State — KRE Audit & Architecture Remediation (Phases AA-AH) (2026-08-15)

- **Audit Completion**: Successfully completed deep-dive audit of end-to-end retrieval metrics.
- **Architectural Fixes Implemented**:
  - **Deterministic IDs**: `parse_service.py` migrated to `uuid.uuid5` for deterministic chunk IDs, solving re-ingestion instability.
  - **Vector Integrity**: Hardened embedding provider to block silent exceptions and retry throttled requests (5-attempt backoff). Qdrant payload schema updated with explicit index fields (`page_number`, `document_id`, `original_id`).
  - **Semantic Routing**: Replaced naive keyword flagging in `planner.py` with semantic centroid routing, cleanly splitting traffic ~50/50 (Fast vs Full path) without additional LLM latency.
  - **Format Support**: Implemented 1D string matching (`candidate_chunk_ids` via `MatchAny`) for pageless formats (DOCX, CSV, PPTX), achieving 100% recall on DOCX/CSV.
- **Evaluation Status**: Final 77-query benchmark executed successfully with 0 dropped queries. True Blended Recall@5 is verified at **72.73%**.
- **Conclusion**: The KRE core architecture is structurally sound, stable, and completely validated against the 77-query baseline.
