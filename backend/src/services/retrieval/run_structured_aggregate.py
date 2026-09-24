"""Pipeline node for deterministic structured aggregate queries."""

import hashlib
import logging
import time
from typing import Any

from db.table_store import get_shared_table_store
from services.retrieval.structured_query_service import (
    AmbiguousBindingError,
    IncompleteDataError,
    StorageFailureError,
    StructuredQueryResult,
    StructuredQueryService,
    UnsupportedQueryError,
)
from services.telemetry import record_structured_aggregate

logger = logging.getLogger(__name__)


def build_structured_evidence_ref(result: StructuredQueryResult, workspace_id: str) -> dict[str, Any]:
    """Build a verifiable structured-evidence reference containing typed predicates and row digest."""
    return {
        "evidence_type": "structured_aggregate",
        "document_id": result.document_id,
        "document_version": result.document_version,
        "table_id": result.table_id,
        "operator": result.operator,
        "target_column": result.target_column,
        "predicate_columns": result.predicate_columns,
        "predicate_values": [str(v) for v in result.predicate_values],
        "selection_count": result.selection_count,
        "total_rows_in_table": result.total_rows_in_table,
        "selection_hash": result.selection_hash,
        "result_value": str(result.result_value),
        "secondary_value": str(result.secondary_value) if result.secondary_value is not None else None,
        "schema_binding_confidence": result.schema_binding_confidence,
        "location_reference": f"{result.selection_count} row(s) from {result.table_id}",
        "text": result.answer_text,
        "text_snippet": result.answer_text[:200],
    }


def execute(state: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    workspace_id = state["workspace_id"]
    query = state["query"]
    document_ids = state.get("document_ids")

    store = get_shared_table_store()
    svc = StructuredQueryService(store)
    existing_timings = state.get("stage_timings", {})

    try:
        result = svc.execute(query, workspace_id, document_ids=document_ids)
    except IncompleteDataError as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.error("structured_aggregate.incomplete_data table=%s manifest=%s", e.table_id, e.manifest)
        return {
            "final_answer": "NOT_FOUND",
            "executed_path": "structured_aggregate_incomplete",
            "status": "error",
            "error_code": "incomplete_data",
            "error": f"Table data is incomplete: {e}",
            "stage_timings": {**existing_timings, "structured_aggregate_ms": latency_ms},
        }
    except AmbiguousBindingError as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.warning("structured_aggregate.ambiguous_binding query='%s' error=%s", query[:60], e)
        return {
            "final_answer": f"Clarification needed: {e}",
            "executed_path": "structured_aggregate_ambiguous",
            "status": "clarification_needed",
            "error_code": "ambiguous_binding",
            "error": str(e),
            "stage_timings": {**existing_timings, "structured_aggregate_ms": latency_ms},
        }
    except StorageFailureError as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.error("structured_aggregate.storage_failure query='%s' error=%s", query[:60], e)
        return {
            "final_answer": "ERROR",
            "executed_path": "structured_aggregate_storage_failure",
            "status": "error",
            "error_code": "storage_failure",
            "error": str(e),
            "stage_timings": {**existing_timings, "structured_aggregate_ms": latency_ms},
        }
    except UnsupportedQueryError as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.info("structured_aggregate.unsupported query='%s' reason=%s", query[:60], e)
        return {
            "executed_path": "structured_aggregate_fallback",
            "stage_timings": {**existing_timings, "structured_aggregate_ms": latency_ms},
        }

    if result is None:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "executed_path": "structured_aggregate_fallback",
            "stage_timings": {**existing_timings, "structured_aggregate_ms": latency_ms},
        }

    latency_ms = (time.perf_counter() - t0) * 1000.0
    record_structured_aggregate()

    citation = build_structured_evidence_ref(result, workspace_id)
    return {
        "final_answer": result.answer_text,
        "citations": [citation],
        "confidence_score": result.overall_confidence,
        "faithfulness": None,
        "citation_utilization_rate": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "execution_result": result,
        "executed_path": "structured_aggregate",
        "status": "success",
        "retrieval_candidates": {"structured_rows": result.row_ids},
        "stage_timings": {**existing_timings, "structured_aggregate_ms": latency_ms},
    }
