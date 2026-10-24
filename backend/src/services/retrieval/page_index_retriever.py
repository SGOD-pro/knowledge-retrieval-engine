import logging
import time
from collections.abc import Sequence

from config import settings
from ingestion.page_index_service import score as structural_score
from schemas.models import Chunk
from src.schemas.contracts.page_index import StructuralLookupResult
from src.services.retrieval.page_index_lookup import (
    PageIndexLookupService,
    get_page_index_controller,
)

logger = logging.getLogger(__name__)


class PageIndexRetriever:
    """Stage 2 retrieval module utilizing structural weight scoring and isolated lookup.

    Rule 5: BM25 runs before PageIndex. PageIndex scopes Vector.
    Rule 10: Logs latency_ms and confidence_score.
    """

    def __init__(self, lookup_service: PageIndexLookupService | None = None) -> None:
        self.lookup_service = lookup_service or PageIndexLookupService()

    async def lookup(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        target_label: str,
        deadline_ms: float | None = None,
    ) -> tuple[StructuralLookupResult | None, str]:
        """Perform deadline-enforced, concurrency-bounded isolated lookup."""
        return await self.lookup_service.lookup_target(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            target_label=target_label,
            deadline_ms=deadline_ms,
        )

    def lookup_sync(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        target_label: str,
        deadline_ms: float = 50.0,
    ) -> tuple[StructuralLookupResult | None, str]:
        """Perform synchronous deadline-enforced, concurrency-bounded isolated lookup."""
        return self.lookup_service.lookup_target_sync(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            target_label=target_label,
            deadline_ms=deadline_ms,
        )

    def filter_and_rank(
        self, query: str, candidates: Sequence[Chunk], top_k: int = 10
    ) -> tuple[list[Chunk], list[int], list[str]]:
        start_time = time.perf_counter()

        if not candidates:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            logger.info(
                "page_index.latency_ms=%.2f page_index.confidence_score=0.00",
                latency_ms,
            )
            return [], [], []

        scored_chunks = [
            (chunk, structural_score(chunk, query)) for chunk in candidates
        ]
        pre_count = len(scored_chunks)
        scored_chunks = [
            item for item in scored_chunks if item[1] >= settings.PAGEINDEX_THRESHOLD
        ]
        post_count = len(scored_chunks)
        logger.debug(
            "page_index.filter_stats threshold=%.2f pre=%d post=%d",
            settings.PAGEINDEX_THRESHOLD,
            pre_count,
            post_count,
        )

        scored_chunks.sort(key=lambda item: item[1], reverse=True)

        selected = [chunk for chunk, _ in scored_chunks[:top_k]]
        candidate_pages = sorted(
            list(
                {
                    chunk.page_number
                    for chunk in selected
                    if chunk.page_number is not None
                }
            )
        )
        candidate_chunk_ids = [
            str(chunk.id) for chunk in selected if chunk.page_number is None
        ]

        avg_score = (
            sum(s for _, s in scored_chunks[:top_k]) / max(1, len(selected))
            if selected
            else 0.0
        )
        confidence_score = min(1.0, avg_score / 10.0)
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        logger.info(
            "page_index.latency_ms=%.2f page_index.confidence_score=%.2f",
            latency_ms,
            confidence_score,
        )
        return selected, candidate_pages, candidate_chunk_ids
