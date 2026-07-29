## ARCHITECTURE.md (rev 5) — API-First, Three-Lambda Architecture

## Deployment Model Change (breaking change from rev 4)

NEW: Three-Lambda distributed architecture.
1. **PDF EXTRACTION LAMBDA (`odl-parser-lambda`)**: Container image, ECR-hosted (10GB limit). Bundles JRE + `opendataloader-pdf`. Invoked synchronously via boto3.
2. **INGESTION LAMBDA**: Zip deployment (<250MB unzipped) pending build-time size check for adapter dependencies. Orchestration, format adapters, OKF extraction.
3. **QUERY LAMBDA**: Zip deployment default (<250MB unzipped), container fallback. Bundles `BGE-small-en-v1.5` ONNX weights (~130MB) for local fast-path embedding.

## Hard Constraints (updated)
- Maximum ONE LLM call per query (unchanged).
- Fast path: p95 < 400ms. Local BGE-small embedding eliminates network latency for this stage.
- Full pipeline: p95 < 4000ms.
- Every module logs latency_ms and confidence_score.
- LLM receives only compressed context. Never raw chunks.
- Max tokens to LLM: 1200.
- Graph: MAX_NODES=40 absolute. MAX_HOPS=2 default, 3 on deep_causal_flag.
- Deployment limits: **<250MB unzipped** for zip deployments, **10GB** for container images. (50MB is only the direct upload limit, not the functional ceiling).
- Local model weights allowed **ONLY** for `BGE-small-en-v1.5` ONNX in the query fast path. All other models MUST use external APIs.

## Model Provider Matrix

| Function          | Dev/Staging (floci + AWS)              | Production (real AWS)          |
|-------------------|----------------------------------------|----------------------------------|
| Fast Path Embed   | Local BGE-small-en-v1.5 (ONNX)         | Local BGE-small-en-v1.5 (ONNX) |
| Full Path Embed   | nvidia/nemotron-3-embed-1b (OpenRouter)| amazon.titan-embed-text-v2 (Bedrock) |
| Reranker          | nvidia/llama-nemotron-rerank-vl-1b-v2 (OpenRouter) | cohere.rerank-v3-5 (Bedrock) |
| OKF extraction    | nvidia/nemotron-3-nano-30b-a3b (OpenRouter) | amazon.nova-micro-v1 (Bedrock) |
| Query LLM         | openai/gpt-oss-20b or nemotron-nano-9b | amazon.nova-lite-v1 or claude-haiku |
| CI judge          | —                                      | amazon.nova-lite-v1 (Bedrock) |

## Vector Store — RDS PostgreSQL + pgvector

- Chunks table gets an `embedding vector(1024)` column for API embeddings, and a separate one (or unified schema) for BGE-small (384-dim) for fast path.
- HNSW index on embedding column.
- Dev environment uses `floci` to emulate RDS PostgreSQL and ElastiCache. Prod uses real RDS PostgreSQL. No local Postgres-only fallback.

## Module Map (rev 5)

kre/
├── pdf_extraction_lambda/    # Deployed separately. Container image (JRE + opendataloader-pdf).
├── ingestion_lambda/         # Zip deployment (pending size check).
│   ├── format_router.py
│   ├── adapters/ (docx/xlsx/pptx in-process)
│   ├── pdf_adapter.py        # Invokes odl-parser-lambda via boto3 (sync, S3 ref in, response contract unconfirmed)
│   ├── parse_service.py
│   ├── page_index_service.py
│   ├── concept_service.py
│   ├── normalize_service.py
│   ├── okf_builder.py
│   └── embed_service.py      # Uses API provider for ingestion embeddings.
├── query_lambda/             # Zip deployment default.
│   ├── planner.py
│   ├── decomposer.py
│   ├── bm25_retriever.py
│   ├── page_index_retriever.py
│   ├── vector_retriever.py   # Routes to local BGE-small (fast) or API (full).
│   ├── okf_retriever.py
│   ├── graph_retriever.py
│   ├── reranker.py
│   ├── fidelity_check.py
│   ├── compressor.py
│   ├── embed_service.py      # Two paths: local BGE-small ONNX vs API.
│   └── api/main.py
└── shared/
    ├── providers/
    │   ├── provider_client.py
    │   ├── embedding_provider.py
    │   ├── reranker_provider.py
    │   └── llm_provider.py
    └── db/
        ├── postgres.py
        └── redis_cache.py