import logging
from typing import Any

from src.schemas.contracts.boundary import QueryExecutionOutcome, CachedQueryResult
from src.schemas.models import QueryRequest

logger = logging.getLogger(__name__)

class ProductionExecutionEngine:
    """Production execution engine adapter for the boundary delivery gate."""
    def recompute_query(
        self,
        workspace_id: str,
        query: Any,
        descriptor: Any,
        table_store: Any,
    ) -> QueryExecutionOutcome:
        from src.modules.query.query_service import query_service
        
        req = QueryRequest(
            query=str(query),
            workspace_id=workspace_id,
            cache=False,
        )
        
        res = query_service.execute_query(req)
        
        status = "SUCCESS"
        ans = res.get("answer", "")
        if ans == "NOT_FOUND" or res.get("error_code") == "incomplete_data":
            status = "INSUFFICIENT_COVERAGE"
            
        return QueryExecutionOutcome(
            execution_id="exec_" + workspace_id,
            status=status,
            answer=ans,
            citations=tuple(),
            surviving_source_ids=tuple(),
            tombstoned_source_ids=tuple(),
        )

class RedisCacheAdapter:
    """Adapter bridging the boundary cache interface to RedisCache."""
    def get_cached_query_by_key(self, cache_key: str) -> CachedQueryResult | None:
        from src.db.redis_cache import cache
        
        data = cache.get_cache(cache_key)
        if data:
            try:
                return CachedQueryResult(**data)
            except Exception as e:
                logger.warning(f"Failed to parse CachedQueryResult: {e}")
        return None

    def set_cached_query(self, cache_key: str, result: CachedQueryResult) -> None:
        from src.db.redis_cache import cache
        cache.set_cache(cache_key, result.model_dump(mode="json"), 3600)

