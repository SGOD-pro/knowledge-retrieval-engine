import logging
import time
from typing import Any

from providers.reranker_provider import (
    get_bypassed_reranker_telemetry,
    get_last_reranker_mode,
    get_last_reranker_telemetry,
    rerank_documents,
)
from schemas.models import Chunk, RerankerMode

logger = logging.getLogger(__name__)


def rerank(
    query: str,
    candidates: list[Chunk],
    top_k: int = 6,
    provider: str | None = None,
    return_mode: bool = False,
    financial_binding: Any | None = None,
    return_telemetry: bool = False,
) -> list[Chunk] | tuple[list[Chunk], str] | tuple[list[Chunk], str, dict[str, Any]]:
    """
    Reranks candidate chunks using the provider layer API.
    Returns top_k chunks, optionally with reranker mode and explicit telemetry.
    """
    start_time = time.perf_counter()
    if not candidates:
        telemetry = get_bypassed_reranker_telemetry(provider=provider, candidate_count=0)
        if return_mode and return_telemetry:
            return ([], RerankerMode.BYPASSED.value, telemetry)
        if return_mode:
            return ([], RerankerMode.BYPASSED.value)
        if return_telemetry:
            return ([], telemetry)
        return []

    if financial_binding is None:
        try:
            from services.retrieval.table_metric_binding import bind_financial_query
            financial_binding = bind_financial_query(query)
        except Exception:
            financial_binding = None

    # Extract text for the API call
    documents = [c.text for c in candidates]

    # Call the provider and carry telemetry explicitly across thread boundaries
    try:
        raw_res = rerank_documents(query, documents, provider=provider, return_telemetry=True)
    except TypeError:
        raw_res = rerank_documents(query, documents, provider=provider)

    if isinstance(raw_res, tuple) and len(raw_res) == 2 and isinstance(raw_res[1], dict):
        scores, provider_telemetry = raw_res
        telemetry = dict(provider_telemetry)
    else:
        scores = raw_res
        telemetry = dict(get_last_reranker_telemetry() or {})

    # Assign scores back to chunks immutably
    from dataclasses import replace
    from services.retrieval.table_metric_binding import score_table_chunk_metric_affinity

    scored_candidates = []
    for chunk, score in zip(candidates, scores):
        aff = 0.0
        if financial_binding and getattr(financial_binding, "is_active", False):
            aff = score_table_chunk_metric_affinity(getattr(chunk, "text", ""), financial_binding)
        scored_chunk = replace(chunk, reranker_score=score)
        scored_candidates.append((scored_chunk, aff, score or 0.0))

    # Sort candidates by (affinity, base_score) descending
    # Reuses the existing ranking design and preserves relevance ordering where affinity is equal.
    scored_candidates.sort(key=lambda item: (item[1], item[2]), reverse=True)
    candidates = [item[0] for item in scored_candidates]

    # Filter by threshold with calibration for low-probability scoring models
    from config import settings

    pre_count = len(candidates)
    max_score = max((item[2] for item in scored_candidates), default=0.0)
    # Calibrate threshold: if scores are on a low absolute probability scale,
    # use relative cutoff (half of max_score) rather than hardcoded threshold.
    effective_threshold = (
        min(settings.RERANKER_THRESHOLD, max_score * 0.5)
        if max_score > 0
        else settings.RERANKER_THRESHOLD
    )
    filtered = [
        item[0]
        for item in scored_candidates
        if item[2] >= effective_threshold or item[1] > 0
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

    if top_k > 2:
        # Only enforce document diversity across documents with competitive relevance scores
        doc_max_scores: dict[str, float] = {}
        for c in candidates_to_use:
            d_id = str(getattr(c, "document_id", ""))
            s = getattr(c, "reranker_score", 0.0) or 0.0
            doc_max_scores[d_id] = max(doc_max_scores.get(d_id, 0.0), s)
        competing_docs = {
            d for d, s in doc_max_scores.items() if s >= max_score * 0.5
        }
        max_per_doc = max(2, top_k - 2) if len(competing_docs) > 1 else top_k
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

    mode = telemetry.get("effective_mode") or get_last_reranker_mode()
    if not telemetry:
        telemetry = get_bypassed_reranker_telemetry(
            provider=provider,
            mode=mode,
            candidate_count=len(documents),
        )
    telemetry["candidate_count"] = telemetry.get("candidate_count") or len(documents)
    if "latency_ms" not in telemetry or telemetry.get("latency_ms", 0.0) == 0.0:
        telemetry["latency_ms"] = round(latency_ms, 2)

    if return_mode and return_telemetry:
        return (top_chunks, mode, telemetry)
    if return_mode:
        return (top_chunks, mode)
    if return_telemetry:
        return (top_chunks, telemetry)
    return top_chunks
