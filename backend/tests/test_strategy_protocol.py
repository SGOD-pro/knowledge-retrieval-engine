"""Tests for RetrievalStrategy protocol compliance."""

import pytest
from services.retrieval.strategies.base import RetrievalStrategy
from services.retrieval.evidence_contract import (
    RetrievalLimits,
    RetrievalStrategyResult,
)
from services.telemetry import RequestTelemetry


class DummyStrategy:
    name: str = "dummy"

    async def retrieve(
        self,
        query: str,
        workspace_id: str,
        plan=None,
        limits=None,
        telemetry=None,
    ) -> RetrievalStrategyResult:
        return RetrievalStrategyResult(
            strategy_name=self.name,
            query=query,
            items=[],
            confidence=1.0,
            latency_ms=1.0,
            remote_call_counts={},
            failure_reason=None,
            debug_trace={},
        )


def test_dummy_strategy_conforms_to_protocol():
    s = DummyStrategy()
    assert isinstance(s, RetrievalStrategy)
    assert s.name == "dummy"
