from typing import Literal

from pydantic_settings import BaseSettings


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

    # NVIDIA NIM Reranker — always prod
    NVIDIA_RERANKER_MODEL: str = "nvidia/llama-nemotron-rerank-1b-v2"
    NVIDIA_API_KEY: str = ""

    # MODEL_PROVIDER: controls which Bedrock profile is used at query time
    MODEL_PROVIDER: str = "prod"

    # FIDELITY_THRESHOLD: minimum entity coverage ratio to pass the fidelity gate.
    # 1.0 = require 100% entity match (too strict for real-world RAG).
    # 0.5 = require at least 50% entity match (recommended baseline).
    FIDELITY_THRESHOLD: float = 0.5
    # Rejection Thresholds
    BM25_THRESHOLD: float = 0.1
    PAGEINDEX_THRESHOLD: float = 0.1
    VECTOR_THRESHOLD: float = 0.3
    RERANKER_THRESHOLD: float = 0.2

    model_config = {"env_file": (".env", "../.env"), "extra": "ignore"}


settings = Settings()

# ==========================================
# Caching Configuration
# ==========================================
CACHE_TTL_SECONDS = 86400
CACHE_MIN_CONFIDENCE = 0.50
