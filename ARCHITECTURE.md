## ARCHITECTURE.md (rev 4) — API-First, Lambda-Compatible

## Deployment Model Change (breaking change from rev 3)

REMOVED: all local model inference (BGE-small local, Nemotron-1B NVFP4
local, bge-reranker-base local). REMOVED: FAISS in-memory index loaded
at Lambda startup — incompatible with Lambda's stateless execution model.

NEW: fully API-first architecture. Lambda function contains ONLY:
  - Orchestration logic (planner, decomposer, fidelity check, compressor)
  - HTTP clients to external model APIs
  - DB clients (Postgres/pgvector, Redis)
  Deployment package target: <50MB zipped. No model weights ship in the zip.

## Hard Constraints (updated)
- Maximum ONE LLM call per query (unchanged).
- Fast path: p95 < 400ms (relaxed from 200ms — network calls replace
  local inference; 200ms was only achievable with local BGE-small).
- Full pipeline: p95 < 4000ms (relaxed from 3000ms — same reason,
  multiple sequential API calls now carry network latency each).
- Every module logs latency_ms and confidence_score.
- LLM receives only compressed context. Never raw chunks.
- Max tokens to LLM: 1200.
- Graph: MAX_NODES=40 absolute. MAX_HOPS=2 default, 3 on deep_causal_flag.
- No model weights in the Lambda deployment package. Zero exceptions.
- Every external model call has a Bedrock fallback config. OpenRouter
  free tier is dev/staging default; production defaults to Bedrock.

---

## Model Provider Matrix (NEW — the core of this revision)

| Function          | Dev/Staging (free/cheap)              | Production (reliable)          |
|--------------------|----------------------------------------|----------------------------------|
| Embedding          | nvidia/nemotron-3-embed-1b (OpenRouter, free) | amazon.titan-embed-text-v2 (Bedrock) |
| Reranker           | nvidia/llama-nemotron-rerank-vl-1b-v2 (OpenRouter, free, text-only mode) | cohere.rerank-v3-5 (Bedrock) |
| OKF extraction     | nvidia/nemotron-3-nano-30b-a3b (OpenRouter, free) | amazon.nova-micro-v1 (Bedrock) |
| Query LLM          | openai/gpt-oss-20b (OpenRouter, free) or nvidia/nemotron-nano-9b-v2 (free) | amazon.nova-lite-v1 or anthropic.claude-haiku (Bedrock) |
| CI faithfulness judge | — | amazon.nova-lite-v1 (Bedrock, unchanged from rev 3) |

Provider selection: config flag `MODEL_PROVIDER=dev|prod`. Never
hardcode a specific provider in retrieval/llm modules — always route
through `provider_client.py` which reads this flag.

Rationale for single embedding model (dropped dual-tier from rev 2/3):
  The BGE-small/Nemotron-1B split existed to trade local compute cost
  for speed on a local-inference deployment. On an API-first
  architecture, both paths pay network latency regardless of model
  size — the "cheap fast model" advantage evaporates. One model
  (Nemotron-3-Embed-1B, 2048-dim) for both paths removes an entire
  index, removes consistency-check code, and removes a class of bugs.
  Fast path stays fast by skipping stages (Graph, OKF, Reranker,
  LLM), not by using a smaller embedding model.

---

## Vector Store — pgvector on Aurora Serverless v2 (replaces FAISS)

Why FAISS is removed:
  FAISS requires loading the full index into process memory at
  startup. Lambda has no persistent startup — every cold start would
  require re-downloading and re-loading the index from S3, adding
  1-3+ seconds before the first query can even run. This directly
  breaks the fast-path latency target and makes cold starts unusable.

Replacement: PostgreSQL + pgvector extension, on Aurora Serverless v2.
  - Chunks table gets an `embedding vector(2048)` column.
  - HNSW index on embedding column for approximate nearest neighbor.
  - Query: standard SQL with `<=>` cosine distance operator.
  - No index loading step. No cold-start penalty for vector search.
  - Aurora Serverless v2 scales to zero-ish cost during idle (scales
    down to 0.5 ACU minimum, not fully zero, but far cheaper than
    always-on RDS for spiky Lambda-driven traffic).

Semantic cache (from rev 3) also moves to pgvector:
  cache_entries table: {query_embedding vector(2048), redis_key,
  doc_scope_hash, created_at}. Same 0.95 cosine threshold logic,
  now a SQL query instead of a FAISS lookup.
  `SELECT redis_key FROM cache_entries
   WHERE doc_scope_hash = %s
   ORDER BY query_embedding <=> %s LIMIT 1`
  then check returned distance against threshold in application code.

