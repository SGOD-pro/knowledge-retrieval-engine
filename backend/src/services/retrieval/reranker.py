import logging
import time

from providers.reranker_provider import rerank_documents
from schemas.models import Chunk

logger = logging.getLogger(__name__)


def rerank(
    query: str, candidates: list[Chunk], top_k: int = 6, provider: str | None = None
) -> list[Chunk]:
    """
    Reranks candidate chunks using the provider layer API.
    Returns the top_k chunks.
    """
    start_time = time.perf_counter()
    if not candidates:
        return []

    # Extract text for the API call
    documents = [c.text for c in candidates]

    # Call the provider
    scores = rerank_documents(query, documents, provider=provider)

    # Assign scores back to chunks immutably
    from dataclasses import replace
    scored_candidates = [
        replace(chunk, reranker_score=score)
        for chunk, score in zip(candidates, scores)
    ]
    scored_candidates.sort(key=lambda c: (getattr(c, "reranker_score", 0.0) or 0.0), reverse=True)
    candidates = scored_candidates

    # Filter by threshold
    from config import settings

    pre_count = len(candidates)
    filtered = [
        c
        for c in candidates
        if getattr(c, "reranker_score", 0.0) >= settings.RERANKER_THRESHOLD
    ]
    post_count = len(filtered)
    logger.debug(
        "reranker.filter_stats threshold=%.2f pre=%d post=%d",
        settings.RERANKER_THRESHOLD,
        pre_count,
        post_count,
    )

    # Take top_k (fall back to candidates if all were filtered out by threshold)
    top_chunks = filtered[:top_k] if filtered else candidates[:top_k]

    # Log latency and confidence score for this stage
    avg_score = (
        sum(getattr(c, "reranker_score", 0.0) for c in top_chunks) / len(top_chunks)
        if top_chunks
        else 0.0
    )
    latency_ms = (time.perf_counter() - start_time) * 1000.0
    logger.info(
        "reranker.latency_ms=%.2f reranker.confidence_score=%.2f", latency_ms, avg_score
    )

    return top_chunks
