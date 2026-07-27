# PHASES.md — Build Sequence (rev 3)

## Rules (unchanged)
- Do not start Phase N+1 until Phase N exit criteria pass.
- No graph code in Phase 1 or Phase 2.
- Benchmark targets are exit criteria, not aspirations.
- Every phase must have passing test cases defined in RULES.md before exit.

---

## Phase 1 — Parsing + PageIndex + Multi-Format (Days 1–10)
*(Unchanged from prior spec - Tests already defined and passing)*

---

## Phase 2 — Fast Path (BM25 + PageIndex + Embeddings) (Days 11–17)
Updated for API-first architecture.

Deliverables:
  - bm25_retriever.py (rank-bm25).
  - providers/embedding_provider.py with dev(OpenRouter)/prod(Bedrock)
    routing implemented and tested.
  - pgvector HNSW index creation on chunks table.
  - vector_retriever.py: fast path uses pgvector query.
  - planner.py: fast_path rule only.
  - response_builder.py with bounding_box + fallback location reference.
  - POST /query (fast path only).

Exit criteria (all must pass):
  - Fast path p95 latency < 400ms (warm), not <200ms.
  - Recall@3 > 0.75 on 40-query factual test set.
  - Citations resolve to bounding_box (PDF) or location reference
    (DOCX/XLSX/PPTX) — zero null location responses.
  - BM25 before vector search enforced by test.
  - Vector search scoped to PageIndex candidates.

Test Cases Required to Pass:
  - test_r01_fast_path_zero_llm_calls
  - test_r05_bm25_before_pageindex_before_vector
  - test_r05_vector_search_only_within_candidate_pages
  - test_r20_all_citations_have_location
  - test_fast_path_p95_under_400ms

---

## Phase 3 — Reranker + OKF + Full Pipeline (Days 18–30)
Updated for provider-first and Postgres-backed graph traversal.

Deliverables:
  - concept_service.py:
      Tier 1: regex patterns for dates/percentages/identifiers.
      Tier 3: dev/prod extraction for OKF properties.
              (Tier 2 is normalize_service.py below)
  - normalize_service.py: embedding-based clustering via provider layer.
  - providers/reranker_provider.py.
  - providers/llm_provider.py.
  - okf_builder.py: Concept + Property + Relation schemas.
  - okf_retriever.py: typed property lookup.
  - graph_retriever.py: rewritten for Postgres recursive CTE traversal,
    no in-memory structure.
  - reranker.py.
  - fidelity_check.py.
  - compressor.py.
  - llm_service.py (single call, T=0, structured output).
  - Full planner.py (all 4 rules).
  - vector_retriever.py: full path uses pgvector search.
  - LangGraph pipeline wiring all stages.
  - POST /query (full pipeline).

QUALITY GATE (run before continuing Phase 3):
  - Extract OKF properties from 10 real documents using both dev and prod
    extraction models.
  - Manually verify extracted properties against source chunks.
  - Precision must be >= 85% for each provider independently.
  - If either provider falls below 85%: do not ship that provider path.

Exit criteria (all must pass):
  - Quality gate passed for both providers.
  - Full pipeline p95 latency < 4000ms (warm).
  - Faithfulness > 0.80 on 120-query test set.
  - Hallucination rate < 5%.
  - Context compression ratio > 30%.
  - Entity coverage post-compression = 100%.
  - Graph activates only on relationship queries.
  - LLM call count == 1 for full path, 0 for fast path.
  - Nova Micro call count == 0 for any query (ingestion only).

Test Cases Required to Pass:
  - test_r02_full_path_exactly_one_llm_call
  - test_r02_planner_zero_llm_calls
  - test_r02_okf_lookup_zero_llm_calls
  - test_r03_llm_receives_compressed_context
  - test_r04_token_limit_never_exceeded
  - test_r06_reranker_before_compressor
  - test_r10_all_modules_log_required_fields
  - test_r13_graph_max_2_hops_enforced
  - test_r13_graph_max_40_nodes_enforced
  - test_r13_low_weight_edges_pruned
  - test_r14_fidelity_failure_blocks_llm
  - test_r15_llm_output_has_no_confidence_field
  - test_r15_confidence_computed_from_deterministic_formula
  - test_r16_graph_not_activated_for_factual_query
  - test_r16_graph_activated_for_relationship_query
  - test_r18_nova_micro_zero_query_time_calls
  - test_hallucination_citation_mismatch_rate

---

## Phase 4 — API + Frontend + Lambda Packaging (Days 31–42)
Updated for Lambda deployment and Frontend Vitest integration.

Deliverables (additions to prior spec):
  - Lambda handler wrapping the FastAPI app (via Mangum or similar
    ASGI adapter) alongside the existing local FastAPI dev server.
  - Package size check in CI (<50MB zipped).
  - Provisioned concurrency configuration for query Lambda
    (decision deferred to Phase 5 cost analysis).
  - Frontend Setup:
    - PDF viewer: bounding_box highlight (unchanged).
    - DOCX/XLSX/PPTX: location reference display
      ("Sheet: Revenue, Row: 14" instead of PDF highlight).
    - Source format badge on each citation chip.
  - Frontend Testing Suite (Vitest + React Testing Library):
    - Configure Vitest in `frontend/vite.config.ts`.
    - API Mock tests for POST /query (success, 404, 500 errors).
    - Component snapshot/UI breakage tests (Citation chips, 3-pane layout).

Exit criteria: 
  - Backend unchanged from prior spec, plus packaging gate passes (<50MB).
  - All Vitest test suites pass with 0 UI breakage failures.
  - Frontend successfully handles API responses with varying location_reference formats.
  - CORS configuration verified via test (frontend origin explicitly allowed in FastAPI).

Test Cases Required to Pass (Frontend/Vitest):
  - test_api_query_success_renders_citations
  - test_api_query_handles_not_found
  - test_citation_chip_displays_bounding_box
  - test_citation_chip_displays_location_reference
  - test_cors_headers_present_on_api_response

---

## Phase 5 — Hardening + Benchmark Parity (Days 43–50)
Updated for the API-first deployment model.

Additional deliverables:
  - Dev vs prod provider parity benchmark (BENCHMARK.md).
  - Cold start latency benchmark, tracked separately.
  - VPC endpoint configuration for Bedrock (regulated-deployment
    option, documented not required for v1 default).

Exit criteria: all BENCHMARK.md rev 4 targets met on BOTH warm-path
and provider-parity runs.

Test Cases Required to Pass:
  - test_full_pipeline_p95_under_4000ms (prod provider)
  - test_full_pipeline_p95_under_4000ms (dev provider)
  - test_lambda_package_size_under_50mb
  - test_cold_start_latency_under_5000ms
