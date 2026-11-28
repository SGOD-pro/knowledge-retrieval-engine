import os

from config import settings


def get_llm_model() -> str:
    return settings.PROD_LLM_MODEL


def get_embedding_model() -> str:
    return settings.PROD_EMBEDDING_MODEL


def is_bedrock_reranker_enabled() -> bool:
    """Return True when the Bedrock reranker is active via env or settings."""
    explicit = os.environ.get("BEDROCK_RERANKER_ENABLED")
    if explicit is not None:
        return explicit.lower() in ("true", "1")
    provider = os.environ.get("RERANKER_PROVIDER") or getattr(settings, "RERANKER_PROVIDER", "bedrock")
    if provider in ("lexical", "bm25", "bm25plus"):
        return False
    return bool(getattr(settings, "BEDROCK_RERANKER_ENABLED", True))


def get_reranker_model() -> str:
    """Return the active configured reranker model name."""
    if is_bedrock_reranker_enabled():
        return getattr(settings, "BEDROCK_RERANKER_MODEL", "amazon.rerank-v1:0")
    return "bm25plus"


def get_concept_model() -> str:
    return settings.PROD_CONCEPT_MODEL


def get_extraction_model(explicit_model_id: str | None = None) -> str:
    """Return the shared Bedrock extraction model ID for OKF, Graph, and LAYA."""
    if explicit_model_id:
        return explicit_model_id
    return getattr(settings, "PROD_CONCEPT_MODEL", "apac.amazon.nova-micro-v1:0")
