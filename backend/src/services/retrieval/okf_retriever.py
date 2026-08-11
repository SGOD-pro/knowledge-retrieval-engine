import logging
from typing import Any
from db.database import CloudRepository

logger = logging.getLogger(__name__)

class OKFRetriever:
    """Stage 4: OKF Property Lookup
    Rule 17: OKF property lookup always runs in full path.
    Rule 10: Logs latency_ms and confidence_score.
    """
    def __init__(self, repository: CloudRepository | None = None):
        self.repository = repository or CloudRepository()

    def lookup(self, entities: list[str]) -> list[dict[str, Any]]:
        import time
        start_time = time.perf_counter()
        
        results = self.repository.get_okf_properties(entities)
            
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        confidence_score = 1.0 if results else 0.0
        logger.info("okf.latency_ms=%.2f okf.confidence_score=%.2f", latency_ms, confidence_score)
        
        return results
