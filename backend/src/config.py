from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings

_SRC_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SRC_DIR.parent
_ROOT_DIR = _BACKEND_DIR.parent
_ENV_FILES = (
    str(_BACKEND_DIR / ".env"),
    str(_ROOT_DIR / ".env"),
    ".env",
    "../.env",
)


class Settings(BaseSettings):
    ENVIRONMENT: Literal["dev", "prod", "test"] = "dev"

    # AWS Region
    AWS_REGION: str = "us-east-1"

    # AWS Resources — Local services (DynamoDB, S3, SQS) route dev→LocalStack
    DYNAMODB_TABLE_NAME: str = "kre-table"
    S3_BUCKET_NAME: str = "kre-documents-prod"
    ODL_PARSER_LAMBDA_NAME: str = "odl-parser-lambda-prod"
    REDIS_URL: str = "redis://localhost:6379/0"

    # BGE Embedding Lambda function
    BGE_EMBEDDING_LAMBDA_NAME: str = "bge-microservice-stack-BGELambdaFunction-roIuowXCDxCe"

    # Qdrant Cloud — always prod, no LocalStack equivalent
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: str | None = None

    # PROD Models (Bedrock) — always prod
    PROD_LLM_MODEL: str = "apac.amazon.nova-lite-v1:0"
    PROD_EMBEDDING_MODEL: str = "amazon.titan-embed-text-v2:0"
    PROD_CONCEPT_MODEL: str = "apac.amazon.nova-micro-v1:0"

    # NVIDIA NIM Reranker — DEPRECATED (410 Gone). Kept for legacy config compat.
    NVIDIA_RERANKER_MODEL: str = "nvidia/llama-nemotron-rerank-1b-v2"
    NVIDIA_API_KEY: str = ""

    # OpenRouter Reranker
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_RERANKER_MODEL: str = "nvidia/llama-nemotron-rerank-vl-1b-v2:free"

    # Primary Reranker model
    PROD_RERANKER_MODEL: str = "nvidia/llama-nemotron-rerank-vl-1b-v2:free"

    # MODEL_PROVIDER: controls which Bedrock profile is used at query time
    MODEL_PROVIDER: str = "prod"

    # FIDELITY_THRESHOLD: minimum entity coverage ratio to pass the fidelity gate.
    # 1.0 = require 100% entity match (too strict for real-world RAG).
    # 0.5 = require at least 50% entity match (recommended baseline).
    # FIDELITY_THRESHOLD: lower = more permissive. 0.35 reduces false-refusals
    # on tabular/structured data that has low cosine similarity to natural queries.
    FIDELITY_THRESHOLD: float = 0.35
    # Rejection Thresholds
    BM25_THRESHOLD: float = 0.1
    PAGEINDEX_THRESHOLD: float = 0.1
    VECTOR_THRESHOLD: float = 0.3
    # Lower reranker threshold: don't drop all chunks just because Jaccard scores are low
    RERANKER_THRESHOLD: float = 0.05

    model_config = {"env_file": _ENV_FILES, "extra": "ignore"}


settings = Settings()

# ==========================================
# Caching Configuration
# ==========================================
CACHE_TTL_SECONDS = 86400
CACHE_MIN_CONFIDENCE = 0.50
