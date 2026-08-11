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
| CSV | `csv` (stdlib) | builtin | — | Standard library |

**opendataloader-pdf runtime dependency:** Handled by the isolated **PDF Extraction Lambda** (`odl-parser-lambda`) deployed as a Container Image. The Ingestion Lambda is decoupled from the JVM requirement.

## 3. Retrieval — Local, Deterministic Components

| Component | Library | Version | License | Notes |
|-----------|---------|---------|---------|-------|
| BM25 | `rank-bm25` | >=0.2.2 | Apache-2.0 | Pure Python |
| Orchestration| `langgraph`, `langchain-core` | >=0.2.0, >=0.3.0 | MIT | Only core primitives |

## 4. Database and Storage

| Component | Service/Library | Version | Notes |
|-----------|-------------------|---------|-------|
| Metadata Store | AWS DynamoDB | — | Documents, chunks, OKF entities/properties |
| Vector Store | QdrantDB | latest | `kre_chunks` collection, dual named vectors |
| Cache | ElastiCache Redis | Redis 7.x | Local Redis in dev |
| Object Store | AWS S3 | — | Document files |

## 5. API Framework and Lambda Packaging

FastAPI wrapped by Mangum. Uvicorn for local dev only.

## 6. BGE-Small Microservice

| Component | Library | Version | Notes |
|-----------|---------|---------|-------|
| Runtime | `fastapi`, `uvicorn` | latest | Standalone service |
| Inference | `onnxruntime` (CPU) | latest | BGE-small-en-v1.5 ONNX weights |
| Tokenizer | `tokenizers` | latest | HuggingFace tokenizers |
| Model | `model.onnx` | — | User-provided, ~130MB |

## 7. Frontend

Vite + React (Latest), Tailwind CSS, `react-pdf`.

## 8. Explicitly Forbidden Dependencies

```text
torch
tensorflow
onnxruntime-gpu
transformers
sentence-transformers
faiss
```
**Exception for `onnxruntime`**: `onnxruntime` (CPU) is EXPLICITLY ALLOWED in the BGE-small microservice only. All Lambda packages remain free of ML frameworks.

## 9. Model Provider Accounts (required before Phase 2)

### AWS Bedrock (all environments)
- IAM permissions required: `bedrock:InvokeModel` and `lambda:InvokeFunction` (for `odl-parser-lambda`).
- Models: Titan Embed V2, Nova Lite, Nova Micro.

### NVIDIA NIM
- API key required for `nvidia/llama-nemotron-rerank-1b-v2` reranker.

### AWS Infrastructure
- DynamoDB tables, QdrantDB (cloud or local), ElastiCache Redis (or local), S3 bucket.

## 10. Development Environment Setup Order

1. Start local Redis (or connect to ElastiCache dev).
2. Start local QdrantDB (`docker run -p 6333:6333 qdrant/qdrant`).
3. Configure **real** AWS credentials for DynamoDB and Bedrock.
4. Configure **real** NVIDIA API key for NIM reranker.
5. Start the BGE-small microservice locally (`cd bge_microservice && uvicorn main:app --port 8001`).
6. Confirm `odl-parser-lambda` is either deployed (prod) or running locally via `odl/main.py` (dev).
7. Install Python dependencies.
8. Run `test_r27_no_forbidden_dependencies`.

## 11. Version Pinning Policy
Use exact pins `==` after Phase 1.