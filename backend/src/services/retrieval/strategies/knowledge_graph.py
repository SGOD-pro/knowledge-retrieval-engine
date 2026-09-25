"""Knowledge Graph evidence retrieval strategy."""

from __future__ import annotations

import logging
import time
from typing import Any

from db.database import CloudRepository
from schemas.models import Chunk
from services.retrieval.evidence_contract import (
    EvidenceItem,
    RetrievalLimits,
    RetrievalStrategyResult,
    evidence_item_from_chunk,
)
from services.retrieval.graph_retriever import GraphRetriever
from services.retrieval.planner import extract_entities
from services.retrieval.subgoal_decomposer import QueryPlan
from services.telemetry import RequestTelemetry

logger = logging.getLogger(__name__)


class KnowledgeGraphStrategy:
    """Retrieval strategy traversing entity-relationship graphs and resolving linked evidence."""

    name: str = "knowledge_graph"

    def __init__(self, repository: CloudRepository | None = None) -> None:
        self.repository = repository or CloudRepository()
        self.retriever = GraphRetriever(self.repository)

    async def retrieve(
        self,
        query: str,
        workspace_id: str,
        plan: QueryPlan | None = None,
        limits: RetrievalLimits | None = None,
        telemetry: RequestTelemetry | None = None,
    ) -> RetrievalStrategyResult:
        if not workspace_id:
            raise ValueError("workspace_id is required for KnowledgeGraphStrategy")

        start_time = time.perf_counter()
        limits = limits or RetrievalLimits(max_hops=2, top_k=10)
        max_hops = limits.max_hops

        # 1. Extract seed entities
        seed_entities = extract_entities(query)

        # 2. Graph expansion
        raw_graph_results: list[dict[str, Any]] = []
        if seed_entities:
            raw_graph_results = self.retriever.expand(seed_entities, max_hops=max_hops)

        # 3. Resolve linked evidence chunks
        chunk_ids_to_fetch = set()
        for item in raw_graph_results:
            cid = item.get("source_chunk_id") or item.get("chunk_id")
            if cid:
                chunk_ids_to_fetch.add(str(cid))

        # Check OKF properties if available for additional source chunk links
        if seed_entities and hasattr(self.repository, "get_okf_properties"):
            try:
                okf_props = self.repository.get_okf_properties(seed_entities)
                for p in okf_props:
                    sc = p.get("source_chunk_id")
                    if sc:
                        chunk_ids_to_fetch.add(str(sc))
            except Exception:
                pass

        all_ws_chunks = self.repository.get_all_chunks(workspace_id=workspace_id)
        ws_by_id = {str(c.id): c for c in all_ws_chunks}

        evidence_items: list[EvidenceItem] = []
        linked_chunk_ids: list[str] = []

        for cid in chunk_ids_to_fetch:
            if cid in ws_by_id:
                c = ws_by_id[cid]
                linked_chunk_ids.append(cid)
                ev = evidence_item_from_chunk(
                    chunk=c,
                    workspace_id=workspace_id,
                    strategy=self.name,
                    score=1.0,
                )
                ev.provenance["graph"] = {
                    "seed_entities": seed_entities,
                    "max_hops": max_hops,
                }
                evidence_items.append(ev)

        # If no linked chunks found but graph edges exist, emit kg_edge items
        if not evidence_items and raw_graph_results:
            for idx, edge in enumerate(raw_graph_results[: limits.top_k]):
                edge_id = f"kg_edge_{idx}_{edge.get('source_entity', '')}_{edge.get('target_entity', '')}"
                evidence_items.append(
                    EvidenceItem(
                        evidence_id=edge_id,
                        workspace_id=workspace_id,
                        document_id="graph_store",
                        document_version=None,
                        source_type="kg_edge",
                        locator=edge,
                        text=f"{edge.get('source_entity')} {edge.get('relation')} {edge.get('target_entity')}",
                        structured_payload=edge,
                        score=1.0,
                        strategy=self.name,
                        provenance={"seed_entities": seed_entities, "graph_edge": edge},
                        citation_payload={
                            "chunk_id": edge_id,
                            "document_id": "graph_store",
                            "document_filename": "knowledge_graph",
                            "location_reference": f"Edge: {edge.get('source_entity')} -> {edge.get('target_entity')}",
                            "strategy": self.name,
                        },
                    )
                )

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        graph_confidence = 1.0 if evidence_items else 0.0

        return RetrievalStrategyResult(
            strategy_name=self.name,
            query=query,
            items=evidence_items,
            confidence=graph_confidence,
            latency_ms=round(latency_ms, 2),
            remote_call_counts={
                "bedrock_embedding_calls": 0,
                "reranker_remote_calls": 0,
                "generation_calls": 0,
            },
            failure_reason=None if evidence_items else "no_graph_evidence",
            debug_trace={
                "seed_entities": seed_entities,
                "expansion_depth": max_hops,
                "linked_evidence_chunks": linked_chunk_ids,
                "graph_confidence": graph_confidence,
                "final_retained_evidence_ids": [ev.evidence_id for ev in evidence_items],
            },
        )
