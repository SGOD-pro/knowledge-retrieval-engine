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
            if any(w in p_lower for w in query_words) or len(paragraphs) == 1:
                kept_paragraphs.append(p)

        if kept_paragraphs:
            compressed_text.append(f"[{c.id}] " + " ... ".join(kept_paragraphs))

    final_text = "\n\n".join(compressed_text)

    from services.retrieval.planner import extract_entities

    entities = extract_entities(query)
    final_text_lower = final_text.lower()
    missing_entities = False
    if final_text:
        for e in entities:
            if e.lower() not in final_text_lower:
                missing_entities = True
                break

    # If compression dropped everything (e.g. no query word matched exactly),
    # OR if it dropped critical query entities, fallback to just sending the raw text.
    if missing_entities or (not final_text and chunks):
        final_text = "\n\n".join(f"[{c.id}] {c.text}" for c in chunks)

    latency_ms = (time.perf_counter() - start_time) * 1000.0
    # Compression doesn't score confidence natively, but we must log it
    logger.info(
        "compressor.latency_ms=%.2f compressor.confidence_score=1.00", latency_ms
    )

    return final_text
