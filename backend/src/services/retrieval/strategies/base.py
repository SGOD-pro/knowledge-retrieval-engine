"""Base protocol and definitions for Evidence Retrieval Strategies."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from services.retrieval.evidence_contract import (
    EvidenceItem,
    RetrievalLimits,
    RetrievalStrategyResult,
)
from services.retrieval.subgoal_decomposer import QueryPlan
from services.telemetry import RequestTelemetry


@runtime_checkable
class RetrievalStrategy(Protocol):
    """Common interface for all evidence retrieval strategies.

    Hard rules:
    1. No strategy may call answer generation.
    2. Each strategy only returns evidence.
    3. Workspace isolation is enforced on every call.
    4. Missing evidence must be visible as a strategy failure or empty items,
       not hidden by later fallback.
    """

    name: str

    async def retrieve(
        self,
        query: str,
        workspace_id: str,
        plan: QueryPlan | None = None,
        limits: RetrievalLimits | None = None,
        telemetry: RequestTelemetry | None = None,
    ) -> RetrievalStrategyResult:
        ...
