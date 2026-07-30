import logging
import time
from typing import Any
from kre.shared.db.postgres import PostgresRepository

logger = logging.getLogger(__name__)

class GraphRetriever:
    """Stage 5: Graph Expansion
    Rule 13: Graph MAX_HOPS=2 (3 for deep causal), MAX_NODES=40. Hardcoded constants.
    Rule 16: Graph activates only on relationship queries (handled by Planner).
    Rule 10: Logs latency_ms and confidence_score.
    """
    def __init__(self, repository: PostgresRepository | None = None):
        self.repository = repository or PostgresRepository()

    def expand(self, start_entities: list[str], max_hops: int = 2) -> list[dict[str, Any]]:
        start_time = time.perf_counter()
        results = []
        
        try:
            with self.repository._connect() as connection:
                # Find start concept IDs from entity names
                query_ids = """
                    SELECT id FROM concepts WHERE name = ANY(%s)
                """
                concept_rows = connection.execute(query_ids, (start_entities,)).fetchall()
                start_ids = [row[0] for row in concept_rows]
                
                if start_ids:
                    # Execute Recursive CTE
                    query_sql = """
                        WITH RECURSIVE graph_walk AS (
                            SELECT to_concept_id, relation_type, relation_weight, 1 AS hop
                            FROM relations
                            WHERE from_concept_id = ANY(%s)
                              AND relation_weight >= 0.3
                            UNION ALL
                            SELECT r.to_concept_id, r.relation_type, r.relation_weight, g.hop + 1
                            FROM relations r
                            JOIN graph_walk g ON r.from_concept_id = g.to_concept_id
                            WHERE g.hop < %s
                              AND r.relation_weight >= 0.3
                        )
                        SELECT DISTINCT to_concept_id, relation_type, relation_weight, hop
                        FROM graph_walk
                        LIMIT 40;
                    """
                    rows = connection.execute(query_sql, (start_ids, max_hops)).fetchall()
                    for row in rows:
                        results.append({
                            "concept_id": row[0],
                            "relation_type": row[1],
                            "relation_weight": row[2],
                            "hop": row[3],
                        })
        except Exception as e:
            logger.warning("Graph retrieval failed or tables do not exist: %s", str(e))
            
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        confidence_score = 1.0 if results else 0.0
        logger.info("graph.latency_ms=%.2f graph.confidence_score=%.2f", latency_ms, confidence_score)
        
        return results
