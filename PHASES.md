# PHASES.md — Build Sequence (rev 5)

## Rules
- Do not start Phase N+1 until Phase N exit criteria pass.
- No graph code in Phase 1 or Phase 2.
- Benchmark targets are exit criteria, not aspirations.
- Every phase must have passing test cases defined in RULES.md before exit.

---

## Phase 1 — Parsing + PageIndex + Multi-Format (Days 1–10)

Deliverables:
- Decide and stand up all three Lambda deployment targets (Container image for PDF Extraction, Container/Zip for Ingestion, Zip for Query) as a Phase 1 deliverable.
- Parse and PageIndex structure.

Exit Criteria:
- 100-page PDF ingested in <30 seconds (Pending re-verification due to cross-Lambda synchronous `odl-parser-lambda` call).

---

## Phase 2 — Fast Path (BM25 + PageIndex + Embeddings) (Days 11–17)

Deliverables:
  - bm25_retriever.py (rank-bm25).
  - providers/embedding_provider.py with dev(OpenRouter)/prod(Bedrock) routing implemented and tested.
  - pgvector HNSW index creation on chunks table in RDS PostgreSQL.
  - vector_retriever.py: fast path uses local BGE-small ONNX model without network calls.
  - planner.py: fast_path rule only.
  - response_builder.py with bounding_box + fallback location reference.
  - POST /query (fast path only).

Exit criteria (all must pass):
  - Fast path p95 latency < 400ms (warm).
  - Recall@3 > 0.75 on 40-query factual test set.
  - Citations resolve to bounding_box (PDF) or location reference (DOCX/XLSX/PPTX) — zero null location responses.
  - BM25 before vector search enforced by test.
  - Vector search scoped to PageIndex candidates.

---

## Phase 3 — Reranker + OKF + Full Pipeline (Days 18–30)

> [!WARNING]
> Any prior Phase 3 completion claim predating this revision is VOID. The full phase must be executed fresh against the current architecture.

**STEP 0 — Provider Configuration Checkpoint (must complete first):**
- Both dev config (OpenRouter API keys for embedding, reranker, OKF-extraction, query-LLM) and prod config (Bedrock model access approved + IAM permissions verified) must be live and independently verified BEFORE any Phase 3 test case is run.
- Verification method: For each of the 4 model roles × 2 environments = 8 total endpoint checks, make one real API call with a trivial test payload and confirm a valid response.
- Only after all 8 checks pass does the existing Phase 3 test list get run. If tests were run prior to this checkpoint, results are INVALID.

Deliverables:
  - concept_service.py (Tier 1 & Tier 3 extraction).
  - normalize_service.py.
  - providers/reranker_provider.py & llm_provider.py.
  - okf_builder.py & okf_retriever.py.
  - graph_retriever.py (recursive CTE).
  - reranker.py, fidelity_check.py, compressor.py, llm_service.py.
  - Full planner.py.
  - vector_retriever.py (API embeddings for full path).
  - LangGraph pipeline wiring.
  - POST /query (full pipeline).

QUALITY GATE:
  - Requires Step 0 to have passed.
  - Extract OKF properties from 10 real documents using dev and prod. Precision >= 85%.

Exit criteria (all must pass, AFTER Step 0 and Quality Gate):
  - Full pipeline p95 latency < 4000ms (warm).
  - Faithfulness > 0.80 on 120-query test set.
  - Hallucination rate < 5%.
  - Entity coverage post-compression = 100%.
  - LLM call count == 1 for full path, 0 for fast path.
  - Nova Micro call count == 0 for any query.

---

## Phase 4 — API + Frontend + Lambda Packaging (Days 31–42)

Goal: A working product a real user can verify answers with, deployed
across the three-Lambda architecture, with the citation-first UI that
is KRE's core differentiator.

Prerequisite: Phase 3 exit criteria fully passed, including the five
pre-benchmark fixes (semantic cache Layer 1, fast-path confidence
formula, OKF schema, Tier 1.5 SVO extraction, normalization
thresholds) and the 120-query benchmark report.

