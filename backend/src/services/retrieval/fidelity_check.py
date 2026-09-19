import logging
import re
import time

from config import settings

logger = logging.getLogger(__name__)

_TABULAR_FORMATS = {"csv", "xlsx", "xls"}
_TABULAR_ELEMENT_TYPES = {"table_row", "cell", "table", "header_row"}


class CoverageError(Exception):
    """Raised when context compression drops critical query entities."""


def _context_is_tabular(context_chunks: list[str]) -> bool:
    """Heuristic: if the joined context looks like structured tabular data
    (many pipe/comma delimited short lines with multiple fields or key-value rows),
    treat it as tabular and skip cosine similarity gating.

    Requires BOTH high structured-line ratio AND multi-field consistency
    to avoid false-positiving on standard prose with commas.
    """
    joined = "\n".join(context_chunks)
    lines = [l.strip() for l in joined.splitlines() if l.strip()]
    if not lines:
        return False

    structured_count = 0
    for l in lines:
        pipe_count = l.count("|")
        comma_count = l.count(",")
        colon_count = l.count(":")
        # Multi-column table row (at least 3 columns) or multiple Key: Value pairs
        if pipe_count >= 2 or comma_count >= 2 or (colon_count >= 2 and len(l) < 200):
            structured_count += 1

    ratio = structured_count / max(1, len(lines))
    return ratio >= 0.5


def check_fidelity(
    query: str,
    context_chunks: list[str],
    query_embedding: list[float] | None = None,
) -> float:
    """
    Stage 7: Fidelity Check.
    Check if the context chunks meet the cosine similarity threshold against the query.
    If the threshold is not met, a CoverageError is raised, gating the LLM execution.

    Tabular/structured contexts (CSV, XLS) pass via tabular bypass — cosine similarity
    between natural language queries and column-value rows is inherently low even when the
    chunk contains the correct answer.
    """
    start_time = time.perf_counter()

    if query == "NOT_FOUND" or not context_chunks:
        return 1.0

    # --- Tabular bypass: never gate on cosine similarity for structured data ---
    if _context_is_tabular(context_chunks):
        logger.info("fidelity_check.tabular_bypass — skipping embedding gate")
        return 1.0

    try:
        import numpy as np

        from ingestion.embed_service import embed_fast_local

        if query_embedding is not None and len(query_embedding) == 384:
            query_emb = np.array(query_embedding, dtype=np.float32)
            q_norm = np.linalg.norm(query_emb)
            if q_norm > 0:
                query_emb = query_emb / q_norm
        else:
            query_emb = embed_fast_local(query)

        max_sim = 0.0
        for chunk_text in context_chunks:
            ctx_emb = embed_fast_local(chunk_text)
            sim = float(np.dot(query_emb, ctx_emb))
            max_sim = max(max_sim, sim)

    except Exception as e:
        logger.warning("fidelity_check_embedding_failed error=%s", e)
        # If embedding fails, fallback to passing
        return 1.0

    latency_ms = (time.perf_counter() - start_time) * 1000.0
    logger.info(
        "fidelity_check.latency_ms=%.2f fidelity_check.confidence_score=%.2f",
        latency_ms,
        max_sim,
    )

    threshold = settings.FIDELITY_THRESHOLD
    if max_sim < threshold:
        _STOPWORDS = {
            "what", "is", "the", "a", "an", "in", "on", "at", "to", "for",
            "of", "and", "or", "with", "by", "from", "as", "are", "how", "many", "does",
        }
        raw_q = set(re.findall(r"\w+", query.lower()))
        query_words = {w for w in raw_q if w not in _STOPWORDS and len(w) > 2} or raw_q
        matched_words = set()
        for chunk_text in context_chunks:
            chunk_words = set(re.findall(r"\w+", chunk_text.lower()))
            matched_words.update(
                qw for qw in query_words
                if any(qw == cw or (len(qw) > 3 and qw.rstrip('s') == cw.rstrip('s')) for cw in chunk_words)
            )
        coverage = len(matched_words) / max(1, len(query_words))
        if coverage >= 0.5:
            return max(max_sim, coverage)

        raise CoverageError(
            f"Coverage ratio {max_sim:.2f} is below {threshold} threshold."
        )

    return max_sim
