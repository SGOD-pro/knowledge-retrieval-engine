import logging
import time

from schemas.models import Chunk

logger = logging.getLogger(__name__)


def compress_chunks(query: str, chunks: list[Chunk]) -> str:
    """
    Stage 8: Compression
    Extracts relevant snippets from chunks to minimize token count while preserving evidence.
    Pure Python logic.

    Guarantees:
      - Interleaves chunks across documents (round-robin) to prevent single-document starvation in multi-hop queries.
      - Never drops semantic prose chunks simply because exact query keywords are paraphrased.
      - Condenses wide tabular rows by keeping identifying context plus query-matching columns without overflowing context.
      - Bounds total context to ~5500 chars, ensuring all selected chunks fit within LLM context budget.
    """
    start_time = time.perf_counter()

    if not chunks:
        return ""

    _TABULAR_FORMATS = {"csv", "xlsx", "xls"}
    _TABULAR_ELEMENT_TYPES = {"table_row", "cell", "table", "header_row", "row"}

    _COMPRESSION_STOPWORDS = {
        "what", "when", "where", "which", "who", "how", "does", "did",
        "is", "are", "was", "were", "the", "this", "that", "with",
        "from", "into", "for", "and", "but", "not", "you", "all",
        "can", "had", "her", "his", "has", "have", "will", "been",
        "tell", "give", "about", "using", "across", "between",
    }
    query_words = set(
        w.lower().strip(".,!?:;\"'()[]{}")
        for w in query.split()
        if w.lower().strip(".,!?:;\"'()[]{}") not in _COMPRESSION_STOPWORDS
        and len(w.strip(".,!?:;\"'()[]{}")) > 1
    )

    # 1. Round-robin interleave chunks across documents
    from collections import defaultdict
    doc_chunks = defaultdict(list)
    for c in chunks:
        doc_chunks[str(getattr(c, "document_id", ""))].append(c)

    interleaved_chunks = []
    if len(doc_chunks) > 1:
        queues = [list(c_list) for c_list in doc_chunks.values()]
        while any(queues):
            for q in queues:
                if q:
                    interleaved_chunks.append(q.pop(0))
    else:
        interleaved_chunks = list(chunks)

    # 2. Allocate fair character budget per chunk
    num_chunks = len(interleaved_chunks)
    budget_per_chunk = min(1200, max(600, 4800 // max(1, num_chunks)))
    MAX_TOTAL_CHARS = 5500

    compressed_text = []
    cur_len = 0

    for c in interleaved_chunks:
        c_text = c.text.strip()
        if not c_text:
            continue

        is_tabular = (
            getattr(c, "source_format", "") in _TABULAR_FORMATS
            or getattr(c, "element_type", "") in _TABULAR_ELEMENT_TYPES
        )

        snippet = ""
        if is_tabular:
            if len(c_text) <= budget_per_chunk:
                snippet = c_text
            else:
                # Wide tabular row: split into parts (header: value pairs)
                parts = [p.strip() for p in c_text.replace("\n", ". ").split(". ") if p.strip()]
                if len(parts) <= 3:
                    snippet = c_text[:budget_per_chunk].rsplit(" ", 1)[0]
                else:
                    # Keep first 2 identifying fields (e.g. District, State, Year, Industry)
                    kept_indices = {0, 1}
                    if len(parts) > 2 and ":" in parts[2]:
                        kept_indices.add(2)
                    # Keep fields matching query words or numbers
                    for idx, p in enumerate(parts):
                        p_lower = p.lower()
                        if any(w in p_lower for w in query_words):
                            kept_indices.add(idx)
                    # Reconstruct row preserving order
                    selected_parts = [parts[i] for i in sorted(kept_indices) if i < len(parts)]
                    snippet = ". ".join(selected_parts)
                    if not snippet.endswith("."):
                        snippet += "."
                    if len(snippet) > budget_per_chunk:
                        snippet = snippet[:budget_per_chunk].rsplit(" ", 1)[0] + "."
        else:
            # Prose / PDF chunks
            paragraphs = [p.strip() for p in c_text.split("\n") if p.strip()]
            kept_paragraphs = []
            for p in paragraphs:
                p_lower = p.lower()
                if any(w in p_lower for w in query_words):
                    kept_paragraphs.append(p)

            if kept_paragraphs:
                joined = " ... ".join(kept_paragraphs)
                if len(joined) > budget_per_chunk:
                    joined = joined[:budget_per_chunk].rsplit(" ", 1)[0] + "..."
                snippet = joined
            else:
                # No query words matched verbatim: keep the beginning of the chunk (semantic match)
                if len(c_text) > budget_per_chunk:
                    snippet = c_text[:budget_per_chunk].rsplit(" ", 1)[0] + "..."
                else:
                    snippet = c_text

        if not snippet:
            continue

        part_str = f"[{c.id}] {snippet}"
        if cur_len + len(part_str) > MAX_TOTAL_CHARS and compressed_text:
            remaining = MAX_TOTAL_CHARS - cur_len - len(f"[{c.id}] ")
            if remaining > 250:
                trimmed = snippet[:remaining].rsplit(" ", 1)[0] + "..."
                compressed_text.append(f"[{c.id}] {trimmed}")
            break

        compressed_text.append(part_str)
        cur_len += len(part_str) + 2

    final_text = "\n\n".join(compressed_text)

    # Fallback if somehow empty
    if not final_text and chunks:
        fallback_parts = []
        cur_len = 0
        for c in interleaved_chunks:
            part = f"[{c.id}] {c.text.strip()[:budget_per_chunk]}"
            if cur_len + len(part) > MAX_TOTAL_CHARS:
                break
            fallback_parts.append(part)
            cur_len += len(part) + 2
        final_text = "\n\n".join(fallback_parts)

    latency_ms = (time.perf_counter() - start_time) * 1000.0
    logger.info(
        "compressor.latency_ms=%.2f compressor.confidence_score=1.00 chunks_in=%d chunks_out=%d len=%d",
        latency_ms,
        len(chunks),
        len(compressed_text),
        len(final_text),
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

