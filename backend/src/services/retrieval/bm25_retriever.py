import hashlib
import logging
import re
import threading
import time
from typing import Sequence

from rank_bm25 import BM25Okapi
from config import settings

from schemas.models import Chunk

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level BM25 cache
# Cache key = MD5 of sorted chunk IDs (content-hash invalidation).
# This fixes the STALE_INDEX_BUG from the kre/ tree which used len(chunks)
# as the key — two different corpora of equal size would share the same index.
# threading.Lock is required: Lambda containers handle concurrent invocations
# and share module-level globals.
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_bm25_cache: dict[str, BM25Okapi] = {}
_corpus_cache: dict[str, list] = {}   # maps key → ordered chunks list


def _corpus_key(chunks: Sequence[Chunk]) -> str:
    return hashlib.md5(",".join(sorted(c.id for c in chunks)).encode()).hexdigest()


def _get_bm25(chunks: Sequence[Chunk]) -> tuple[BM25Okapi, list]:
    """Return (BM25Okapi, ordered_chunks_list) for the given corpus, using cache."""
    key = _corpus_key(chunks)
    with _lock:
        if key not in _bm25_cache:
            ordered = list(chunks)
            _bm25_cache[key] = BM25Okapi([_tokenize(c.text) for c in ordered])
            _corpus_cache[key] = ordered
        return _bm25_cache[key], _corpus_cache[key]


def _tokenize(text: str) -> list[str]:
    return [word.lower() for word in re.findall(r"\w+", text)]


class BM25Retriever:
    """Stage 1 retrieval module using rank-bm25.

    Rule 5: BM25 runs before PageIndex (PageIndex removed as pipeline stage in Phase 3).
    Rule 10: Logs latency_ms and confidence_score.
    """

    def search(self, query: str, chunks: Sequence[Chunk], top_k: int = 10) -> list[tuple[Chunk, float]]:
        start_time = time.perf_counter()

        if not chunks:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            logger.info("bm25.latency_ms=%.2f bm25.confidence_score=0.00", latency_ms)
            return []

        bm25, ordered_chunks = _get_bm25(chunks)
        tokenized_query = _tokenize(query)

        scores = bm25.get_scores(tokenized_query)
        pre_count = len(ordered_chunks)
        scored_chunks = [(chunk, float(s)) for chunk, s in zip(ordered_chunks, scores) if float(s) >= settings.BM25_THRESHOLD]
        post_count = len(scored_chunks)
        print(f"[DEBUG BM25] Threshold: {settings.BM25_THRESHOLD}, Pre-filter: {pre_count}, Post-filter: {post_count}")

        scored_chunks.sort(key=lambda item: item[1], reverse=True)
        results = scored_chunks[:top_k]

        max_score = results[0][1] if results else 0.0
        confidence_score = min(1.0, max_score / 10.0) if max_score > 0 else 0.0
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        logger.info("bm25.latency_ms=%.2f bm25.confidence_score=%.2f", latency_ms, confidence_score)
        return results
