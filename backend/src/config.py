from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    ENVIRONMENT: str = "dev"
    
    # AWS Region
    AWS_REGION: str = "us-east-1"

    # AWS Resources — Local services (DynamoDB, S3, SQS) route dev→LocalStack
    DYNAMODB_TABLE_NAME: str = "kre-table"
    S3_BUCKET_NAME: str = "kre-documents-prod"
    ODL_PARSER_LAMBDA_NAME: str = "odl-parser-lambda-prod"
    REDIS_URL: str = "redis://localhost:6379/0"

    # BGE Embedding — dev uses local ONNX, prod invokes this Lambda
    BGE_EMBEDDING_LAMBDA_NAME: str = "bge-small-en-v1-5-lambda-prod"

    # Qdrant Cloud — always prod, no LocalStack equivalent
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: Optional[str] = None

    # PROD Models (Bedrock) — always prod
    PROD_LLM_MODEL: str = "apac.amazon.nova-lite-v1:0"
    PROD_EMBEDDING_MODEL: str = "amazon.titan-embed-text-v2:0"
    PROD_CONCEPT_MODEL: str = "apac.amazon.nova-micro-v1:0"

    # NVIDIA NIM Reranker — always prod
    NVIDIA_RERANKER_MODEL: str = "nvidia/llama-nemotron-rerank-1b-v2"
    NVIDIA_API_KEY: str = ""

    # MODEL_PROVIDER: controls which Bedrock profile is used at query time
    MODEL_PROVIDER: str = "prod"

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()

# ==========================================
# Caching Configuration
# ==========================================
CACHE_TTL_SECONDS = 86400
CACHE_MIN_CONFIDENCE = 0.50