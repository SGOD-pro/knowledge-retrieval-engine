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

    # Filter by threshold with calibration for low-probability scoring models
    from config import settings

    pre_count = len(candidates)
    max_score = (
        max((getattr(c, "reranker_score", 0.0) or 0.0) for c in candidates)
        if candidates
        else 0.0
    )
    # Calibrate threshold: if scores are on a low absolute probability scale,
    # use relative cutoff (half of max_score) rather than hardcoded threshold.
    effective_threshold = (
        min(settings.RERANKER_THRESHOLD, max_score * 0.5)
        if max_score > 0
        else settings.RERANKER_THRESHOLD
    )
    filtered = [
        c
        for c in candidates
        if (getattr(c, "reranker_score", 0.0) or 0.0) >= effective_threshold
    ]
    post_count = len(filtered)
    logger.debug(
        "reranker.filter_stats threshold=%.2f effective=%.4f pre=%d post=%d",
        settings.RERANKER_THRESHOLD,
        effective_threshold,
        pre_count,
        post_count,
    )

    # Take top_k with document diversity and page diversity (fall back to candidates if all filtered out)
    candidates_to_use = filtered if filtered else candidates
    unique_docs = {str(getattr(c, "document_id", "")) for c in candidates_to_use}

    if top_k > 2:
        max_per_doc = max(2, top_k - 2) if len(unique_docs) > 1 else top_k
        max_per_page = 2
        doc_counts: dict[str, int] = {}
        page_counts: dict[tuple[str, int | str], int] = {}
        top_chunks = []
        deferred = []
        for c in candidates_to_use:
            d_id = str(getattr(c, "document_id", ""))
            p_num = getattr(c, "page_number", None)
            if p_num is None and getattr(c, "metadata", None):
                p_num = c.metadata.get("page")

            d_cnt = doc_counts.get(d_id, 0)
            p_key = (d_id, p_num) if p_num is not None else None
            p_cnt = page_counts.get(p_key, 0) if p_key is not None else 0

            if d_cnt < max_per_doc and (p_key is None or p_cnt < max_per_page):
                top_chunks.append(c)
                doc_counts[d_id] = d_cnt + 1
                if p_key is not None:
                    page_counts[p_key] = p_cnt + 1
                if len(top_chunks) == top_k:
                    break
            else:
                deferred.append(c)
        if len(top_chunks) < top_k:
            for c in deferred:
                top_chunks.append(c)
                if len(top_chunks) == top_k:
                    break
    else:
        top_chunks = candidates_to_use[:top_k]

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
