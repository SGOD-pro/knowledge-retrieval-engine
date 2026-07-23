# PHASES.md — Build Sequence (rev 4)

## Rules (unchanged)
- Do not start Phase N+1 until Phase N exit criteria pass.
- No graph code in Phase 1 or Phase 2.
- Benchmark targets are exit criteria, not aspirations.

---

## Phase 1 — Parsing + PageIndex + Multi-Format (Days 1–10)
Extended by 3 days to account for format_router.

Goal: Ingest PDF/DOCX/XLSX/PPTX and query structurally.
No embeddings. No LLM. No graph.

Deliverables:
  - format_router.py with pdf/docx/xlsx/pptx routing.
  - pdf_adapter.py (opendataloader-pdf batch).
  - docx_adapter.py (python-docx).
  - xlsx_adapter.py (openpyxl, formula → computed value).
  - pptx_adapter.py (python-pptx, speaker notes as caption chunks).
  - parse_service.py: unified chunk schema with nullable bounding_box.
  - PostgreSQL schema: all fields including source_format, bounding_box nullable.
  - PostgreSQL schema now includes `embedding vector(N)` column and
    pgvector extension enabled at database creation. No FAISS setup.
  - page_index_service.py: structural_weight scoring.
  - POST /ingest endpoint (accepts any supported format).
  - GET /documents/{id} endpoint.

Exit criteria (all must pass):
  - 100-page PDF ingested in <30 seconds.
  - 50-page DOCX ingested in <15 seconds.
  - 10-sheet XLSX ingested in <10 seconds.
  - 30-slide PPTX ingested in <10 seconds.
  - PageIndex returns correct top-5 for 10/10 queries
    across 3 PDF types AND 1 DOCX and 1 XLSX.
  - structural_weight scores heading matches 2–3x footnote matches.
  - bounding_box is non-null for all PDF chunks,
    null for all XLSX chunks (correct nullable behavior).

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

---

## Phase 4 — API + Frontend + Lambda Packaging (Days 31–42)
Updated for Lambda deployment.

Deliverables (additions to prior spec):
  - Lambda handler wrapping the FastAPI app (via Mangum or similar
    ASGI adapter) alongside the existing local FastAPI dev server.
  - Package size check in CI (<50MB zipped).
  - Provisioned concurrency configuration for query Lambda
    (decision deferred to Phase 5 cost analysis).
  - PDF viewer: bounding_box highlight (unchanged).
  - DOCX/XLSX/PPTX: location reference display
    ("Sheet: Revenue, Row: 14" instead of PDF highlight).
  - Source format badge on each citation chip.

Exit criteria: unchanged from prior spec, plus packaging gate passes.

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


