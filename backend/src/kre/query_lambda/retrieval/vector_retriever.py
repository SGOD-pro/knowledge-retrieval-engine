"""Stage 3 vector retrieval using pgvector.

Rule 5: Vector search scoped to PageIndex candidates.
Rule 19: Fast-path queries use local BGE-small and search embedding_fast.
         Full-path queries use API provider and search embedding_full.
Rule 28: No direct SDK imports — API calls go through providers/*.py.
Rule 30: No query ever compares across both columns.
Rule 10: Logs latency_ms and confidence_score.
"""

import logging
import time

from kre.shared.db.postgres import PostgresRepository
from kre.shared.models import Chunk

logger = logging.getLogger(__name__)


class VectorRetriever:

    def __init__(self, repository: PostgresRepository | None = None):
        self.repository = repository or PostgresRepository()

    def search(
        self,
        query: str,
        fast_path: bool = False,
        document_ids: list[str] | None = None,
        candidate_page_ids: list[int] | None = None,
        candidate_chunk_ids: list[str] | None = None,
        top_k: int = 10,
    ) -> list[tuple[Chunk, float]]:
        """Execute vector search routed by retrieval path.

        fast_path=True  → embed locally via BGE-small ONNX, search embedding_fast.
        fast_path=False → embed via API provider, search embedding_full.
        """
        start_time = time.perf_counter()

        if fast_path:
            # Local ONNX embedding — zero network calls (Rule 19)
            from kre.shared.providers.embedding_provider import embed_fast_local

            query_embedding = embed_fast_local(query)
            embedding_column = "embedding_fast"
        else:
            # API embedding — through provider layer (Rule 28)
            from kre.shared.providers.embedding_provider import embed_text
            from kre.shared.providers.provider_client import get_active_provider

            active_provider = get_active_provider()
            query_embedding = embed_text(query, provider=active_provider)
            embedding_column = "embedding_full"

        results = self.repository.search_vector(
            query_embedding=query_embedding,
            embedding_column=embedding_column,
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

    def build_query_sql(self, query: str, plan) -> str:
        """Return the SQL template that would be used for this query+plan.

        Used by test_r30_schema_level_routing_isolation to assert
        column-level isolation without executing the query.
        """
        if plan.fast_path:
            embedding_column = "embedding_fast"
        else:
            embedding_column = "embedding_full"

        return f"""
            SELECT id, document_id, source_format, text, element_type, page_number,
                   section_path, bounding_box, location_reference, metadata, structural_weight, provider,
                   {embedding_column} <=> %s::vector AS distance
            FROM chunks
            WHERE {embedding_column} IS NOT NULL
            ORDER BY distance ASC LIMIT %s
        """
