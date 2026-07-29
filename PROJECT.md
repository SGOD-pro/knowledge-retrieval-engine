# PROJECT.md — Knowledge Retrieval Engine (KRE)

## What We Are Building

An enterprise document intelligence platform where every answer is traceable to an exact page and paragraph in the source PDF.

### Core Innovation
1. **PageIndex** — a structural positional index.
2. **OKF (Ontology-driven Knowledge Framework)** — a typed semantic layer.
3. **Staged Hybrid Retrieval** — cheapest filter runs first. BM25 -> PageIndex -> Vector.

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
