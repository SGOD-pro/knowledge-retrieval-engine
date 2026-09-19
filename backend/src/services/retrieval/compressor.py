import logging
import time

from schemas.models import Chunk

logger = logging.getLogger(__name__)


def compress_chunks(query: str, chunks: list[Chunk]) -> str:
    """
    Stage 8: Compression
    Extracts relevant snippets from chunks to minimize token count.
    Pure Python logic.

    For structured/tabular chunks (CSV, XLS/XLSX, table rows/cells), ALL rows
    are included without query-word filtering.  Natural-language query terms
    rarely appear verbatim inside column-value rows, so paragraph-level
    filtering silently drops the evidence and causes false refusals.
    """
    start_time = time.perf_counter()

    _TABULAR_FORMATS = {"csv", "xlsx", "xls"}
    _TABULAR_ELEMENT_TYPES = {"table_row", "cell", "table", "header_row"}

    _COMPRESSION_STOPWORDS = {
        "what", "when", "where", "which", "who", "how", "does", "did",
        "is", "are", "was", "were", "the", "this", "that", "with",
        "from", "into", "for", "and", "but", "not", "you", "all",
        "can", "had", "her", "his", "has", "have", "will", "been",
    }
    query_words = set(
        w.lower().strip(".,!?:;\"'()[]{}")
        for w in query.split()
        if w.lower().strip(".,!?:;\"'()[]{}") not in _COMPRESSION_STOPWORDS
        and len(w.strip(".,!?:;\"'()[]{}")) > 1
    )
    compressed_text = []

    for c in chunks:
        # --- Tabular chunks: include every row, no query-word filtering ---
        if (
            getattr(c, "source_format", "") in _TABULAR_FORMATS
            or getattr(c, "element_type", "") in _TABULAR_ELEMENT_TYPES
        ):
            if c.text.strip():
                compressed_text.append(f"[{c.id}] {c.text.strip()}")
            continue

        # --- Prose/PDF chunks: keep paragraphs containing query words ---
        paragraphs = [p for p in c.text.split("\n") if p.strip()]
        kept_paragraphs = []
        for p in paragraphs:
            p_lower = p.lower()
            if any(w in p_lower for w in query_words) or (not query_words and len(paragraphs) == 1):
                kept_paragraphs.append(p)

        if kept_paragraphs:
            compressed_text.append(f"[{c.id}] " + " ... ".join(kept_paragraphs))

    final_text = "\n\n".join(compressed_text)

    # If compression dropped everything (no query word matched paragraphs),
    # fallback to the top chunks preserving evidence boundaries within budget.
    if not final_text and chunks:
        # Keep up to 3 top chunks within token budget (~1500 tokens / 6000 chars)
        budget_chars = 6000
        fallback_parts = []
        cur_len = 0
        for c in chunks:
            part = f"[{c.id}] {c.text.strip()}"
            if cur_len + len(part) > budget_chars:
                break
            fallback_parts.append(part)
            cur_len += len(part)
        final_text = "\n\n".join(fallback_parts)

    latency_ms = (time.perf_counter() - start_time) * 1000.0
    logger.info(
        "compressor.latency_ms=%.2f compressor.confidence_score=1.00", latency_ms
    )

    return final_text


class Compressor:
    """Wrapper class providing object-oriented interface for token compression."""

    def compress(
        self,
        chunks: list[Chunk],
        query: str = "",
        max_tokens: int = 1200,
    ) -> str:
        q_words = set(query.lower().split()) if query else set()
        sorted_chunks = sorted(
            chunks,
            key=lambda c: sum(1 for w in q_words if w in c.text.lower()),
            reverse=True,
        )
        text = compress_chunks(query, sorted_chunks)
        char_limit = max_tokens * 4
        if len(text) > char_limit:
            text = text[:char_limit].rsplit("\n", 1)[0]
        return text

