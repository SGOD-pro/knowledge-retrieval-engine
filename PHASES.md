# PHASES.md — Build Sequence (rev 6)

## Rules
- Do not start Phase N+1 until Phase N exit criteria pass.
- No graph code in Phase 1 or Phase 2.
- Benchmark targets are exit criteria, not aspirations.
- Every phase must have passing test cases defined in RULES.md before exit.

---

## Phase 1 — Parsing + PageIndex + Multi-Format (Days 1–10)

Deliverables:
- Decide and stand up all deployment targets (Container image for PDF Extraction, Zip for Ingestion, Zip for Query, BGE-small Microservice) as a Phase 1 deliverable.
- Parse and PageIndex structure.

Exit Criteria:
- 100-page PDF ingested in <30 seconds.

**Status: COMPLETE.**

---

## Phase 2 — Fast Path (BM25 + PageIndex + Embeddings) (Days 11–17)

Deliverables:
  - bm25_retriever.py (rank-bm25).
  - providers/embedding_provider.py with Bedrock routing implemented and tested.
  - QdrantDB dual named vectors (`embedding_fast` 384-dim, `embedding_full` 1024-dim) on `kre_chunks` collection.
  - vector_retriever.py: fast path calls BGE-small microservice, no Bedrock network calls.
  - planner.py: fast_path rule only.
  - response_builder.py with bounding_box + fallback location reference.
  - POST /query (fast path only).

Exit criteria (all must pass):
  - Fast path p95 latency < 400ms (warm).
  - Recall@3 > 0.75 on 40-query factual test set.
  - Citations resolve to bounding_box (PDF) or location reference (DOCX/XLSX/PPTX) — zero null location responses.
  - BM25 before vector search enforced by test.
  - Vector search scoped to PageIndex candidates.

**Status: COMPLETE.**

---

## Phase 3 — Reranker + OKF + Full Pipeline (Days 18–30)

> [!WARNING]
> Any prior Phase 3 completion claim predating rev 6 must be re-verified against the current architecture (DynamoDB + QdrantDB, no Postgres).

**STEP 0 — Provider Configuration Checkpoint (must complete first):**
- Smoke test all providers BEFORE running any Phase 3 test case:
  1. Embed one word via Titan V2 → confirm 1024-dim vector returned.
  2. Embed one word via BGE-small microservice → confirm 384-dim vector returned.
  3. Rerank one query/doc pair via NVIDIA NIM → confirm float score returned.
  4. Call Nova Micro with trivial extraction prompt → confirm JSON response.
  5. Call Nova Lite with trivial query → confirm text response.
- If ANY smoke test fails, do NOT proceed. Fix the provider configuration first. This prevents burning tokens on doomed test runs.

Deliverables:
  - concept_service.py (Tier 1 & Tier 3 extraction via Nova Micro).
  - normalize_service.py.
  - providers/reranker_provider.py (NVIDIA NIM) & llm_provider.py (Nova Lite/Micro).
  - okf_builder.py: extracts OKF entities/properties/relations → DynamoDB (`okf_entities`, `okf_properties` tables).
  - okf_retriever.py: zero-LLM DynamoDB lookup at query time.
  - graph_retriever.py (DynamoDB adjacency traversal, MAX_HOPS=2, MAX_NODES=40).
  - reranker.py, fidelity_check.py, compressor.py, llm_service.py.
  - Full planner.py (Rules 1-4).
  - vector_retriever.py (Titan API embeddings for full path, QdrantDB search).
  - LangGraph pipeline wiring — combined OKF + Traditional RAG + PageIndex.
  - POST /query (full pipeline).

OKF Architecture:
  - OKF (Ontology-driven Knowledge Framework) — concepts, typed properties, and cross-references.
  - Nova Micro extracts structured facts at ingestion time: `{Concept: "Revenue", Type: "METRIC", Property: "Q3 2024", Value: "$1.2M"}`.
  - Facts stored in DynamoDB for <10ms query-time lookup.
  - At query time, `okf_retriever.py` does a pure database lookup — zero LLM calls.
  - OKF handles ALL document types: text PDFs, CSVs, Excel (e.g., "which location had the most orders?" → OKF knows the schema).
  - The knowledge graph (concepts linked by relations) enables graph traversal for relationship queries.

