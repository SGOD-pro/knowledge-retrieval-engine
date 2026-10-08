"""Cache delivery gate, snapshot verification, and query recomputation evaluation.

Rechecks caller authorization, tombstones, and exact pinned snapshot bindings
before delivering cached query results; handles fallback recomputation over surviving
sources and emits explicit insufficient coverage on deleted aggregate support.
"""

import logging
from typing import Any

from src.schemas.contracts.boundary import (
    CachedQueryResult,
    CacheKeyDescriptor,
    CitationItem,
    QueryExecutionOutcome,
    SnapshotRecord,
    WorkspaceAccessDeniedError,
)
from src.schemas.contracts.envelope import TrustedAuthContext

logger = logging.getLogger(__name__)


def delivery_gate_recheck(
    cached_result: CachedQueryResult,
    auth_context: TrustedAuthContext,
    target_snapshot_id: str,
    table_store: Any,
) -> CachedQueryResult | None:
    """Verify that cached result is authorized, not tombstoned, and strictly consistent with pinned snapshot."""
    # 1. Recheck current caller authorization and scope
    if auth_context.authorized_workspace_id != cached_result.authorized_workspace_id:
        return None
    if auth_context.principal_id != cached_result.authorized_principal_id:
        return None
    if auth_context.access_policy_version != cached_result.access_policy_version:
        return None

    # 2. Recheck pinned snapshot identity match
    if target_snapshot_id != cached_result.snapshot_id:
        return None

    # 3. Recheck workspace deletion
    if table_store.has_tombstone(auth_context.authorized_workspace_id, "workspace", auth_context.authorized_workspace_id):
        return None

    # 4. Load authoritative pinned snapshot and verify active status
    snapshot = table_store.get_snapshot(auth_context.authorized_workspace_id, target_snapshot_id)
    if snapshot is None or snapshot.status == "tombstoned":
        return None

    active_versions = snapshot.source_version_map
    bound_artifacts = set(snapshot.artifact_ids)

    # 5. Verify source versions against both tombstones and pinned snapshot bindings
    for source_id, source_version in cached_result.dependent_source_versions:
        # Check tombstone store
        if table_store.has_tombstone(auth_context.authorized_workspace_id, "source", source_id):
            return None
        # Verify exact source version binding in pinned snapshot
        if active_versions.get(source_id) != source_version:
            return None

    # 6. Verify dependent artifacts against both tombstones and pinned snapshot bindings
    for artifact_id in cached_result.dependent_artifact_ids:
        # Check tombstone store
        if table_store.has_tombstone(auth_context.authorized_workspace_id, "artifact", artifact_id):
            return None
        # Verify artifact is bound in pinned snapshot
        if artifact_id not in bound_artifacts:
            return None

    return cached_result


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


def evaluate_query_delivery_and_recomputation(
    workspace_id: str,
    query_input: str | Any,
    cache_key_descriptor: CacheKeyDescriptor,
    auth_context: TrustedAuthContext,
    cache_store: Any,
    table_store: Any,
    execution_engine: Any | None = None,
) -> QueryExecutionOutcome:
    """Reconcile AC-8 deleted resource 404 with AC-12 multi source query recomputation."""
    # 1. Validate requested workspace, descriptor principal, and access policy against TrustedAuthContext
    if workspace_id != auth_context.authorized_workspace_id or cache_key_descriptor.workspace_id != workspace_id:
        raise WorkspaceAccessDeniedError(
            f"Caller {auth_context.principal_id} unauthorized for workspace {workspace_id}"
        )
    if cache_key_descriptor.principal_id != auth_context.principal_id:
        raise WorkspaceAccessDeniedError(
            f"Descriptor principal {cache_key_descriptor.principal_id} does not match caller {auth_context.principal_id}"
        )
    if cache_key_descriptor.access_policy_version != auth_context.access_policy_version:
        raise WorkspaceAccessDeniedError(
            f"Descriptor policy {cache_key_descriptor.access_policy_version} does not match caller policy {auth_context.access_policy_version}"
        )

    # 2. If the workspace itself is tombstoned, direct lookup and query return 404
    if table_store.has_tombstone(workspace_id, "workspace", workspace_id):
        raise FileNotFoundError("Workspace not found")

    engine = execution_engine or ControlledFixtureExecutionEngine()

    # 3. Retrieve cache entry using the full canonical cache key from CacheKeyDescriptor
    expected_cache_key = cache_key_descriptor.to_canonical_key()
    cached: CachedQueryResult | None = None
    try:
        if cache_store is not None:
            cached = cache_store.get_cached_query_by_key(expected_cache_key)
    except ConnectionError as err:
        logger.warning("Cache unreachable, falling back gracefully to authoritative store: %s", err)
        cached = None

    if cached is not None:
        # Validate that returned entry key strictly matches expected descriptor key
        if cached.cache_key == expected_cache_key:
            validated = delivery_gate_recheck(
                cached,
                auth_context,
                cache_key_descriptor.snapshot_id,
                table_store,
            )
            if validated is not None:
                return QueryExecutionOutcome(
                    execution_id=validated.execution_id,
                    status="SUCCESS",
                    answer=validated.answer,
                    citations=validated.citations,
                    surviving_source_ids=tuple(s[0] for s in validated.dependent_source_versions),
                    tombstoned_source_ids=(),
                )

    # 4. Recompute over surviving sources in the pinned snapshot, preserving original query input
    return engine.recompute_query(
        workspace_id=workspace_id,
        query=query_input,
        descriptor=cache_key_descriptor,
        table_store=table_store,
    )
