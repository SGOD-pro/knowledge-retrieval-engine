# PROJECT.md — Knowledge Retrieval Engine (KRE)

## What We Are Building

An enterprise document intelligence platform where every answer is traceable to an exact page and paragraph in the source PDF.

### Core Innovation
1. **PageIndex** — a structural positional index.
2. **OKF (Ontology-driven Knowledge Framework)** — a typed semantic layer.
3. **Staged Hybrid Retrieval** — cheapest filter runs first. BM25 -> PageIndex -> Vector.

## Why This Architecture — Cost, Latency, and Utilization Reasoning

Traditional vector RAG, knowledge graphs, and structural indexing
each have a well-known failure mode. KRE's three-layer design exists
specifically because each layer's weakness is covered by a different
layer's strength — this is a deliberate compensating design, not
three features bolted together.

| Layer | Weakness alone | What covers it |
|-------|-----------------|-----------------|
| Vector search (BGE-small/Titan) | Finds semantically plausible but structurally irrelevant chunks; no concept of document hierarchy | PageIndex pre-filters candidate pages by structural_weight BEFORE vector search runs, shrinking the search space and correcting for hierarchy blindness |
| Knowledge graph / relationship traversal | Expensive, error-prone at scale, most queries don't need multi-hop reasoning at all | OKF gives typed properties via a flat Postgres lookup — zero model calls, zero traversal — for the majority of "what is X's value" queries; the graph only activates when `relationship_flag` is actually detected |
| LLM reasoning | Hallucinates when given ambiguous or excessive context; expensive per call | Staged retrieval (BM25 → PageIndex → Vector → OKF → Graph?) does all narrowing and fact-lookup deterministically before the LLM is ever invoked — the LLM's job is reduced to explaining pre-verified facts, not finding or computing them (Rules 1, 7, 8) |

### Cost discipline, concretely

- Fast-path queries (target: a majority of real traffic, tracked via
  the "LLM activation rate <60%" benchmark) use LOCAL BGE-small
  embeddings — zero embedding API cost, zero LLM cost, zero network
  hop for the embedding step. This is the single biggest cost lever
  in the system: the most common query type is also the cheapest.
- OKF Tier 3 extraction (the only LLM cost in the entire ingestion
  path) runs once per document, batched, never per query — it is
  amortized, not recurring.
- Graph traversal is gated behind `relationship_flag`, not run by
  default, and capped at MAX_NODES=40 regardless of hop count — so
  even when it does run, its cost is bounded and predictable.
- Every retrieval stage before the LLM call is deterministic code,
  not a model call (Rule 1) — the LLM is invoked at most once per
  query (Rule 2), and only after the deterministic pipeline has
  already reduced the candidate set and verified entity coverage
  (fidelity_check, Rule 14).

### Latency discipline, concretely

- Local BGE-small removes a network round-trip from the fast path,
  which is why fast path has the tightest latency budget (<400ms)
  and full path does not (<4000ms) — the architecture puts local
  inference exactly where the tightest SLA is, and API calls exactly
  where more budget exists to absorb them.
- PageIndex's structural pre-filter runs before either embedding
  path, so vector search — local or API — never searches the full
  corpus, only a pre-narrowed candidate set. This is what keeps
  Recall@k high without needing to embed and compare every chunk in
  the document set on every query.
- Graph traversal, when it runs, executes as a single recursive CTE
  in Postgres (not an in-memory library, not a separate service call)
  — no additional network hop beyond the existing DB connection.

### Utilization discipline, concretely

- Each layer does the job it's actually good at and nothing else:
  BM25/PageIndex handle lexical and structural matching, vector
  search handles semantic matching, OKF handles typed fact lookup,
  graph handles relationship traversal, and the LLM handles natural-
  language synthesis of already-verified facts. No layer is asked to
  compensate for a job outside its strength (e.g., the LLM is never
  asked to compute statistics — Rule 7 — because deterministic code
  already did that upstream).
- This is why the planner (DECISION.md Rules 1–4) exists as a hard
  router rather than a soft ranking: sending a simple factual query
  through the full graph+OKF+rerank+LLM pipeline would be strictly
  worse on cost and latency with no accuracy benefit, so the router
  actively prevents that instead of relying on the pipeline to be
  "smart" about skipping unnecessary work internally.

### Tech Stack
```text
Backend:   FastAPI + LangGraph + Python + Boto3
Architecture: Three-Lambda Distributed Setup (PDF Extraction, Ingestion, Query)
Parsing:   opendataloader-pdf (Isolated in ECR Container Lambda)
Retrieval: BM25 → PageIndex → Vector (Local BGE-small for fast path, API for full path) → OKF → Graph
LLM:       Single call, API-driven via Bedrock/OpenRouter
Storage:   RDS PostgreSQL (pgvector) + ElastiCache Redis (`floci` emulation in dev)
Frontend:  Next.js 3-pane workspace
```

---

## Business Impact
Verifiable RAG for regulated industries. Citation-to-bounding-box in under 3 seconds (fast path).

## Competitive Landscape
[Same as rev 4]

## What Makes KRE Hard to Copy
1. PageIndex structural scoring.
2. OKF Knowledge Layer.
3. Institutional trust lock-in via audit trail.

## What This Is Not (v1)
- Not a chatbot or conversational assistant.
- Not a multi-tenant SaaS with authentication (v2).
- Not a real-time document sync system.
- Not a SQL / structured query interface.
