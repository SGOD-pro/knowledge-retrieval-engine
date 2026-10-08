"""Vector search boundary adapter and pre dispatch filter validation.

Enforces mandatory tenant workspace payload filtering and pre dispatch checks
before calling Qdrant, filtering candidate matches against authoritative tombstones.
"""

from typing import Any
from src.schemas.contracts.boundary import (
    QdrantFilterContract,
    WorkspaceAccessDeniedError,
)
from src.schemas.contracts.envelope import TrustedAuthContext


def dispatch_qdrant_search(
    query_vector: list[float],
    filter_contract: QdrantFilterContract,
    auth_context: TrustedAuthContext,
    client: Any,
    table_store: Any | None = None,
) -> list[dict[str, Any]]:
    """Dispatch vector search to Qdrant with pre dispatch filter validation.
    
    Validates and rejects missing or mismatched workspace filters before dispatch.
    Filters candidate matches against authoritative tombstones.
    """
    # Pre dispatch validation: rejects missing or mismatched filters before calling vector store
    payload_filter = filter_contract.validate_and_build_search_filter(auth_context.authorized_workspace_id)

    raw_results = client.search(
        collection_name="kre_vectors",
        query_vector=query_vector,
        query_filter=payload_filter,
    )

    if table_store is None:
        return raw_results

    # Tombstone barrier: discard candidate matches belonging to tombstoned sources
    surviving_matches = []
    for match in raw_results:
        source_id = match.get("payload", {}).get("source_id")
        if source_id and table_store.has_tombstone(auth_context.authorized_workspace_id, "source", source_id):
            continue
        surviving_matches.append(match)

    return surviving_matches


class MockQdrantClient:
    """Mock Qdrant client for boundary isolation testing."""

    def __init__(self) -> None:
        self.points: list[dict[str, Any]] = []

    def add_point(self, point_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        self.points.append({"id": point_id, "vector": vector, "payload": payload})

    def search(
        self,
        collection_name: str,
        query_vector: list[float],
        query_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not query_filter:
            # Unfiltered query returns points only if no tenant boundary was specified
            return self.points

        must_conditions = query_filter.get("must", [])
        matched = []
        for pt in self.points:
            pt_payload = pt.get("payload", {})
            matches_all = True
            for cond in must_conditions:
                key = cond.get("key")
                expected = cond.get("match", {}).get("value")
                if pt_payload.get(key) != expected:
                    matches_all = False
                    break
            if matches_all:
                matched.append(pt)
        return matched
