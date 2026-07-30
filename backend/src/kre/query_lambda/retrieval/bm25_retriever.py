import logging
import re
import time
from typing import Sequence

from rank_bm25 import BM25Okapi

from kre.shared.models import Chunk

logger = logging.getLogger(__name__)


def _tokenize(text: str) -> list[str]:
    return [word.lower() for word in re.findall(r"\w+", text)]


class BM25Retriever:
    """Stage 1 retrieval module using rank-bm25.

    Rule 5: BM25 runs before PageIndex.
    Rule 10: Logs latency_ms and confidence_score.
    """

    def search(self, query: str, chunks: Sequence[Chunk], top_k: int = 10) -> list[tuple[Chunk, float]]:
        start_time = time.perf_counter()

        if not chunks:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            logger.info("bm25.latency_ms=%.2f bm25.confidence_score=0.00", latency_ms)
            return []

        tokenized_corpus = [_tokenize(chunk.text) for chunk in chunks]
        bm25 = BM25Okapi(tokenized_corpus)
        tokenized_query = _tokenize(query)

        scores = bm25.get_scores(tokenized_query)
        scored_chunks = list(zip(chunks, [float(s) for s in scores]))

        scored_chunks.sort(key=lambda item: item[1], reverse=True)
        results = scored_chunks[:top_k]

        max_score = results[0][1] if results else 0.0
        # Normalize score to 0..1 range for confidence metric
        confidence_score = min(1.0, max_score / 10.0) if max_score > 0 else 0.0
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        logger.info("bm25.latency_ms=%.2f bm25.confidence_score=%.2f", latency_ms, confidence_score)
        return results
