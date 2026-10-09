from typing import Any
from src.schemas.contracts.boundary import CachedQueryResult, QueryExecutionOutcome, CacheKeyDescriptor

class ControlledFixtureExecutionEngine:
    """Controlled fixture execution engine proving boundary enforcement for this slice."""

    def recompute_query(
        self,
        workspace_id: str,
        query: Any,
        descriptor: CacheKeyDescriptor,
        table_store: Any,
    ) -> QueryExecutionOutcome:
        """Recompute query over surviving sources within the pinned snapshot."""
        # Query text extraction
        query_text = str(query)

        # Check pinned snapshot
        snapshot = table_store.get_snapshot(workspace_id, descriptor.snapshot_id)
        active_sources = snapshot.source_versions if snapshot else ()

        surviving = []
        tombstoned = []
        for s_id, s_ver in active_sources:
            if table_store.has_tombstone(workspace_id, "source", s_id):
                tombstoned.append(s_id)
            else:
                surviving.append(s_id)

        # Check if query is an aggregate query requiring all sources
        is_aggregate = "sum" in query_text.lower() or "aggregate" in query_text.lower() or "total" in query_text.lower()

        if is_aggregate and tombstoned:
            return QueryExecutionOutcome(
                execution_id=f"exec_{workspace_id}_incomplete",
                status="INSUFFICIENT_COVERAGE",
                answer="Aggregate calculation aborted: supporting source was tombstoned",
                citations=(),
                surviving_source_ids=tuple(surviving),
                tombstoned_source_ids=tuple(tombstoned),
            )

        return QueryExecutionOutcome(
            execution_id=f"exec_{workspace_id}_recomputed",
            status="SUCCESS",
            answer=f"Grounded response for query: {query_text}",
            citations=(),
            surviving_source_ids=tuple(surviving),
            tombstoned_source_ids=tuple(tombstoned),
        )


class InMemoryCacheStore:
    """In-memory cache store fixture simulating Redis cache with graceful fallback."""

    def __init__(self) -> None:
        self._entries: dict[str, CachedQueryResult] = {}
        self.is_connected: bool = True

    def get_cached_query_by_key(self, cache_key: str) -> CachedQueryResult | None:
        if not self.is_connected:
            raise ConnectionError("Redis cache connection unavailable")
        return self._entries.get(cache_key)

    def set_cached_query(self, cache_key: str, result: CachedQueryResult) -> None:
        if not self.is_connected:
            raise ConnectionError("Redis cache connection unavailable")
        self._entries[cache_key] = result

    def clear(self) -> None:
        self._entries.clear()
