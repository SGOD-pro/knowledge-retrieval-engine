# ARCHITECTURE.md (rev 7) — Hybrid Architecture (FastAPI Engine + Auxiliary Lambdas)

## Deployment Model (rev 7 — Hybrid Architecture)

The system employs a **Hybrid Architecture** combining a unified core engine with isolated auxiliary Lambdas for heavy runtime dependencies:

1. **CORE ENGINE / API (`backend/src/`)**:
   - Built as a unified **FastAPI application** (`main.py`) orchestrating the LangGraph retrieval pipeline, DynamoDB/Qdrant repository, and caching layer.
   - **Deployment Modes:**
     - **Standalone Service / Container:** Runs directly via `uvicorn main:app --host 0.0.0.0 --port 8000` (used for local development, benchmark evaluations, and containerized cloud deployment).
     - **Serverless Lambda:** Wrapped with **Mangum** in `backend/src/lambda.py` (`handler = Mangum(app)`) for single-deployment API Gateway + AWS Lambda execution (<250MB unzipped).
2. **PDF EXTRACTION LAMBDA (`odl-parser-lambda` / `odl/`)**:
   - Container image Lambda (ECR-hosted, 10GB limit) bundling Java/JRE + `opendataloader-pdf`.
   - Invoked synchronously via boto3 from `pdf_adapter.py` for complex PDF chunk extraction. In dev/local mode, can be imported directly or executed locally.
3. **BGE EMBEDDING LAMBDA (`bge_microservice/`)**:
   - Dedicated AWS Lambda handler (`bge_microservice/main.py:lambda_handler`) hosting `BGE-small-en-v1.5` ONNX weights (`bge-onnx/model.onnx`).
   - Invoked via boto3 (`bge-microservice-stack-BGELambdaFunction-roIuowXCDxCe`) by `embed_service.py` to keep heavy ONNX weights out of the core lambda bundle.
4. **INGESTION PATH (Canonical vs. Deferred)**:
   - **Canonical v1 Ingestion Path:** Direct multipart upload via FastAPI `/ingest` (`api/routes.py` -> `ingestion_lambda/parse_service.py:ingest_document`). This is the sole active ingestion pipeline for v1, executing document parsing, format adapters, OKF extraction via Nova Micro, dual-column embeddings, and database persistence.
   - **S3-Event Worker (`backend/src/ingestion_lambda/main.py`):** **Intentionally deferred to v2 / Backlog**. The event-driven S3 handler is currently a stub. Direct HTTP upload completely covers all current frontend and API use cases; asynchronous S3 event-driven batch ingestion is reserved as a future scaling enhancement for bulk document drops without HTTP connection timeout constraints.
5. **FRONTEND UI (`frontend/`)**:
   - React 18, Vite, TypeScript, Tailwind CSS single-page app deployed via static hosting (S3/CloudFront/Vercel).

## Hard Constraints
- Maximum ONE LLM call per query (Nova Micro during ingestion is exempt).
- Fast path: p95 < 400ms. Local BGE-small / ONNX microservice eliminates Bedrock latency.
- Full pipeline: p95 < 4000ms.
- Every module logs latency_ms and confidence_score.
- LLM receives only compressed context. Never raw chunks.
- Max tokens to LLM: 1200.
- Graph: MAX_NODES=40 absolute. MAX_HOPS=2 default.
- Deployment limits: **<250MB unzipped** for zip deployments, **10GB** for container images.
- Heavy binaries (Java JRE for PDF parsing, standalone ONNX embedding) are separated into specialized auxiliary Lambdas with local fallback.

## Model Provider Matrix

| Function          | Dev/Staging                             | Production (real AWS)          |
|-------------------|-----------------------------------------|----------------------------------|
| Fast Path Embed   | BGE-small-en-v1.5 (Local ONNX Fallback) | BGE-small-en-v1.5 (Lambda / ONNX)|
| Full Path Embed   | amazon.titan-embed-text-v2 (Bedrock)    | amazon.titan-embed-text-v2 (Bedrock) |
| Reranker          | nvidia/llama-nemotron-rerank-1b-v2 (NVIDIA NIM) | nvidia/llama-nemotron-rerank-1b-v2 (NVIDIA NIM) |
| OKF extraction    | amazon.nova-micro-v1 (Bedrock)          | amazon.nova-micro-v1 (Bedrock) |
| Query LLM         | amazon.nova-lite-v1 (Bedrock)           | amazon.nova-lite-v1 (Bedrock) |

## Storage — DynamoDB + QdrantDB + Redis

### DynamoDB (Single Table Design)
- **Documents & Chunks:** `kre-table` with PK/SK pattern (`DOC#<id>` / `CHUNK#<id>`).
- **OKF Entities:** `okf_entities` table — stores concepts extracted by Nova Micro during ingestion.
- **OKF Properties:** `okf_properties` table — stores typed properties linked to concepts.
- **OKF Relations:** `okf_relations` table — stores adjacency entries for graph traversal.

