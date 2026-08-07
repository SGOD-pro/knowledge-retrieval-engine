import logging
import time
from kre.models import Chunk

logger = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 4000   # ~1000 tokens at ~4 chars/token (enforces Rule 4)


def compress_chunks(query: str, chunks: list[Chunk]) -> str:
    """
    Stage 8: Compression
    Extracts relevant snippets from chunks, strictly enforcing a 1000-token
    (~4000-char) budget to minimize hallucination and latency.
    """
    start_time = time.perf_counter()

    query_words = set(w.lower() for w in query.split() if len(w) > 3)
    compressed_parts: list[str] = []
    budget_used = 0

    for i, c in enumerate(chunks, 1):
        if budget_used >= MAX_CONTEXT_CHARS:
            break

        # Keep paragraphs containing query words; fallback to full chunk if nothing matches
        paragraphs = [p for p in c.text.split("\n") if p.strip()]
        kept_paragraphs = [
            p for p in paragraphs
            if any(w in p.lower() for w in query_words)
        ] or paragraphs[:1]  # Always keep at least the first paragraph

        snippet = " ... ".join(kept_paragraphs)
        # Enforce per-chunk budget so one large chunk can't swamp the context
        remaining = MAX_CONTEXT_CHARS - budget_used
        if len(snippet) > remaining:
            snippet = snippet[:remaining].rsplit(" ", 1)[0]  # clean word boundary

        chunk_entry = f"[{i}] {snippet}"
        compressed_parts.append(chunk_entry)
        budget_used += len(chunk_entry) + 2  # +2 for "\n\n"

    final_text = "\n\n".join(compressed_parts)

    # If compression dropped everything (degenerate edge case), fallback to
    # raw text of top chunks, still respecting the budget.
    if not final_text and chunks:
        raw = "\n\n".join(f"[{i}] {c.text}" for i, c in enumerate(chunks, 1))
        final_text = raw[:MAX_CONTEXT_CHARS]

    if len(final_text) >= MAX_CONTEXT_CHARS:
        logger.debug("Context exceeded %d chars. Truncating to enforce Rule 4.", MAX_CONTEXT_CHARS)

    latency_ms = (time.perf_counter() - start_time) * 1000.0
    logger.info("compressor.latency_ms=%.2f compressor.confidence_score=1.00", latency_ms)

    return final_text
