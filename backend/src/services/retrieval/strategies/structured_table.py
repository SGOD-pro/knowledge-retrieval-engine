"""Structured Table evidence retrieval strategy."""

from __future__ import annotations

import logging
import time
from typing import Any

from db.table_store import get_shared_table_store
from db.table_store.base import TableStore
from services.retrieval.evidence_contract import (
    EvidenceItem,
    RetrievalLimits,
    RetrievalStrategyResult,
)
from services.retrieval.structured_query_service import (
    AmbiguousBindingError,
    IncompleteDataError,
    StorageFailureError,
    StructuredQueryResult,
    StructuredQueryService,
    UnsupportedQueryError,
)
from services.retrieval.subgoal_decomposer import QueryPlan
from services.telemetry import RequestTelemetry

logger = logging.getLogger(__name__)


class StructuredTableStrategy:
    """Deterministic structured table retrieval strategy against TableStore.

    Uses zero LLM calls, zero embedding calls, and operates directly over complete
    persisted table rows. Fails closed on incomplete coverage, ambiguous binding,
    or storage failures.
    """

    name: str = "structured_table"

    def __init__(
        self,
        store: TableStore | None = None,
        service: StructuredQueryService | None = None,
    ) -> None:
        self.store = store or get_shared_table_store()
        self.service = service or StructuredQueryService(self.store)

    async def retrieve(
        self,
        query: str,
        workspace_id: str,
        plan: QueryPlan | None = None,
        limits: RetrievalLimits | None = None,
        telemetry: RequestTelemetry | None = None,
    ) -> RetrievalStrategyResult:
        if not workspace_id:
            raise ValueError("workspace_id is required for StructuredTableStrategy")

        start_time = time.perf_counter()

        zero_remote_calls = {
            "bedrock_embedding_calls": 0,
            "reranker_remote_calls": 0,
            "generation_calls": 0,
            "retrieval_llm_calls": 0,
        }

        try:
            result: StructuredQueryResult | None = self.service.execute(
                query=query,
                workspace_id=workspace_id,
            )
        except IncompleteDataError as e:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            return RetrievalStrategyResult(
                strategy_name=self.name,
                query=query,
                items=[],
                confidence=0.0,
                latency_ms=round(latency_ms, 2),
                remote_call_counts=zero_remote_calls,
                failure_reason="incomplete_table_coverage",
                debug_trace={"error": str(e), "table_id": e.table_id, "manifest": getattr(e, "manifest", {})},
            )
        except AmbiguousBindingError as e:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            return RetrievalStrategyResult(
                strategy_name=self.name,
                query=query,
                items=[],
                confidence=0.0,
                latency_ms=round(latency_ms, 2),
                remote_call_counts=zero_remote_calls,
                failure_reason="ambiguous_binding",
                debug_trace={"error": str(e)},
            )
        except StorageFailureError as e:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            return RetrievalStrategyResult(
                strategy_name=self.name,
                query=query,
                items=[],
                confidence=0.0,
                latency_ms=round(latency_ms, 2),
                remote_call_counts=zero_remote_calls,
                failure_reason="storage_failure",
                debug_trace={"error": str(e)},
            )
        except UnsupportedQueryError as e:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            return RetrievalStrategyResult(
                strategy_name=self.name,
                query=query,
                items=[],
                confidence=0.0,
                latency_ms=round(latency_ms, 2),
                remote_call_counts=zero_remote_calls,
                failure_reason="unsupported_query",
                debug_trace={"error": str(e)},
            )
        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            return RetrievalStrategyResult(
                strategy_name=self.name,
                query=query,
                items=[],
                confidence=0.0,
                latency_ms=round(latency_ms, 2),
                remote_call_counts=zero_remote_calls,
                failure_reason=f"execution_error: {e}",
                debug_trace={"error": str(e)},
            )

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        if result is None:
            return RetrievalStrategyResult(
                strategy_name=self.name,
                query=query,
                items=[],
                confidence=0.0,
                latency_ms=round(latency_ms, 2),
                remote_call_counts=zero_remote_calls,
                failure_reason="no_table_match",
                debug_trace={},
            )

        # Check for multiple rows match when exactly one was required
        if result.operator == "exact_row" and result.selection_count > 1:
            return RetrievalStrategyResult(
                strategy_name=self.name,
                query=query,
                items=[],
                confidence=0.0,
                latency_ms=round(latency_ms, 2),
                remote_call_counts=zero_remote_calls,
                failure_reason="multiple_rows_matched",
                debug_trace={"selection_count": result.selection_count},
            )

        ev_id = f"table_{result.table_id}_{result.selection_hash}"
        locator = {
            "table_id": result.table_id,
            "selection_hash": result.selection_hash,
            "row_ids": result.row_ids,
            "selection_count": result.selection_count,
            "total_rows_in_table": result.total_rows_in_table,
            "location_reference": f"{result.selection_count} row(s) from {result.table_id}",
        }

        structured_payload = {
            "table_id": result.table_id,
            "document_version": result.document_version,
            "operator": result.operator,
            "target_column": result.target_column,
            "predicate_columns": result.predicate_columns,
            "predicate_values": [str(v) for v in result.predicate_values],
            "selection_count": result.selection_count,
            "total_rows_in_table": result.total_rows_in_table,
            "selection_hash": result.selection_hash,
            "result_value": str(result.result_value),
            "secondary_value": str(result.secondary_value) if result.secondary_value is not None else None,
            "unit": result.unit,
        }

        citation_payload = {
            "evidence_type": "structured_aggregate",
            "table_id": result.table_id,
            "document_id": result.document_id,
            "document_version": result.document_version,
            "operator": result.operator,
            "target_column": result.target_column,
            "selection_hash": result.selection_hash,
            "selection_count": result.selection_count,
            "total_rows_in_table": result.total_rows_in_table,
            "location_reference": locator["location_reference"],
            "strategy": self.name,
        }

        item = EvidenceItem(
            evidence_id=ev_id,
            workspace_id=workspace_id,
            document_id=result.document_id,
            document_version=result.document_version,
            source_type="table_row_set",
            locator=locator,
            text=result.answer_text,
            structured_payload=structured_payload,
            score=result.overall_confidence,
            strategy=self.name,
            provenance={
                "table_id": result.table_id,
                "row_indices": result.row_indices,
                "document_version": result.document_version,
            },
            citation_payload=citation_payload,
        )

        return RetrievalStrategyResult(
            strategy_name=self.name,
            query=query,
            items=[item],
            confidence=round(result.overall_confidence, 4),
            latency_ms=round(latency_ms, 2),
            remote_call_counts=zero_remote_calls,
            failure_reason=None,
            debug_trace={
                "operator": result.operator,
                "table_id": result.table_id,
                "target_column": result.target_column,
                "selection_count": result.selection_count,
                "selection_hash": result.selection_hash,
                "final_retained_evidence_ids": [ev_id],
            },
        )
