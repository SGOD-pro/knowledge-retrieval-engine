# PROJECT_REQUIREMENTS.md — Dependencies, Versions, and Provider Accounts

This file is the single source of truth for what gets installed.

## 1. Runtime

```text
Python: 3.12.x
Node.js: 20.x LTS
```

## 2. Document Parsing

| Format | Library | Version | License | Notes |
|--------|---------|---------|---------|-------|
| PDF | `opendataloader-pdf` | latest | Apache-2.0 | Java-based (JVM per call) |
| DOCX | `python-docx` | >=1.1.2 | MIT | Reads Heading 1/2/3 styles |
| XLSX | `openpyxl` | >=3.1.5 | MIT | `data_only=True` |
| PPTX | `python-pptx` | >=1.0.2 | MIT | Reads speaker notes |

**opendataloader-pdf runtime dependency:** Handled by the isolated **PDF Extraction Lambda** (`odl-parser-lambda`) deployed as a Container Image. The Ingestion Lambda is decoupled from the JVM requirement.

## 3. Retrieval — Local, Deterministic Components

| Component | Library | Version | License | Notes |
|-----------|---------|---------|---------|-------|
| BM25 | `rank-bm25` | >=0.2.2 | Apache-2.0 | Pure Python |
| Orchestration| `langgraph`, `langchain-core` | >=0.2.0, >=0.3.0 | MIT | Only core primitives |

## 4. Database and Storage

| Component | Service/Library | Version | Notes |
|-----------|-------------------|---------|-------|
| Store | RDS PostgreSQL | 16.x | `floci` emulation in dev |
| Vector ext| `pgvector` | >=0.7.0 | `CREATE EXTENSION vector;` |
| Cache | ElastiCache Redis| Redis 7.x | `floci` emulation in dev |

## 5. API Framework and Lambda Packaging

FastAPI wrapped by Mangum. Uvicorn for local dev only.

## 6. Frontend

Next.js 14.x (Deprecated) -> Vite React (Latest), Tailwind CSS, `react-pdf`.

## 7. Explicitly Forbidden Dependencies

```text
torch
tensorflow
onnxruntime-gpu
transformers
sentence-transformers
faiss
```
**Exception for `onnxruntime`**: `onnxruntime` (CPU) and ONNX-exported weights for BGE-small are EXPLICITLY ALLOWED strictly for the Query Lambda's fast path. Full transformers/torch remain forbidden.

## 8. Model Provider Accounts (required before Phase 2)

### OpenRouter (dev/staging)
- Account: openrouter.ai, API key. Used for dev-tier free models.

### AWS Bedrock (prod)
- Dev environment requires Bedrock access setup too.
- IAM permissions required: `bedrock:InvokeModel` and `lambda:InvokeFunction` (for `odl-parser-lambda`).

### AWS Infrastructure (dev/prod)
- RDS PostgreSQL, ElastiCache Redis, S3 bucket.

## 9. Development Environment Setup Order

The Dev environment uses a strict three-way split. Do not use local emulation for AWS services beyond RDS/Redis.

1. Start `floci` to emulate RDS PostgreSQL and ElastiCache Redis.
2. Run `CREATE EXTENSION vector;` on the `floci` PostgreSQL instance and apply schemas.
3. Configure **real** OpenRouter API keys in your environment for dev LLMs.
4. Configure **real** AWS Bedrock credentials in your environment.
5. Confirm `odl-parser-lambda` is deployed and reachable via boto3 with dev AWS credentials (requires `lambda:InvokeFunction`).
6. Document concrete timeout values: Ingestion Lambda timeout > `odl-parser-lambda` timeout (currently UNKNOWN and pending verification against the live function).
7. Install Python dependencies.
8. Run `test_r27_no_forbidden_dependencies`.

## 10. Version Pinning Policy
Use exact pins `==` after Phase 1.