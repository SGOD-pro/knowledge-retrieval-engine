import logging
import time
from collections.abc import Sequence

from rank_bm25 import BM25Plus

from config import settings
from schemas.models import Chunk

logger = logging.getLogger(__name__)


import re

# Workspace-level chunk cache with TTL to avoid repeated DynamoDB full scans
_CHUNK_CACHE: dict[str, tuple[float, list[Chunk]]] = {}  # key -> (expiry_timestamp, chunks)
_CHUNK_CACHE_TTL = 3600.0  # 1 hour cache to prevent repetitive DynamoDB scans


def get_cached_chunks(workspace_id: str, loader_fn, corpus_version: str = "") -> list[Chunk]:
    """Return cached chunks for a workspace and corpus version, refreshing from loader_fn if expired."""

    key = f"{workspace_id}::{corpus_version}" if corpus_version else workspace_id
    now = time.monotonic()
    cached = _CHUNK_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]
    chunks = loader_fn()
    _CHUNK_CACHE[key] = (now + _CHUNK_CACHE_TTL, chunks)
    if len(_CHUNK_CACHE) > 10:
        oldest_key = min(_CHUNK_CACHE, key=lambda k: _CHUNK_CACHE[k][0])
        del _CHUNK_CACHE[oldest_key]
    return chunks


def invalidate_chunk_cache(workspace_id: str | None = None):
    """Invalidate chunk cache for a workspace (prefix match), or all if None."""
    if workspace_id:
        to_del = [k for k in _CHUNK_CACHE if k == workspace_id or k.startswith(f"{workspace_id}::")]
        for k in to_del:
            _CHUNK_CACHE.pop(k, None)
    else:
        _CHUNK_CACHE.clear()


def _stem(word: str) -> str:
    """Lightweight rule-based English stemmer for BM25 keyword matching."""
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"  # inventories -> inventory
    if word.endswith("es") and len(word) > 4 and word[-3] in "sxyzc":
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]        # papers -> paper, stocks -> stock
    if word.endswith("ing") and len(word) > 5:
        return word[:-3]
    if word.endswith("ed") and len(word) > 4:
        return word[:-2]
    return word


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric word tokenization with stemming support."""
    words = re.findall(r"\w+", text.lower())
    tokens = []
    for w in words:
        tokens.append(w)
        s = _stem(w)
        if s != w:
            tokens.append(s)
    return tokens


# Cache BM25 index keyed on chunk id tuple so repeated queries don't re-index.
# Sequence[Chunk] is converted to a tuple of ids for the cache key.
_BM25_CACHE: dict[tuple[str, ...], tuple[BM25Plus, list[Chunk]]] = {}


def _chunk_to_text(c: Chunk) -> str:
    """Enrich chunk text with section path and metadata for BM25 indexing."""
    parts = [c.text]
    if c.section_path:
        parts.extend(c.section_path)
    if c.location_reference:
        parts.append(c.location_reference)
    if isinstance(c.metadata, dict):
        for v in c.metadata.values():
            if isinstance(v, str):
                parts.append(v)
    return " ".join(parts)


def _get_bm25(chunks: Sequence[Chunk]) -> tuple[BM25Plus, list[Chunk]]:
    chunk_list = list(chunks)
    key = tuple(c.id for c in chunk_list)
    if key not in _BM25_CACHE:
        # Keep cache bounded
        if len(_BM25_CACHE) > 8:
            _BM25_CACHE.pop(next(iter(_BM25_CACHE)))
        corpus = [_tokenize(_chunk_to_text(c)) for c in chunk_list]
        _BM25_CACHE[key] = (BM25Plus(corpus), chunk_list)
    return _BM25_CACHE[key]


class BM25Retriever:
    """BM25 keyword retriever.

    Rule 5: BM25 runs before PageIndex (PageIndex removed as pipeline stage in Phase 3).
    Rule 10: Logs latency_ms and confidence_score.
    """

    def search(
        self, query: str, chunks: Sequence[Chunk], top_k: int = 10
    ) -> list[tuple[Chunk, float]]:
        start_time = time.perf_counter()

        if not chunks:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            logger.info("bm25.latency_ms=%.2f bm25.confidence_score=0.00", latency_ms)
            return []

        bm25, ordered_chunks = _get_bm25(chunks)
        tokenized_query = _tokenize(query)

        scores = bm25.get_scores(tokenized_query)
        pre_count = len(ordered_chunks)
        scored_chunks = [
            (chunk, float(s))
            for chunk, s in zip(ordered_chunks, scores)
            if float(s) >= settings.BM25_THRESHOLD
        ]
        post_count = len(scored_chunks)
        logger.debug(
            "bm25.filter_stats threshold=%.2f pre=%d post=%d",
            settings.BM25_THRESHOLD,
            pre_count,
            post_count,
        )

        scored_chunks.sort(key=lambda item: item[1], reverse=True)

        # Enforce document diversity so a single large document doesn't monopolize top_k
        unique_docs = {str(getattr(c, "document_id", "")) for c, _ in scored_chunks}
        if len(unique_docs) > 1 and top_k > 5:
            max_per_doc = max(6, top_k // 2)
            doc_counts: dict[str, int] = {}
            results = []
            deferred = []
            for item in scored_chunks:
                doc_id = str(getattr(item[0], "document_id", ""))
                cnt = doc_counts.get(doc_id, 0)
                if cnt < max_per_doc:
                    results.append(item)
                    doc_counts[doc_id] = cnt + 1
                    if len(results) == top_k:
                        break
                else:
                    deferred.append(item)
            if len(results) < top_k:
                for item in deferred:
                    results.append(item)
                    if len(results) == top_k:
                        break
        else:
            results = scored_chunks[:top_k]


        max_score = results[0][1] if results else 0.0
        confidence_score = min(1.0, max_score / 10.0) if max_score > 0 else 0.0
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        logger.info(
            "bm25.latency_ms=%.2f bm25.confidence_score=%.2f",
            latency_ms,
            confidence_score,
        )
        return results
