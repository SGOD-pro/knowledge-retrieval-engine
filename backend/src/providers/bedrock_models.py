import os

from config import settings


def get_llm_model() -> str:
    return settings.PROD_LLM_MODEL


def get_embedding_model() -> str:
    return settings.PROD_EMBEDDING_MODEL


def is_bedrock_reranker_enabled() -> bool:
    """Return True when the opt-in Bedrock reranker is active via env or settings."""
    return (
        os.environ.get("BEDROCK_RERANKER_ENABLED", "").lower() in ("true", "1")
        or bool(getattr(settings, "BEDROCK_RERANKER_ENABLED", False))
        or os.environ.get("RERANKER_PROVIDER") == "bedrock"
        or getattr(settings, "RERANKER_PROVIDER", "") == "bedrock"
    )


def get_reranker_model() -> str:
    """Return the active configured reranker model name."""
    if is_bedrock_reranker_enabled():
        return getattr(settings, "BEDROCK_RERANKER_MODEL", "amazon.rerank-v1:0")
    provider = os.environ.get("RERANKER_PROVIDER") or getattr(settings, "RERANKER_PROVIDER", "")
    if provider in ("lexical", "bm25", "bm25plus"):
        return "bm25plus"
    return settings.OPENROUTER_RERANKER_MODEL


def get_concept_model() -> str:
    return settings.PROD_CONCEPT_MODEL