Compressor Rules:
  - Reranker selects top 6 most relevant chunks from the 15-20 candidates.
  - Compressor strips ALL metadata from those 6 chunks.
  - Concatenates only raw text, strictly capped at 1200 tokens.
  - Must select exact relevant sections — no extra content, no less.

QUALITY GATE:
  - Requires Step 0 to have passed.
  - Extract OKF properties from 10 real documents using Nova Micro. Precision >= 85%.

Exit criteria (all must pass, AFTER Step 0 and Quality Gate):
  - Full pipeline p95 latency < 4000ms (warm).
  - Faithfulness > 0.80 on 120-query test set.
  - Hallucination rate < 5%.
  - Entity coverage post-compression = 100%.
  - LLM call count == 1 for full path, 0 for fast path.
  - Nova Micro call count == 0 for any query.
  - OKF retriever makes zero LLM calls (pure DynamoDB lookup).

**Status: COMPLETE (verified metrics: Recall@5=0.908, Faithfulness=0.894, LLM Activation=0.55, p95=3678ms).**

---

## Phase 4 — API + Frontend + Lambda Packaging (Days 31–42)

Goal: A working product a real user can verify answers with, deployed
across the three-Lambda + microservice architecture, with the citation-first UI that
is KRE's core differentiator.

Prerequisite: Phase 3 exit criteria fully passed.

Deliverables:
  - Lambda handler wrapping the FastAPI app (via Mangum, `main.handler` entry point) alongside the existing local FastAPI dev server.
  - BGE-small microservice scaffolded in `bge_microservice/` with FastAPI `/embed` endpoint. Model file (`model.onnx`) to be provided by user.
  - Package size checks in CI, per deployable unit:
      query_lambda: <250MB zipped (no BGE weights — calls microservice).
      ingestion_lambda: <250MB zipped.
      pdf_extraction_lambda: <10GB container image via ECR.
      bge_microservice: standalone, no size constraint.
  - Auth integration: OAuth2.1 Bearer token validation placed before Stage 0 (cache check). Unauthenticated requests never reach cache or retrieval.
  - File size limit enforced at the API layer: uploads over 50MB rejected with 413 before any parsing begins.
  - CORS Configuration (SECURITY):
      - Dev: Allow `http://localhost:5173` only. Methods: GET, POST, OPTIONS.
      - Prod: Allow configured frontend URL only (NO localhost, NO wildcard `*`). Methods: GET, POST, OPTIONS. All others blocked.
  - Frontend Setup:
    - PDF viewer: bounding_box highlight.
    - DOCX/XLSX/PPTX: location reference display.
    - Source format badge on each citation chip.
    - Confidence badge (HIGH/MEDIUM/LOW).
    - Fast Match vs Reasoned Answer badge.
  - Frontend Testing Suite (Vitest + React Testing Library).

Exit criteria:
  - All packaging gates pass for all deployment units.
  - All Vitest test suites pass with 0 UI breakage failures.
  - Frontend successfully handles API responses with varying location_reference formats.
  - CORS configuration verified via test — dev allows localhost:5173, prod allows ONLY configured URL.
  - Unauthenticated requests correctly receive 401 before touching retrieval or cache.
  - Uploads over 50MB correctly receive 413 before parsing begins.

Test Cases Required to Pass:
  - test_api_query_success_renders_citations
  - test_api_query_handles_not_found
  - test_cors_headers_present_on_api_response
  - test_cors_no_wildcard_in_prod
  - test_unauthenticated_request_returns_401_before_retrieval
  - test_upload_over_50mb_returns_413_before_parsing
  - test_query_lambda_package_size_under_250mb
  - test_ingestion_lambda_package_size_under_250mb

**Status: COMPLETE.**

---

## Phase 5 — Hardening + Benchmark Parity (Days 43–50)

Goal: Production-ready, no silent failures, all benchmarks passing
together on Bedrock providers.

Prerequisite: Phase 4 exit criteria fully passed.

