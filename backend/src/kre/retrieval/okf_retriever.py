import logging
from typing import Any
from kre.db.postgres import PostgresRepository

logger = logging.getLogger(__name__)

class OKFRetriever:
    """Stage 4: OKF Property Lookup
    Rule 17: OKF property lookup always runs in full path.
    Rule 10: Logs latency_ms and confidence_score.
    """
    def __init__(self, repository: PostgresRepository | None = None):
        self.repository = repository or PostgresRepository()

    def lookup(self, entities: list[str]) -> list[dict[str, Any]]:
        import time
        start_time = time.perf_counter()
        results = []
        
        try:
            with self.repository._connect() as connection:
                # Assuming properties table exists: concept_id, property_name, property_value, source_chunk_id, confidence
                # We will match entity names to concepts and get properties
                query_sql = """
                    SELECT c.name, p.property_name, p.property_value, p.source_chunk_id, p.confidence
                    FROM concepts c
                    JOIN properties p ON c.id = p.concept_id
                    WHERE c.name = ANY(%s)
                """
                rows = connection.execute(query_sql, (entities,)).fetchall()
                for row in rows:
                    results.append({
                        "concept": row[0],
                        "property_name": row[1],
                        "property_value": row[2],
                        "source_chunk_id": row[3],
                        "confidence": row[4],
                    })
        except Exception as e:
            logger.warning("OKF lookup failed or tables do not exist: %s", str(e))
            
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        confidence_score = 1.0 if results else 0.0
        logger.info("okf.latency_ms=%.2f okf.confidence_score=%.2f", latency_ms, confidence_score)
        
        return results
