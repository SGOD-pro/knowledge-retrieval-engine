# PHASES.md — Build Sequence

> [!IMPORTANT]
> - **Rule 1:** Do not start Phase N+1 until Phase N exit criteria pass.
> - **Rule 2:** No graph code in Phase 1 or Phase 2. Build it only in Phase 3.
> - **Rule 3:** Benchmark targets are exit criteria, not aspirations.

---

## Phase 1 — Parsing + PageIndex (Days 1–7)
**Goal:** Ingest a PDF and query it structurally. No embeddings. No LLM.

### Deliverables
- `opendataloader-pdf` batch ingestion in `parse_service.py`.
- PostgreSQL schema for chunks with `bounding_box`, `element_type`, `section_depth`, `section_heading`.
- `page_index_service.py`: `structural_weight` scoring implemented.
- `POST /ingest` endpoint.
- `GET /documents/{id}` endpoint.

### Exit Criteria (all must pass)
- 100-page PDF ingested in <30 seconds.
- PageIndex returns correct top-5 structurally-weighted pages for 10/10 manually verified queries across 3 PDF types.
- `structural_weight` correctly scores heading matches 2–3x higher than footnote matches (verified by unit test).

---

## Phase 2 — Fast Path (BM25 + PageIndex + BGE) (Days 8–14)
**Goal:** Sub-200ms factual query answering with citation bounding boxes. No graph. No OKF. No LLM.

### Deliverables
- `bm25_retriever.py` (`rank-bm25`).
- `embed_service.py` + FAISS index (`BGE-small`).
- `vector_retriever.py` scoped to PageIndex candidate pages.
- `planner.py`: `fast_path` rule only.
- `response_builder.py` with `bounding_box` citation attachment.
- `POST /query` endpoint (fast path only).

### Exit Criteria (all must pass)
- Fast path p95 latency < 200ms on local hardware.
- Recall@3 > 0.75 on 40-query factual test set.
- All citations resolve to valid `bounding_box` in original PDF.
- BM25 runs before vector search (enforced by execution order test).
- Vector search scoped to PageIndex candidates (enforced by test).

---

## Phase 3 — OKF + Full Pipeline (Days 15–25)
**Goal:** Full reasoning path with typed knowledge layer, reranker, LLM. Graph is built here but activated conditionally.

### Deliverables
- `concept_service.py` (`spaCy` extraction).
- `normalize_service.py` (`BGE-small` clustering).
- `okf_builder.py`: Concept + Property + Relation schemas.
- `okf_retriever.py`: typed property lookup.
- `graph_retriever.py`: dict-based adjacency list, 2-hop max.
- `reranker.py` (`bge-reranker-base`).
- `fidelity_check.py`.
- `compressor.py`.
- `llm_service.py` (single call, structured output, T=0).
- Full `planner.py` (all 4 rules).
- LangGraph pipeline wiring all stages.
- `POST /query` endpoint (full pipeline).

### Exit Criteria (all must pass)
- Full pipeline p95 latency < 3000ms.
- Faithfulness score > 0.80 on 120-query test set.
- Hallucination rate < 5% (citation mismatch check).
- Context compression ratio > 30%.
- Entity coverage post-compression = 100% on fidelity tests.
- Graph only activates on relationship queries (planner test).
- LLM call count == 1 for full path queries (instrumented test).
- LLM call count == 0 for fast path queries.

---

## Phase 4 — API + Frontend (Days 26–35)
**Goal:** Working product that a real user can verify answers with.

### Deliverables
- FastAPI: all endpoints from `api.md`.
- Next.js 3-pane workspace:
  - **Left:** query input + history.
  - **Center:** answer card + `RetrievalPath` breadcrumb (stages + latencies).
  - **Right:** PDF viewer with `bounding_box` highlights on citations.
- Fast Match badge vs Reasoned Answer badge.
- Confidence badge (HIGH/MEDIUM/LOW).
- Thumbs up/down feedback (writes to DB, not used for reranking).
- Empty state: shows which stage failed, not generic "no results".

### Exit Criteria
- 5 real users complete 10 queries each.
- Average time-to-verify (user confirms answer against source): <15s.
- Zero unhandled errors in 100-query session.

---

## Phase 5 — Hardening (Days 36–42)
**Goal:** Production-ready. No silent failures. All benchmarks pass together.

### Deliverables
- Redis caching (TTL 24h, keyed by `query_hash + doc_scope_hash`).
- Cache invalidation on re-ingestion.
- Stale index detection (doc modified after last embed).
- All modules log `latency_ms + confidence_score` (CI-enforced).
- Conflicting source detection (two chunks contradict on same fact).
- Docker Compose for full local deployment.
- Stress test: 500 queries, 0 unhandled errors.

### Exit Criteria
- All `BENCHMARK.md` targets met simultaneously on same hardware.
- CI blocks merge if any benchmark regresses >5% from Phase 3 baseline.
- All Phase 1–3 test cases still pass.

