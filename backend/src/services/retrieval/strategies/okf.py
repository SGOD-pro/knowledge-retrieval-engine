"""OKF-compatible property and fact retrieval strategy."""

from __future__ import annotations

import logging
import time
from typing import Any

from db.database import CloudRepository
from services.retrieval.evidence_contract import (
    EvidenceItem,
    RetrievalLimits,
    RetrievalStrategyResult,
)
from services.retrieval.okf_retriever import OKFRetriever
from services.retrieval.planner import extract_entities
from services.retrieval.subgoal_decomposer import QueryPlan
from services.telemetry import RequestTelemetry

logger = logging.getLogger(__name__)


class OKFStrategy:
    """Retrieval strategy querying OKF-compatible runtime storage for canonical facts and properties.

    Note: Runtime storage in DynamoDB provides OKF-compatible runtime storage for facts
    and properties, but does not claim full OKF v0.2 archival compliance unless complete
    schema, metadata, provenance, relations, and lifecycle fields are verified.
    """

    name: str = "okf"

    def __init__(self, repository: CloudRepository | None = None) -> None:
        self.repository = repository or CloudRepository()
        self.retriever = OKFRetriever(self.repository)

    async def retrieve(
        self,
        query: str,
        workspace_id: str,
        plan: QueryPlan | None = None,
        limits: RetrievalLimits | None = None,
        telemetry: RequestTelemetry | None = None,
    ) -> RetrievalStrategyResult:
        if not workspace_id:
            raise ValueError("workspace_id is required for OKFStrategy")

        start_time = time.perf_counter()
        limits = limits or RetrievalLimits(top_k=10)

        # 1. Extract entities
        entities = extract_entities(query)

        # 2. Query OKF-compatible runtime storage
        properties: list[dict[str, Any]] = []
        if entities:
            properties = self.retriever.lookup(entities)

        evidence_items: list[EvidenceItem] = []
        for idx, prop in enumerate(properties[: limits.top_k]):
            entity = prop.get("entity", "")
            attribute = prop.get("attribute", "")
            value = str(prop.get("value", ""))
            sc_id = prop.get("source_chunk_id")
            conf = float(prop.get("confidence", 1.0))

            ev_id = f"okf_fact_{entity}_{attribute}_{idx}"
            text = f"{entity} {attribute}: {value}"

            item = EvidenceItem(
                evidence_id=ev_id,
                workspace_id=workspace_id,
                document_id="okf_runtime_store",
                document_version=None,
                source_type="okf_fact",
                locator={
                    "entity": entity,
                    "attribute": attribute,
                    "source_chunk_id": sc_id,
                },
                text=text,
                structured_payload={
                    "entity": entity,
                    "attribute": attribute,
                    "value": value,
                    "source_chunk_id": sc_id,
                },
                score=conf,
                strategy=self.name,
                provenance={
                    "storage_type": "OKF-compatible runtime storage",
                    "source_chunk_id": sc_id,
                    "raw_property": prop,
                },
                citation_payload={
                    "chunk_id": sc_id or ev_id,
                    "document_id": "okf_runtime_store",
                    "document_filename": "okf_knowledge_store",
                    "location_reference": f"OKF Fact: {entity} -> {attribute}",
                    "strategy": self.name,
                },
            )
            evidence_items.append(item)

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        return RetrievalStrategyResult(
            strategy_name=self.name,
            query=query,
            items=evidence_items,
            confidence=round(1.0 if evidence_items else 0.0, 4),
            latency_ms=round(latency_ms, 2),
            remote_call_counts={
                "bedrock_embedding_calls": 0,
                "reranker_remote_calls": 0,
                "generation_calls": 0,
            },
            failure_reason=None if evidence_items else "no_okf_properties",
            debug_trace={
                "storage_type": "OKF-compatible runtime storage",
                "extracted_entities": entities,
                "fact_count": len(evidence_items),
                "final_retained_evidence_ids": [ev.evidence_id for ev in evidence_items],
            },
        )
