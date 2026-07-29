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
[Same as rev 4]

---

## Phase 5 — Hardening + Benchmark Parity (Days 43–50)
[Same as rev 4]
