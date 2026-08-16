import logging
import time
from typing import Any

from db.database import CloudRepository

logger = logging.getLogger(__name__)


class GraphRetriever:
    """Stage 5: Graph Expansion
    Rule 13: Graph MAX_HOPS=2 (3 for deep causal), MAX_NODES=40. Hardcoded constants.
    Rule 16: Graph activates only on relationship queries (handled by Planner).
    Rule 10: Logs latency_ms and confidence_score.
    """

    def __init__(self, repository: CloudRepository | None = None):
        self.repository = repository or CloudRepository()

    def expand(
        self, start_entities: list[str], max_hops: int = 2
    ) -> list[dict[str, Any]]:
        start_time = time.perf_counter()

        results = self.repository.expand_graph(start_entities, max_hops)

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        confidence_score = 1.0 if results else 0.0
        logger.info(
            "graph.latency_ms=%.2f graph.confidence_score=%.2f",
            latency_ms,
            confidence_score,
        )

        return results
