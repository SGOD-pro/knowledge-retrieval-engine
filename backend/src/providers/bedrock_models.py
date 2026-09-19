from config import settings


def get_llm_model() -> str:
    return settings.PROD_LLM_MODEL


def get_embedding_model() -> str:
    return settings.PROD_EMBEDDING_MODEL


def get_reranker_model() -> str:
    # Reranker is OpenRouter-only (all environments). PROD_RERANKER_MODEL is removed.
    return settings.OPENROUTER_RERANKER_MODEL


def get_concept_model() -> str:
    return settings.PROD_CONCEPT_MODEL
