"""Centralized AWS and environment configuration.

Handles routing between local (LocalStack/floci) and real AWS endpoints.
As requested, Bedrock and Lambda clients MUST ALWAYS use real AWS endpoints,
even if AWS_ENDPOINT_URL is set to a local LocalStack instance (e.g., localhost:4566).
"""

import os
import boto3
from botocore.config import Config


def is_local_env() -> bool:
    """Check if we are in a local environment (e.g. LocalStack)."""
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    return "localhost" in endpoint or "127.0.0.1" in endpoint


def get_boto3_client(service_name: str):
    """Get a boto3 client, enforcing real AWS for Bedrock and Lambda."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    
    # Always use real AWS for Lambda and Bedrock
    if service_name in ("lambda", "bedrock-runtime", "bedrock"):
        # We explicitly omit endpoint_url here to force real AWS
        return boto3.client(service_name, region_name=region)
    
    # For other services (like S3), respect AWS_ENDPOINT_URL if present
    endpoint_url = os.environ.get("AWS_ENDPOINT_URL")
    if endpoint_url:
        return boto3.client(service_name, region_name=region, endpoint_url=endpoint_url)
    
    return boto3.client(service_name, region_name=region)

# ==========================================
# Model Configurations (Environment Overrides)
# ==========================================

def get_llm_model(provider: str) -> str:
    if provider == "prod":
        return os.environ.get("PROD_LLM_MODEL", "amazon.nova-lite-v1:0")
    return os.environ.get("DEV_LLM_MODEL", "nvidia/nemotron-nano-9b-v2:free")

def get_embedding_model(provider: str) -> str:
    if provider == "prod":
        return os.environ.get("PROD_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
    return os.environ.get("DEV_EMBEDDING_MODEL", "nvidia/nemotron-3-embed-1b:free")

def get_reranker_model(provider: str) -> str:
    if provider == "prod":
        return os.environ.get("PROD_RERANKER_MODEL", "cohere.rerank-v3-5:0")
    return os.environ.get("DEV_RERANKER_MODEL", "nvidia/llama-nemotron-rerank-vl-1b-v2:free")

def get_concept_model(provider: str) -> str:
    if provider == "prod":
        return os.environ.get("PROD_CONCEPT_MODEL", "amazon.nova-micro-v1:0")
    return os.environ.get("DEV_CONCEPT_MODEL", "nvidia/nemotron-3-nano-30b-a3b:free")

# ==========================================
# Caching Configuration
# ==========================================
CACHE_TTL_SECONDS = 86400
CACHE_MIN_CONFIDENCE = 0.50
