# PROJECT_REQUIREMENTS.md — Dependencies, Versions, and Provider Accounts

This file is the single source of truth for what gets installed,
which exact versions, and what accounts/credentials are needed before
Phase 1 can start. If a dependency isn't listed here, it doesn't go
in requirements.txt without updating this file first.

---

## 1. Runtime

```text
Python: 3.12.x (Lambda-supported runtime, matches Mangum compatibility)
Node.js: 20.x LTS (for Next.js frontend)
```

---

## 2. Document Parsing

| Format | Library | Version | License | Notes |
|--------|---------|---------|---------|-------|
| PDF | `opendataloader-pdf` | latest (pin exact version at install) | Apache-2.0 | Java-based (JVM per call) — batch all files in ONE call, never per-document |
| DOCX | `python-docx` | >=1.1.2 | MIT | Reads Heading 1/2/3 styles for section_depth |
| XLSX | `openpyxl` | >=3.1.5 | MIT | `data_only=True` to read computed values, not formulas |
| PPTX | `python-pptx` | >=1.0.2 | MIT | Reads speaker notes via `slide.notes_slide` |

```bash
pip install opendataloader-pdf python-docx openpyxl python-pptx
```

**opendataloader-pdf runtime dependency:** requires a JVM (Java 11+)
available in the execution environment. On Lambda this means either
a custom container image with JRE bundled, or a Lambda layer with a
minimal JRE — a bare `pip install` alone will NOT work in a standard
Lambda zip deployment. **Resolve this in Phase 1, day 1** — it is the
single most likely blocker in the entire ingestion pipeline, since it
conflicts with the "<50MB zipped, no heavy binaries" Lambda constraint
elsewhere in this spec.

Decision required before Phase 1 code starts:
- Option A: Lambda container image (10GB limit, not the 250MB zip
  limit) for the ingestion Lambda specifically. Query Lambda stays
  on the zip deployment path since it has no JVM dependency.
- Option B: Run ingestion as an ECS Fargate task instead of Lambda,
  triggered by the same API. Query path remains Lambda.
Pick one and document the choice in ARCHITECTURE.md before Phase 1
Day 1 — this affects the ingestion deployment target, not just a
library choice.

---

## 3. Retrieval — Local, Deterministic Components

| Component | Library | Version | License | Notes |
|-----------|---------|---------|---------|-------|
| BM25 | `rank-bm25` | >=0.2.2 | Apache-2.0 | Pure Python, no compiled binary, Lambda-safe |
| Regex/NLP utils | Python `re` (stdlib) | — | — | No external NER library — spaCy explicitly excluded |
| Query orchestration | `langgraph` | >=0.2.0 (pin at install) | MIT | Conditional-edge routing for planner |
| Query orchestration | `langchain-core` | >=0.3.0 | MIT | Only core primitives — NOT langchain's built-in retriever/vectorstore abstractions (Rule 28 / architecture note: raw functions preferred for latency visibility) |

```bash
pip install rank-bm25 langgraph langchain-core
```

---

## 4. Database and Storage

| Component | Service/Library | Version | Notes |
|-----------|-------------------|---------|-------|
| Relational + vector store | PostgreSQL | 16.x | Aurora Serverless v2 in prod |
| Vector extension | `pgvector` | >=0.7.0 | Must be enabled via `CREATE EXTENSION vector;` at DB creation |
| Postgres driver | `psycopg[binary]` | >=3.2.0 | Async-capable, binary wheel avoids compiling on Lambda |
| Cache | ElastiCache Serverless (Redis-compatible) | Redis 7.x protocol | Prod |
| Cache client | `redis` (py) | >=5.0.0 | — |
| Local dev DB | Docker Compose: `postgres:16` + `pgvector/pgvector:pg16` image | — | Matches prod schema exactly |

```bash
pip install "psycopg[binary]" redis
```

```sql
-- Run once per database
CREATE EXTENSION IF NOT EXISTS vector;
```

---

## 5. API Framework and Lambda Packaging

| Component | Library | Version | Notes |
|-----------|---------|---------|-------|
| API framework | `fastapi` | >=0.115.0 | — |
| ASGI server (local dev) | `uvicorn[standard]` | >=0.30.0 | Local only, not shipped in Lambda package |
| Lambda ASGI adapter | `mangum` | >=0.19.0 | Wraps FastAPI app for Lambda handler |
| Data validation | `pydantic` | >=2.9.0 | Ships with FastAPI, pin explicitly |

```bash
pip install fastapi mangum pydantic
# local dev only, exclude from Lambda build:
pip install "uvicorn[standard]"
```

---

