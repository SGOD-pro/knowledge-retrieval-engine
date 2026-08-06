from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    ENVIRONMENT: str = "dev"
    
    # AWS Resources
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/kre"
    REDIS_URL: str = "redis://localhost:6379/0"
    S3_BUCKET_NAME: str = "kre-documents-prod"
    ODL_PARSER_LAMBDA_NAME: str = "odl-parser-lambda"
    DYNAMODB_TABLE_NAME: str = "kre-table"

    # PROD Models (Bedrock)
    PROD_LLM_MODEL: str = "apac.amazon.nova-lite-v1:0"
    PROD_EMBEDDING_MODEL: str = "amazon.titan-embed-text-v2:0"
    PROD_RERANKER_MODEL: str = "cohere.rerank-v3-5:0"
    PROD_CONCEPT_MODEL: str = "apac.amazon.nova-micro-v1:0"

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()

# ==========================================
# Caching Configuration
# ==========================================
CACHE_TTL_SECONDS = 86400
CACHE_MIN_CONFIDENCE = 0.50
