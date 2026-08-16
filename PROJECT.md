# PROJECT.md — Knowledge Retrieval Engine (KRE)

## What We Are Building

An enterprise document intelligence platform where every answer is traceable to an exact page and paragraph in the source PDF.

### Core Innovation
1. **PageIndex** — a structural positional index.
2. **OKF (Ontology-driven Knowledge Framework)** — a typed semantic layer that extracts structured facts at ingestion time for zero-LLM query lookups.
3. **Staged Hybrid Retrieval** — cheapest filter runs first. BM25 -> PageIndex -> Vector.

## The True Vision of KRE

We are building a system that eliminates the weaknesses of individual retrieval methods:

- **Traditional Vector RAG Weakness:** It chops documents into blind chunks. If a table or a continuous paragraph is split in half, Vector RAG fails. It also has no concept of document hierarchy (it treats a footnote the same as a main heading).
- **PageIndex Solution:** Before we do any semantic search, we use the document's structural hierarchy (Headings, Pages, Sheets, Slides) to filter the search space. We know *where* to look.
- **OKF (Ontology-driven Knowledge Framework) Solution:** Traditional RAG forces the LLM to read raw text and "compute" facts (e.g., reading a paragraph to find the revenue number). This causes hallucinations. OKF extracts typed facts at ingestion time (e.g., `{Concept: Revenue, Property: Q3, Value: $1.2M}`). At query time, we do a zero-LLM database lookup. Users don't only dump text files — they also upload CSV, Excel, etc. OKF captures concepts, properties, and relationships from ALL formats, forming a knowledge graph that knows exactly where to find structured answers.
- **The LLM's Role:** The LLM is only used to synthesize natural language (e.g., "Describe this to me") or explain pre-verified facts. It receives only the compressed, highly relevant chunks and OKF facts, never the whole page.

## Why This Architecture — Cost, Latency, and Utilization Reasoning

Traditional vector RAG, knowledge graphs, and structural indexing
each have a well-known failure mode. KRE's three-layer design exists
specifically because each layer's weakness is covered by a different
layer's strength — this is a deliberate compensating design, not
three features bolted together.

| Layer | Weakness alone | What covers it |
|-------|-----------------|-----------------| 
| Vector search (BGE-small/Titan) | Finds semantically plausible but structurally irrelevant chunks; no concept of document hierarchy | PageIndex pre-filters candidate pages by structural_weight BEFORE vector search runs, shrinking the search space and correcting for hierarchy blindness |
| Knowledge graph / relationship traversal | Expensive, error-prone at scale, most queries don't need multi-hop reasoning at all | OKF gives typed properties via a flat DynamoDB lookup — zero model calls, zero traversal — for the majority of "what is X's value" queries; the graph only activates when `relationship_flag` is actually detected |
| LLM reasoning | Hallucinates when given ambiguous or excessive context; expensive per call | Staged retrieval (BM25 → PageIndex → Vector → OKF → Graph?) does all narrowing and fact-lookup deterministically before the LLM is ever invoked — the LLM's job is reduced to explaining pre-verified facts, not finding or computing them (Rules 1, 7, 8) |

### Cost discipline, concretely

- Fast-path queries (target: a majority of real traffic, tracked via
  the "LLM activation rate <60%" benchmark) use the **BGE-small
  microservice** (local ONNX inference) — zero embedding API cost, zero LLM cost, zero network
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

- The BGE-small microservice removes a network round-trip from the fast path,
  which is why fast path has the tightest latency budget (<400ms)
  and full path does not (<4000ms) — the architecture puts local
  inference exactly where the tightest SLA is, and API calls exactly
  where more budget exists to absorb them.
- PageIndex's structural pre-filter runs before either embedding
  path, so vector search — local or API — never searches the full
  corpus, only a pre-narrowed candidate set. This is what keeps
  Recall@k high without needing to embed and compare every chunk in
  the document set on every query.
- Graph traversal, when it runs, executes against DynamoDB
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

### The Model Strategy (Bedrock + NVIDIA)

| Model | Role | When |
|-------|------|------|
| Amazon Nova Micro | OKF fact extraction | Ingestion only. Never at query time. |
| Amazon Titan Embeddings V2 | 1024-dim vectors | Full Path (complex queries) |
| BGE-small-en-v1.5 (ONNX Microservice) | 384-dim vectors | Fast Path (simple queries), zero network latency |
| NVIDIA Llama-Nemotron Rerank | Cross-encoder reranking | Sorts top 15–20 candidates down to top 6 |
| Amazon Nova Lite | Natural language synthesis | Called exactly once at end of pipeline |

### Tech Stack
```text
Backend:   FastAPI + LangGraph + Python + Boto3
Architecture: Three-Lambda Distributed Setup (PDF Extraction, Ingestion, Query) + BGE-Small Microservice
Parsing:   opendataloader-pdf (Isolated in ECR Container Lambda)
Retrieval: BM25 → PageIndex → Vector (BGE-small microservice for fast path, Titan API for full path) → OKF → Graph
LLM:       Single call, API-driven via Bedrock
Storage:   DynamoDB (documents, chunks, OKF entities/properties) + QdrantDB (vector search) + ElastiCache Redis (cache)
Frontend:  Next.js 3-pane workspace
```

---

## Business Impact
Verifiable RAG for regulated industries. Citation-to-bounding-box in under 3 seconds (fast path).

## Competitive Landscape

| Competitor | Approach | Core Failure |
| :--- | :--- | :--- |
| **ChatGPT + PDF** | Flat vector RAG | Unverifiable, no source pinning |
| **Perplexity** | Web search + LLM | Not document-private |
| **Notion AI** | Flat keyword in notes | No hierarchy, no semantic layer |
| **LlamaIndex** | Framework only | User assembles, no quality guarantee |
| **Azure AI Search** | BM25 + vector hybrid | Black box, no audit trail |
| **Vertex AI Search** | Google-managed RAG | Cloud-locked, no structural index |
| **Elasticsearch** | BM25 + dense vector | No document hierarchy, no OKF |

---

## What Makes KRE Hard to Copy
1. PageIndex structural scoring.
2. OKF Knowledge Layer (Ontology-driven Knowledge Framework).
3. Institutional trust lock-in via audit trail.

## What This Is Not (v1)
- Not a chatbot or conversational assistant.
- Not a multi-tenant SaaS with authentication (v2).
- Not a real-time document sync system.
- Not a SQL / structured query interface.
