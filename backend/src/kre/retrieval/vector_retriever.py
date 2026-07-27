import logging
import time

from kre.db.postgres import PostgresRepository
from kre.models import Chunk
from kre.providers.embedding_provider import embed_text
from kre.providers.provider_client import get_active_provider

logger = logging.getLogger(__name__)


class VectorRetriever:
    """Stage 3 vector retrieval using pgvector.

    Rule 5: Vector search scoped to PageIndex candidates.
    Rule 19: Uses embedding_provider.py via provider layer.
    Rule 28: No direct SDK imports.
    Rule 30: Query and chunk provider must match.
    Rule 10: Logs latency_ms and confidence_score.
    """

    def __init__(self, repository: PostgresRepository | None = None):
        self.repository = repository or PostgresRepository()

    def search(
        self,
        query: str,
        document_ids: list[str] | None = None,
        candidate_page_ids: list[int] | None = None,
        candidate_chunk_ids: list[str] | None = None,
        top_k: int = 10,
        provider: str | None = None,
    ) -> list[tuple[Chunk, float]]:
        start_time = time.perf_counter()

        active_provider = provider or get_active_provider()
        query_embedding = embed_text(query, provider=active_provider)

        results = self.repository.search_vector(
            query_embedding=query_embedding,
            provider=active_provider,
            document_ids=document_ids,
            candidate_page_ids=candidate_page_ids,
            candidate_chunk_ids=candidate_chunk_ids,
            limit=top_k,
        )

        avg_sim = sum(sim for _, sim in results) / max(1, len(results)) if results else 0.0
        confidence_score = min(1.0, max(0.0, avg_sim))
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        logger.info("vector.latency_ms=%.2f vector.confidence_score=%.2f", latency_ms, confidence_score)
        return results