Additional deliverables:
  - Full 120-query benchmark run under Bedrock providers.
  - Cold start latency benchmark, tracked separately from warm p95.
  - Both cache layers (QdrantDB semantic + Redis exact-match) confirmed active together.
  - Stress test: 500 queries, 0 unhandled errors.
  - AWS interaction minimization: smoke tests gate Bedrock-dependent test runs.

Exit criteria: all BENCHMARK.md rev 5 targets met, specifically:
  - Fast path p95 latency (warm) < 400ms.
  - Fast path p95 latency (cold start) < 1500ms.
  - Cold start delta (p95_cold − p95_warm) < 1200ms.
  - Full pipeline p95 latency (warm) < 4000ms.
  - CI blocks merge if any benchmark regresses >5% from Phase 3 baseline.

Test Cases Required to Pass:
  - test_full_pipeline_p95_under_4000ms
  - test_fast_path_cold_start_under_1500ms
  - test_cold_start_delta_under_1200ms

**Status: COMPLETE (exit criteria met 2026-08-10).**

---

## Phase 6 — OKF Knowledge Graph Rebuild + Routing Precision (Days 51–60)

Goal: Rebuild the OKF layer from scratch to be a true knowledge graph backed by DynamoDB. Ensure the combined retrieval pipeline (OKF + Traditional RAG + PageIndex) routes with surgical precision — the user gets exactly the right amount of data, not less, not more.

Prerequisite: Phase 5 exit criteria fully passed. BGE-small microservice scaffolded and `model.onnx` provided by user.

Deliverables:
  - **OKF DynamoDB Schema**: `okf_entities` and `okf_properties` tables with strict typed schemas.
  - **okf_builder.py rewrite**: Nova Micro extracts concepts/properties/relations from ALL document formats (PDF, CSV, Excel, DOCX, PPTX) at ingestion time. Stores directly to DynamoDB (drawing concept type vocabulary such as PRODUCT, PERSON, ORGANIZATION, METRIC, POLICY, PROCESS, DATE_PERIOD, LOCATION from OKF conventions, without file-based markdown bundle serialization).
  - **okf_retriever.py rewrite**: Pure DynamoDB lookup. Given query entities, retrieves typed properties and related concepts. Zero LLM calls.
  - **Knowledge Graph Traversal**: DynamoDB adjacency-based graph traversal. Relations stored as concept-to-concept links with relation_type and weight.
  - **Combined Retrieval Routing**: Pipeline merges OKF facts + vector chunks + BM25 hits + PageIndex structural scores. The compressor receives ALL of these signals and selects the exact relevant sections.
  - **BGE-Small Microservice Integration**: Wire `embedding_provider.py` to call the BGE-small microservice HTTP endpoint for fast-path embeddings instead of bundling ONNX weights.

Exit criteria:
  - OKF entities/properties stored in DynamoDB, retrievable in <10ms.
  - Zero PostgreSQL references in entire codebase.
  - BGE-small microservice responds to `/embed` endpoint with 384-dim vectors.
  - Combined retrieval (OKF + RAG + PageIndex) produces higher recall than any single method.
  - Knowledge graph traversal works for relationship queries across CSV/Excel structured data.

---

## Phase 7 — Security Hardening + Production Deploy (Days 61–70)

Goal: Lock down security, optimize AWS costs, prepare for production deployment.

Deliverables:
  - **CORS Lockdown**: Prod environment allows ONLY the configured frontend URL. No localhost, no wildcards. Only GET, POST, OPTIONS methods.
  - **Auth Hardening**: OAuth2.1 token validation is non-bypassable in prod.
  - **AWS Cost Optimization**: Smoke tests before every Bedrock invocation in test suites. Local-first dev for odl-parser and BGE-small.
  - **Infrastructure as Code**: `aws/infra.py` validates and creates DynamoDB tables, S3 buckets, Qdrant collections on cold start.
  - **Monitoring**: CloudWatch dashboards for Lambda invocations, DynamoDB read/write units, Qdrant query latency.

Exit criteria:
  - Penetration test: no unauthenticated access to /query or /ingest in prod.
  - CORS test: prod responds with 403 to requests from unauthorized origins.
  - Full 120-query benchmark passes on production infrastructure.
  - Zero unhandled errors across 500-query stress test.