### OKF Implementation Scope (DynamoDB-Only)
KRE's OKF implementation is purely **DynamoDB-backed**, inspired by Google's Open Knowledge Format vocabulary (concepts, properties, typed relations). It **does NOT implement** file-based markdown bundles, YAML frontmatter schemas, or `index.md` manifests from Google OKF v0.1 or v0.2.
- **Storage:** Dedicated DynamoDB tables (`okf_entities`, `okf_properties`, `okf_relations`).
- **Extraction:** Nova Micro extracts structured concepts, properties, and relations at ingestion time into DynamoDB.
- **Tradeoffs:** No disaster recovery without re-running Nova Micro extraction; no filesystem bundle export; governance metadata (`verified`, `status`, `stale_after`, `type` hierarchy) is not tracked. This is a deliberate architectural scope decision for high-performance sub-10ms lookups.


### QdrantDB (Vector Search)
- Collection `kre_chunks` with two named vectors, never merged:
  - `embedding_fast` (384-dim) for BGE-small embeddings.
  - `embedding_full` (1024-dim) for Titan V2 API embeddings.
- Both vectors populated at ingestion time for every chunk.
- Query-time routing rule (Rule 19): fast-path queries search ONLY `embedding_fast`. Full-path queries search ONLY `embedding_full`. No query ever compares against both vectors.

### Redis (Cache Layer)
- Exact-match query cache with 24h TTL.
- Semantic cache layer via cosine similarity over query embeddings.
- Write-guard: only cache responses with confidence >= 0.50 and answer != "NOT_FOUND".

## CORS & Security

### Dev Environment
- Allowed origins: `http://localhost:5173`, `http://localhost:5174` (Vite dev server).
- Allowed methods: GET, POST, OPTIONS.

### Production Environment  
- Allowed origins: configured frontend URL only (e.g., `https://app.kre.example.com`).
- Strictly NO `localhost` or wildcard `*` in prod CORS.
- Allowed methods: GET, POST, OPTIONS only.

## Module Map (rev 7)
```
knowledge-retrieval-engine/
├── backend/
│   ├── src/
│   │   ├── api/
│   │   │   └── routes.py              # FastAPI endpoints (/query, /ingest, /documents)
│   │   ├── aws/
│   │   │   └── infra.py               # boto3 client factory & profile routing
│   │   ├── db/
│   │   │   ├── database.py            # DynamoDB + QdrantDB (CloudRepository)
│   │   │   └── redis_cache.py         # Redis exact & semantic caching
│   │   ├── ingestion/
│   │   │   ├── adapters/              # docx, xlsx, pptx, csv, md parsers
│   │   │   ├── concept_service.py     # Deterministic gating for OKF
│   │   │   ├── embed_service.py       # Dual-column embedder (Titan V2 + BGE Lambda/ONNX)
│   │   │   └── okf_builder.py         # Nova Micro Tier-3 extraction & DynamoDB writes
│   │   ├── ingestion_lambda/          # S3 event-driven ingestion entry point
│   │   ├── pdf_extraction_lambda/     # Containerized PDF parser entry point
│   │   ├── providers/
│   │   │   ├── bedrock_models.py      # Bedrock model config & rate limits
│   │   │   ├── embedding_provider.py  # Titan V2 embeddings
│   │   │   ├── llm_provider.py        # Nova Lite LLM generation
│   │   │   ├── provider_client.py     # Provider dispatch
│   │   │   └── reranker_provider.py   # NVIDIA NIM reranker with fallback
│   │   ├── schemas/
│   │   │   └── models.py              # Pydantic & dataclass schemas
│   │   ├── services/
│   │   │   ├── langgraph_pipeline.py  # LangGraph state graph pipeline
│   │   │   └── retrieval/             # BM25, PageIndex, Vector, OKF, Graph, Reranker, Fidelity, Compressor, Planner
│   │   ├── config.py                  # Pydantic Settings
│   │   ├── lambda.py                  # Mangum AWS Lambda handler
│   │   └── main.py                    # FastAPI app entry point
│   └── tests/                         # Unit, integration, and benchmark tests
├── bge_microservice/                  # Dedicated BGE-small Lambda & model files
│   ├── bge-onnx/                      # ONNX weights (model.onnx, tokenizer.json)
│   ├── main.py                        # Pure AWS Lambda handler
│   └── README.md
├── odl/                               # OpenDataLoader PDF Lambda SAM package
│   ├── Dockerfile
│   ├── samconfig.toml
│   └── template.yml
└── frontend/                          # React + Vite TypeScript SPA
```