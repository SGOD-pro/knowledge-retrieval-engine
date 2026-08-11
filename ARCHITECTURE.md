# ARCHITECTURE.md (rev 6) — API-First, Three-Lambda + Microservice Architecture

## Deployment Model (rev 6 — breaking change from rev 5)

Four deployment units:
1. **PDF EXTRACTION LAMBDA (`odl-parser-lambda`)**: Container image, ECR-hosted (10GB limit). Bundles JRE + `opendataloader-pdf`. Invoked synchronously via boto3. Can run locally for dev.
2. **INGESTION LAMBDA**: Zip deployment (<250MB unzipped). Orchestration, format adapters, OKF extraction via Nova Micro.
3. **QUERY LAMBDA**: Zip deployment (<250MB unzipped). Core retrieval pipeline. Does NOT bundle BGE-small weights — calls the BGE-small microservice instead.
4. **BGE-SMALL MICROSERVICE** (`bge_microservice/`): Standalone FastAPI service running BGE-small-en-v1.5 ONNX model (`model.onnx`). Separated from the Query Lambda to keep Lambda package under 250MB and allow independent scaling. Can run locally or as a Lambda.

## Hard Constraints (updated)
- Maximum ONE LLM call per query (unchanged).
- Fast path: p95 < 400ms. BGE-small microservice eliminates Bedrock network latency for this stage.
- Full pipeline: p95 < 4000ms.
- Every module logs latency_ms and confidence_score.
- LLM receives only compressed context. Never raw chunks.
- Max tokens to LLM: 1200.
- Graph: MAX_NODES=40 absolute. MAX_HOPS=2 default, 3 on deep_causal_flag.
- Deployment limits: **<250MB unzipped** for zip deployments, **10GB** for container images.
- No local model weights in the Query Lambda package. BGE-small runs as a separate microservice.

## Model Provider Matrix

| Function          | Dev/Staging                             | Production (real AWS)          |
|-------------------|-----------------------------------------|----------------------------------|
| Fast Path Embed   | BGE-small-en-v1.5 (ONNX Microservice)  | BGE-small-en-v1.5 (ONNX Microservice) |
| Full Path Embed   | amazon.titan-embed-text-v2 (Bedrock)    | amazon.titan-embed-text-v2 (Bedrock) |
| Reranker          | nvidia/llama-nemotron-rerank-1b-v2 (NVIDIA NIM) | nvidia/llama-nemotron-rerank-1b-v2 (NVIDIA NIM) |
| OKF extraction    | amazon.nova-micro-v1 (Bedrock)          | amazon.nova-micro-v1 (Bedrock) |
| Query LLM         | amazon.nova-lite-v1 (Bedrock)           | amazon.nova-lite-v1 (Bedrock) |

## Storage — DynamoDB + QdrantDB (No PostgreSQL)

### DynamoDB (Single Table Design)
- **Documents & Chunks:** `kre-table` with PK/SK pattern (`DOC#<id>` / `CHUNK#<id>`).
- **OKF Entities:** `okf_entities` table — stores concepts extracted by Nova Micro during ingestion.
- **OKF Properties:** `okf_properties` table — stores typed properties linked to concepts.
- **OKF Relations:** Stored in `okf_entities` table as adjacency entries for graph traversal.

### QdrantDB (Vector Search)
- Collection `kre_chunks` with two named vectors, never merged:
  - `embedding_fast` (384-dim) for BGE-small microservice embeddings.
  - `embedding_full` (1024-dim) for Titan V2 API embeddings.
- Both vectors populated at ingestion time for every chunk.
- Query-time routing rule (Rule 19): fast-path queries embed via BGE-small microservice and search ONLY `embedding_fast`. Full-path queries embed via Titan API and search ONLY `embedding_full`. No query ever compares against both vectors.

### Redis (ElastiCache)
- Exact-match query cache with 24h TTL.
- Write-guard: only cache responses with confidence >= 0.50 and answer != "NOT_FOUND".

## CORS & Security

### Dev Environment
- Allowed origins: `http://localhost:5173` (Vite dev server).
- Allowed methods: GET, POST, OPTIONS (only what's used).
- No wildcard `*` origins.

### Production Environment  
- Allowed origins: configured frontend URL only (e.g., `https://app.kre.example.com`).
- Strictly NO `localhost` in prod CORS.
- Allowed methods: GET, POST, OPTIONS only. All others blocked.

## PDF Extraction Invocation Contract (Dev vs. Prod)
- **Dev Environment:** The Ingestion Lambda dynamically imports the `odl/main.py` handler from the repository root and passes `{"local_file_path": "..."}` in the event payload. This allows local PDF parsing without uploading to S3.
- **Prod Environment:** The Ingestion Lambda uses `boto3.client('lambda').invoke(FunctionName='odl-parser-lambda', ...)` and passes `{"s3_bucket": "...", "s3_key": "..."}`.

## AWS Interaction Minimization

- **Smoke Tests First:** Before running any test suite that invokes Bedrock models, run a lightweight connectivity check (embed one word, call one rerank). If that fails, skip the Bedrock-dependent tests instead of burning tokens on doomed runs.
- **Local-First Dev:** `odl-parser-lambda` and `bge_microservice` can both run locally without AWS. Only Bedrock calls (Titan, Nova, NVIDIA NIM) require real cloud credentials.
- **Infrastructure Validation:** `aws/infra.py` checks for required DynamoDB tables and S3 buckets on startup and creates them if missing, minimizing manual AWS console interactions.

## Module Map (rev 6)
```
kre/
├── bge_microservice/           # Standalone FastAPI + ONNX. Separate deployment.
│   ├── main.py                 # FastAPI app with /embed endpoint
│   └── model.onnx              # BGE-small-en-v1.5 weights (user-provided)
├── pdf_extraction_lambda/      # Container image (JRE + opendataloader-pdf).
├── ingestion_lambda/           # Zip deployment.
│   ├── format_router.py
│   ├── adapters/ (docx/xlsx/pptx/csv in-process)
│   ├── pdf_adapter.py          # Invokes odl-parser-lambda via boto3
│   ├── parse_service.py
│   ├── page_index_service.py
│   ├── concept_service.py      # Nova Micro OKF extraction
│   ├── normalize_service.py
│   ├── okf_builder.py          # Builds OKF entities/properties/relations → DynamoDB
│   └── embed_service.py        # Titan V2 + BGE-small microservice for dual vectors
├── query_lambda/               # Zip deployment.
│   ├── planner.py
│   ├── bm25_retriever.py
│   ├── page_index_retriever.py
│   ├── vector_retriever.py     # Routes to BGE-small microservice (fast) or Titan API (full)
│   ├── okf_retriever.py        # DynamoDB lookup, zero LLM calls
│   ├── graph_retriever.py      # DynamoDB adjacency traversal
│   ├── reranker.py             # NVIDIA Nemotron via NIM API
│   ├── fidelity_check.py
│   ├── compressor.py           # Top 6 chunks, metadata stripped, 1200 token cap
│   └── api/main.py
└── shared/
    ├── providers/
    │   ├── provider_client.py
    │   ├── embedding_provider.py   # Two paths: BGE-small microservice vs Titan API
    │   ├── reranker_provider.py    # NVIDIA NIM
    │   └── llm_provider.py         # Nova Lite (query) / Nova Micro (ingestion)
    └── db/
        ├── database.py             # DynamoDB + QdrantDB (CloudRepository)
        └── redis_cache.py
```