## 6. Frontend

| Component | Library | Version | Notes |
|-----------|---------|---------|-------|
| Framework | Next.js | 14.x | App Router |
| Styling | Tailwind CSS | 3.4.x | — |
| PDF rendering | `react-pdf` (pdf.js wrapper) | >=9.0.0 | For bounding-box overlay rendering in the citation viewer |
| State | React built-in (useState/useContext) | — | No external state library needed for v1 scope |

```bash
npx create-next-app@14 --typescript --tailwind
npm install react-pdf
```

---

## 7. Explicitly Forbidden Dependencies (enforced by Rule 27 CI check)

```text
torch
tensorflow
onnxruntime
transformers      # (with model weights — API-only usage of hosted
                    models is fine, importing the library to run
                    local inference is not)
sentence-transformers
faiss / faiss-cpu / faiss-gpu
spacy             # explicitly removed, replaced by regex + API embeddings
networkx          # rejected for production per DECISION.md
```

CI check (`test_r27_no_forbidden_dependencies`) fails the build if
any of these appear in `requirements.txt`, including as a transitive
dependency. Run `pip show <package>` reverse-dependency check if a
library you DO want pulls one of these in — find an alternative
rather than allow-listing an exception.

---

## 8. Model Provider Accounts (required before Phase 2)

### OpenRouter (dev/staging)
- Account: openrouter.ai, API key generated under account settings.
- Free-tier models used (verify current availability before each
  phase — free-tier model list changes without notice, this is a
  known and accepted risk per BOUNDARIES.md):
  - `nvidia/nemotron-3-embed-1b:free` — embedding
  - `nvidia/llama-nemotron-rerank-vl-1b-v2:free` — reranker (text-only mode)
  - `nvidia/nemotron-3-nano-30b-a3b:free` — OKF Tier 3 extraction
  - `openai/gpt-oss-20b:free` or `nvidia/nemotron-nano-9b-v2:free` — query LLM
- Rate limits: check current OpenRouter free-tier limits before
  load testing — these are NOT documented as stable and must be
  re-verified at the start of every phase that depends on them.

### AWS Bedrock (prod)
- AWS account with Bedrock model access requested and approved for:
  - `amazon.titan-embed-text-v2:0` — embedding
  - `cohere.rerank-v3-5:0` — reranker
  - `amazon.nova-micro-v1:0` — OKF Tier 3 extraction
  - `amazon.nova-lite-v1:0` — query LLM and CI faithfulness judge
- Bedrock model access is NOT automatic — must be explicitly enabled
  per-model in the Bedrock console under "Model access" before first
  API call. **Do this in Phase 1**, not Phase 3, since approval can
  take time depending on account type and region.
- IAM role for Lambda execution needs `bedrock:InvokeModel` permission
  scoped to the specific model ARNs above — not wildcard `bedrock:*`.

### AWS Infrastructure (prod)
- Aurora Serverless v2 cluster (PostgreSQL 16-compatible), min 0.5 ACU.
- ElastiCache Serverless (Redis-compatible) cache.
- Lambda execution role with VPC access if Aurora/ElastiCache are in
  a private VPC (standard for Aurora Serverless v2).
- S3 bucket for raw document upload staging (pre-ingestion).

---

## 9. Development Environment Setup Order

Do these in order — later steps assume earlier ones are done:

1. Provision Postgres locally via Docker Compose (`pgvector/pgvector:pg16`).
2. Run `CREATE EXTENSION vector;` and apply the chunks/relations/
   cache_entries schema (see MEMORY.md, ARCHITECTURE.md for exact
   column definitions).
3. Resolve the opendataloader-pdf JVM dependency decision (Section 2)
   before writing `pdf_adapter.py`.
4. Create OpenRouter account, generate API key, verify all four
   free-tier models listed above are currently accessible (they may
   have changed — check openrouter.ai/models before assuming).
5. Request Bedrock model access for all four models listed above —
   submit this on day 1 regardless of which phase needs it, since
   approval isn't instant.
6. Install Python dependencies from Sections 2–5 above.
7. Run `test_r27_no_forbidden_dependencies` locally before writing
   any ingestion code, to confirm the environment is clean from the start.

---

## 10. Version Pinning Policy

`requirements.txt` uses exact pins (`==`), not ranges, once Phase 1
passes its exit criteria. Ranges (`>=`) shown above are for initial
setup only. After Phase 1 sign-off, freeze versions with
`pip freeze > requirements.txt` and only bump deliberately, with a
benchmark re-run per the Regression Policy in BENCHMARK.md — a
library version bump counts as a change that could affect retrieval
or latency behavior.