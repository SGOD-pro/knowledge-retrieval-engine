import logging
import time
from typing import Sequence

from kre.ingestion.page_index_service import score as structural_score
from kre.models import Chunk

logger = logging.getLogger(__name__)


class PageIndexRetriever:
    """Stage 2 retrieval module utilizing structural weight scoring.

    Rule 5: BM25 runs before PageIndex. PageIndex scopes Vector.
    Rule 10: Logs latency_ms and confidence_score.
    """

    def filter_and_rank(self, query: str, candidates: Sequence[Chunk], top_k: int = 10) -> tuple[list[Chunk], list[int]]:
        start_time = time.perf_counter()

        if not candidates:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            logger.info("page_index.latency_ms=%.2f page_index.confidence_score=0.00", latency_ms)
            return [], []

        scored_chunks = [(chunk, structural_score(chunk, query)) for chunk in candidates]
        scored_chunks.sort(key=lambda item: item[1], reverse=True)

        selected = [chunk for chunk, _ in scored_chunks[:top_k]]
        candidate_pages = sorted(list({chunk.page_number for chunk in selected if chunk.page_number is not None}))

        avg_score = sum(s for _, s in scored_chunks[:top_k]) / max(1, len(selected)) if selected else 0.0
        confidence_score = min(1.0, avg_score / 10.0)
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        logger.info("page_index.latency_ms=%.2f page_index.confidence_score=%.2f", latency_ms, confidence_score)
        return selected, candidate_pages
