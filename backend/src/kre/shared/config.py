"""Centralized AWS and environment configuration.

Dev:  uses AWS_PROFILE for the default session; Bedrock always uses
      profile 'aws' + ap-south-1 (Mumbai) regardless of the default profile.
Prod: ambient IAM creds (instance role / env vars) — no profile needed.
Local services (S3, SQS, …) route through AWS_ENDPOINT_URL when set (floci).
"""

import os

import boto3

_ENV = os.environ.get("ENVIRONMENT", "dev")
_REGION = os.environ.get("AWS_REGION", "us-east-1")
_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL") or None  # None disables override

# Services that may be emulated locally via floci/LocalStack
_LOCAL_SERVICES = {"dynamodb", "s3", "sqs", "sns", "lambda"}


def _default_session() -> boto3.Session:
    """Profile-based session in dev; ambient-creds session in prod."""
    if _ENV != "prod":
        return boto3.Session(
            profile_name=os.environ.get("AWS_PROFILE", "default"),
            region_name=_REGION,
        )
    return boto3.Session(region_name=_REGION)


_session = _default_session()


def get_boto3_client(service_name: str):
    """Return a boto3 client for service_name.

    Bedrock always hits real AWS (profile 'aws', ap-south-1 in dev;
    ambient creds in prod).  All other local-emulated services get the
    floci endpoint when AWS_ENDPOINT_URL is set.
    """
    if service_name == "bedrock-runtime":
        if _ENV != "prod":
            return boto3.Session(
                profile_name="aws",
                region_name="ap-south-1",
            ).client("bedrock-runtime")
        # Prod: let the instance role / env creds supply everything
        return boto3.Session(region_name=_REGION).client("bedrock-runtime")

    kwargs = {}
    if _ENDPOINT_URL and service_name in _LOCAL_SERVICES:
        kwargs["endpoint_url"] = _ENDPOINT_URL

    return _session.client(service_name, **kwargs)

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