Deliverables:
  - Lambda handler wrapping the FastAPI app (via Mangum or similar
    ASGI adapter, main.handler entry point) alongside the existing
    local FastAPI dev server.
  - Package size checks in CI, per deployable unit — not just
    query_lambda:
      query_lambda: <250MB zipped.
      ingestion_lambda: <250MB zipped (confirm post-JVM-split size,
        since it's now lighter without the bundled JRE).
      pdf_extraction_lambda: <10GB container image via ECR.
  - Provisioned concurrency configuration for query Lambda (decision
    deferred to Phase 5 cost analysis).
  - Auth integration: OAuth2.1 (SGOD-pro/OAuth2.1) Bearer token
    validation placed before Stage 0 (semantic cache check) in the
    request pipeline — unauthenticated requests never reach cache
    or retrieval.
  - File size limit enforced at the API layer: uploads over 50MB
    rejected with 413 before any parsing begins.
  - Frontend Setup:
    - PDF viewer: bounding_box highlight (unchanged).
    - DOCX/XLSX/PPTX: location reference display
      ("Sheet: Revenue, Row: 14" instead of PDF highlight).
    - Source format badge on each citation chip.
    - Confidence badge (HIGH/MEDIUM/LOW) using the corrected
      per-path formulas (fast path: vector_similarity_avg * 0.6 +
      coverage_ratio * 0.4; full path: reranker_score_avg * 0.6 +
      coverage_ratio * 0.4).
    - Fast Match vs Reasoned Answer badge, reflecting which path
      actually served the query.
  - Frontend Testing Suite (Vitest + React Testing Library):
    - Configure Vitest in frontend/vite.config.ts.
    - API mock tests for POST /query (success, 404, 500 errors).
    - Component snapshot/UI breakage tests (citation chips, 3-pane
      layout).

Exit criteria:
  - Backend unchanged from prior spec, plus packaging gates pass for
    all three Lambda units (query, ingestion, pdf_extraction).
  - All Vitest test suites pass with 0 UI breakage failures.
  - Frontend successfully handles API responses with varying
    location_reference formats.
  - CORS configuration verified via test (frontend origin explicitly
    allowed in FastAPI).
  - Unauthenticated requests correctly receive 401 before touching
    retrieval or cache — verified by test, not code review.
  - Uploads over 50MB correctly receive 413 before parsing begins.
  - 5 real users complete 10 queries each; average time-to-verify
    (user confirms an answer against the source document) < 15s.
  - Zero unhandled errors across a 100-query session.

Test Cases Required to Pass:
  - test_api_query_success_renders_citations
  - test_api_query_handles_not_found
  - test_citation_chip_displays_bounding_box
  - test_citation_chip_displays_location_reference
  - test_cors_headers_present_on_api_response
  - test_unauthenticated_request_returns_401_before_retrieval
  - test_upload_over_50mb_returns_413_before_parsing
  - test_query_lambda_package_size_under_250mb
  - test_ingestion_lambda_package_size_under_250mb
  - test_pdf_extraction_lambda_image_size_under_10gb

---

## Phase 5 — Hardening + Benchmark Parity (Days 43–50)

Goal: Production-ready, no silent failures, all benchmarks passing
together on both dev and prod providers.

Prerequisite: Phase 4 exit criteria fully passed. The 120-query
benchmark dataset and dev-mode Phase 3 verification (if not already
run under both providers) must exist before parity testing begins —
do not attempt provider parity against a dataset that only ran once.

Additional deliverables:
  - Dev vs prod provider parity benchmark (BENCHMARK.md): full
    120-query run under MODEL_PROVIDER=dev and again under
    MODEL_PROVIDER=prod, results compared side by side. If dev-mode
    Recall@5/Faithfulness falls more than 5% below prod-mode, QA
    proceeds prod-provider-only from that point forward.
  - Cold start latency benchmark, tracked separately from warm p95.
  - VPC endpoint configuration for Bedrock (regulated-deployment
    option, documented, not required for v1 default).
  - Both cache layers (pgvector semantic + Redis exact-match)
    confirmed active together, not just one — this was the exact
    gap caught and fixed before Phase 3 closed, re-verify it hasn't
    regressed.
  - Stress test: 500 queries, 0 unhandled errors.

Exit criteria: all BENCHMARK.md rev 5 targets met on BOTH warm-path
and provider-parity runs, specifically:
  - Fast path p95 latency (warm) < 400ms.
  - Fast path p95 latency (cold start) < 1500ms.
  - Cold start delta (p95_cold − p95_warm) < 1200ms.
  - Full pipeline p95 latency (warm) < 4000ms, both providers.
  - CI blocks merge if any benchmark regresses >5% from the Phase 3
    baseline recorded in benchmark_baseline.json.

Test Cases Required to Pass:
  - test_full_pipeline_p95_under_4000ms (prod provider)
  - test_full_pipeline_p95_under_4000ms (dev provider)
  - test_lambda_package_size_under_250mb (query_lambda)
  - test_fast_path_cold_start_under_1500ms
  - test_cold_start_delta_under_1200ms