## OKF Graph Storage — Postgres recursive CTE (replaces adjacency dict)

Why the in-memory adjacency dict is removed:
  Same reason as FAISS — "load into memory at startup" doesn't exist
  as a pattern in Lambda. Every invocation is a fresh process.

Replacement: `relations` table in Postgres, traversal via recursive CTE.

  WITH RECURSIVE graph_walk AS (
    SELECT to_concept_id, relation_type, relation_weight, 1 AS hop
    FROM relations
    WHERE from_concept_id = ANY(%(start_entities)s)
      AND relation_weight >= 0.3
    UNION ALL
    SELECT r.to_concept_id, r.relation_type, r.relation_weight, g.hop + 1
    FROM relations r
    JOIN graph_walk g ON r.from_concept_id = g.to_concept_id
    WHERE g.hop < %(max_hops)s
      AND r.relation_weight >= 0.3
  )
  SELECT DISTINCT to_concept_id, relation_type, relation_weight, hop
  FROM graph_walk
  LIMIT 40;

  max_hops parameter: 2 default, 3 on deep_causal_flag (unchanged logic
  from rev 3, now expressed as a query parameter instead of Python loop).
  LIMIT 40 enforces MAX_NODES directly in SQL — cheaper than
  post-filtering in application code.

---

## Query Pipeline (updated stage implementations, same stage order)

Stage 0 — Semantic Cache Check
  pgvector query against cache_entries. Cosine >= 0.95 → return cached.

Stage 1 — BM25
  Unchanged logic (rank-bm25), but corpus loaded from Postgres per
  query rather than an in-memory structure. For corpora under 10k
  pages this is fast enough (<50ms) — benchmark to confirm per Phase 2.

Stage 2 — PageIndex
  Unchanged. Already Postgres-backed, no change needed.

Stage 3 — Vector Search
  API call to embedding provider (per Model Provider Matrix) to embed
  the query, then pgvector HNSW search scoped to PageIndex candidates.
  Network latency now included in this stage's budget: ~50-150ms
  for embedding API call + ~10-30ms for pgvector query.

Stage 4 — OKF Lookup
  Unchanged, Postgres property lookup, no model call.

Stage 5 — Graph Expansion (conditional)
  Recursive CTE per above. No in-memory traversal.

Stage 6 — Reranker
  API call to reranker provider. Batches all candidates (max 40) in
  ONE API call, not one call per candidate — check provider supports
  batch scoring (both Cohere Rerank and Nemotron Rerank do).

Stage 7 — Fidelity Check
  Unchanged, pure Python, no model call.

Stage 8 — Compression
  Unchanged, pure Python, no model call.

Stage 9 — Single LLM Call
  API call per Model Provider Matrix. Unchanged contract (Rule 2-15).

Stage 10 — Confidence Scoring
  Unchanged, deterministic formula.

---

## Module Map (rev 4)

kre/
├── providers/
│   ├── provider_client.py        # NEW: routes dev/prod per MODEL_PROVIDER flag
│   ├── embedding_provider.py     # NEW: OpenRouter Nemotron / Bedrock Titan
│   ├── reranker_provider.py      # NEW: OpenRouter Nemotron VL / Bedrock Cohere
│   └── llm_provider.py           # NEW: OpenRouter free models / Bedrock Nova
├── ingestion/
│   ├── format_router.py
│   ├── adapters/ (pdf/docx/xlsx/pptx, unchanged)
│   ├── parse_service.py
│   ├── page_index_service.py
│   ├── concept_service.py        # Tier1 regex + Tier1.5 SVO + Tier3 API extraction
│   ├── normalize_service.py      # now calls embedding_provider.py, not local model
│   └── okf_builder.py
├── retrieval/
│   ├── planner.py
│   ├── decomposer.py
│   ├── bm25_retriever.py         # queries Postgres per-request
│   ├── page_index_retriever.py
│   ├── vector_retriever.py       # calls embedding_provider.py + pgvector query
│   ├── okf_retriever.py
│   ├── graph_retriever.py        # recursive CTE, no adjacency dict
│   ├── reranker.py                # calls reranker_provider.py
│   ├── fidelity_check.py
│   └── compressor.py
├── llm/
│   └── llm_service.py             # calls llm_provider.py
├── api/
│   └── main.py                    # FastAPI locally / Lambda handler in prod
├── graph/
│   └── langgraph_pipeline.py
└── db/
    ├── postgres.py                # includes pgvector helpers
    └── redis_cache.py             # ElastiCache Serverless in Lambda deployment


