import logging
from pathlib import Path
from typing import Any

from src.schemas.contracts.boundary import QueryExecutionOutcome, CachedQueryResult
from src.schemas.models import QueryRequest

logger = logging.getLogger(__name__)

class ProductionExecutionEngine:
    """Production execution engine adapter for the boundary delivery gate."""
    def __init__(self, query_service: Any = None):
        self.query_service = query_service

    def execute_query(
        self,
        workspace_id: str,
        query_text: str,
        is_aggregate: bool = False,
        descriptor: Any = None,
        table_store: Any = None,
    ) -> QueryExecutionOutcome:
        return self.recompute_query(
            workspace_id=workspace_id,
            query=query_text,
            descriptor=descriptor,
            table_store=table_store,
        )

    def recompute_query(
        self,
        workspace_id: str,
        query: Any,
        descriptor: Any = None,
        table_store: Any = None,
    ) -> QueryExecutionOutcome:
        query_svc = self.query_service
        if query_svc is None:
            from src.modules.query.query_service import query_service
            query_svc = query_service
        from src.schemas.contracts.boundary import (
            CitationItem,
            QueryExecutionOutcome,
            StorageUnavailableError,
        )
        from src.schemas.contracts.location import DocumentLocation, TableLocation

        import re

        query_text = str(query)
        q_lower = query_text.lower()
        is_aggregate = bool(re.search(r"\b(sum|total|aggregate|count|average|earliest|latest|min|max)\b", q_lower))

        surviving: list[str] = []
        tombstoned: list[str] = []
        tables: list[str] = []

        # 1. Recheck pinned snapshot sources for tombstones
        if table_store and hasattr(table_store, "get_snapshot"):
            try:
                snap_id = getattr(descriptor, "snapshot_id", "snap_default")
                snapshot = table_store.get_snapshot(workspace_id, snap_id)
                active_sources = snapshot.source_versions if snapshot else ()
                for s_id, _ in active_sources:
                    if table_store.has_tombstone(workspace_id, "source", s_id):
                        tombstoned.append(s_id)
                    else:
                        surviving.append(s_id)
            except Exception as e:
                logger.warning("Tombstone check on snapshot failed: %s", e)

        # 2. Recheck active table sources in workspace for tombstones
        try:
            from db.table_store import get_shared_table_store
            store = get_shared_table_store()
            tables = store.list_tables(workspace_id)
            for tid in tables:
                ver = store.get_active_version(tid, workspace_id)
                t_meta = store.get_table(tid, workspace_id, version=ver)
                doc_id = t_meta.document_id if (t_meta and t_meta.document_id) else tid
                is_tomb = False
                if table_store and hasattr(table_store, "has_tombstone"):
                    if table_store.has_tombstone(workspace_id, "source", doc_id) or table_store.has_tombstone(workspace_id, "source", tid):
                        is_tomb = True
                if is_tomb:
                    if doc_id not in tombstoned:
                        tombstoned.append(doc_id)
                    if tid not in tombstoned:
                        tombstoned.append(tid)
                else:
                    if doc_id not in surviving:
                        surviving.append(doc_id)
                    if tid not in surviving:
                        surviving.append(tid)
        except Exception as e:
            logger.warning("Tombstone check on table store failed: %s", e)

        # 3. Recheck boundary sources in workspace for tombstones
        if table_store and hasattr(table_store, "list_sources"):
            try:
                for src in table_store.list_sources(workspace_id):
                    if table_store.has_tombstone(workspace_id, "source", src.source_id):
                        if src.source_id not in tombstoned:
                            tombstoned.append(src.source_id)
                    else:
                        if src.source_id not in surviving:
                            surviving.append(src.source_id)
            except Exception as e:
                logger.warning("Tombstone check on boundary sources failed: %s", e)

        # Collect all identifiable names for tombstoned items (ids, filenames, titles)
        tombstoned_names = set(tombstoned)
        if table_store and hasattr(table_store, "get_document_snapshot"):
            for t_id in list(tombstoned):
                try:
                    snap = table_store.get_document_snapshot(workspace_id, t_id)
                    if snap and snap.source:
                        tombstoned_names.add(snap.source)
                        tombstoned_names.add(Path(snap.source).stem)
                except Exception:
                    pass

        # If supporting source for an aggregate query was tombstoned, block complete aggregate
        target_tombstoned = False
        if is_aggregate and tombstoned:
            for t_name in tombstoned_names:
                t_low = t_name.lower()
                if t_low in q_lower or (t_low + ".csv") in q_lower or (t_low + ".pdf") in q_lower:
                    target_tombstoned = True
                    break
            if not target_tombstoned and tables:
                for tid in tables:
                    if tid.lower() in q_lower or (tid + ".csv").lower() in q_lower:
                        if tid in tombstoned_names:
                            target_tombstoned = True
                            break
                if not target_tombstoned and len(tables) == 1 and (tables[0] in tombstoned_names or any(t in tombstoned_names for t in tables)):
                    target_tombstoned = True
            elif not target_tombstoned and not surviving and tombstoned:
                target_tombstoned = True

        if is_aggregate and target_tombstoned:
            return QueryExecutionOutcome(
                execution_id=f"exec_{workspace_id}_incomplete",
                status="INSUFFICIENT_COVERAGE",
                answer="Aggregate calculation aborted: supporting source was tombstoned",
                citations=(),
                surviving_source_ids=tuple(surviving),
                tombstoned_source_ids=tuple(tombstoned),
                token_usage={"input_tokens": 0, "output_tokens": 0},
            )

        # Cache is disabled here to bypass legacy query cache in query_service.
        # Query caching is unified under the boundary delivery gate using CacheKeyDescriptor
        # which enforces tenant identity, snapshot generation binding, and tombstone checks.
        req = QueryRequest(
            query=query_text,
            workspace_id=workspace_id,
            cache=False,
        )

        res = query_svc.execute_query(req)

        # Distinguish infrastructure failure from insufficient evidence
        if res.get("status") == "error" and res.get("error_code") == "storage_failure":
            raise StorageUnavailableError(res.get("error", "Storage failure during execution"))

        status = "SUCCESS"
        ans = res.get("answer", "")
        if res.get("error_code") in ("incomplete_data", "unresolved_filter") or (is_aggregate and ans == "NOT_FOUND"):
            status = "INSUFFICIENT_COVERAGE"
        elif ans == "NOT_FOUND":
            ans = "I couldn't find any relevant passages in the workspace documents matching your query."

        citation_items: list[CitationItem] = []
        for idx, c in enumerate(res.get("citations", [])):
            try:
                s_id = str(c.get("document_id") or c.get("table_id") or "src_1")
                if table_store and hasattr(table_store, "has_tombstone"):
                    if table_store.has_tombstone(workspace_id, "source", s_id):
                        if s_id not in tombstoned:
                            tombstoned.append(s_id)
                        continue
                ev_id = str(c.get("evidence_id") or f"ev_{idx+1}")
                raw_v = c.get("document_version") or c.get("source_version") or 1
                try:
                    s_ver = max(1, int(raw_v))
                except (ValueError, TypeError):
                    s_ver = 1
                snippet = str(c.get("text") or c.get("text_snippet") or c.get("snippet") or "")[:2048]
                if not snippet:
                    snippet = f"Data from {s_id}"

                if "table_id" in c or c.get("source_format") in ("csv", "xlsx", "table") or c.get("evidence_type") == "structured_aggregate":
                    loc = TableLocation(
                        table_id=str(c.get("table_id") or s_id),
                        row_id=str(c.get("row_id") or c.get("location_reference") or "1"),
                    )
                else:
                    page_n = c.get("page_number", 1)
                    try:
                        page_i = max(1, int(page_n))
                    except (ValueError, TypeError):
                        page_i = 1
                    loc = DocumentLocation(
                        page=page_i,
                        element_id=c.get("chunk_id"),
                    )

                citation_items.append(
                    CitationItem(
                        evidence_id=ev_id,
                        source_id=s_id,
                        source_version=s_ver,
                        snippet=snippet,
                        location=loc,
                    )
                )
            except Exception as cit_err:
                logger.warning("Failed to parse CitationItem %d: %s", idx, cit_err)

        if not citation_items and not is_aggregate and tombstoned:
            ans = "I couldn't find any relevant passages in the workspace documents matching your query."

        for c_item in citation_items:
            if c_item.source_id not in surviving:
                surviving.append(c_item.source_id)
        for doc_id in res.get("document_ids", []):
            if str(doc_id) not in surviving:
                surviving.append(str(doc_id))


        raw_usage = res.get("token_usage") or {}
        usage_dict = {
            "input_tokens": int(raw_usage.get("input_tokens") or raw_usage.get("prompt_tokens") or 0),
            "output_tokens": int(raw_usage.get("output_tokens") or raw_usage.get("completion_tokens") or 0),
        }

        return QueryExecutionOutcome(
            execution_id="exec_" + workspace_id,
            status=status,
            answer=ans,
            citations=tuple(citation_items),
            surviving_source_ids=tuple(surviving),
            tombstoned_source_ids=tuple(tombstoned),
            token_usage=usage_dict,
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

