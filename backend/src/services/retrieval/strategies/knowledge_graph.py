"""Authoritative Knowledge Graph evidence retrieval strategy.

Enforces:
- Scoped retrieval exclusively through workspace-partitioned storage (AC-11).
- Bounded multi-hop traversal with cycle suppression and targeted chunk hydration (AC-6, AC-9).
- Fast-path query bypass (0 model calls, 0ms, 0 tokens) (AC-10).
- Calibrated RRF candidate weights (1.10, halved to 0.55 on ambiguity/conflict) (AC-10).
- Caller deadline bounds and concurrency capacity retention (AC-8).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from config import settings
from schemas.models import Chunk
from services.retrieval.evidence_contract import (
    EvidenceItem,
    RetrievalLimits,
    RetrievalStrategyResult,
    evidence_item_from_chunk,
)
from services.retrieval.subgoal_decomposer import QueryPlan
from services.telemetry import RequestTelemetry

try:
    from src.db.knowledge_graph_repo import get_shared_knowledge_graph_repository
    from src.db.workspace_boundary_repo import get_workspace_boundary_repo
    from src.modules.documents.documents_repository import DocumentsRepository
    from src.services.retrieval.knowledge_graph_retriever import KnowledgeGraphRetriever
except ImportError:
    from db.knowledge_graph_repo import get_shared_knowledge_graph_repository
    from db.workspace_boundary_repo import get_workspace_boundary_repo
    from modules.documents.documents_repository import DocumentsRepository
    from services.retrieval.knowledge_graph_retriever import KnowledgeGraphRetriever

logger = logging.getLogger(__name__)


class KnowledgeGraphStrategy:
    """Retrieval strategy traversing workspace-partitioned entity graphs and resolving linked evidence."""

    name: str = "knowledge_graph"

    def __init__(
        self,
        retriever: KnowledgeGraphRetriever | None = None,
        repository: Any | None = None,
        boundary_repo: Any | None = None,
        documents_repo: DocumentsRepository | None = None,
    ) -> None:
        # Backward compatibility for legacy callers passing CloudRepository as repository
        if repository is not None and not hasattr(repository, "resolve_mentions"):
            if documents_repo is None and hasattr(repository, "get_chunks_by_ids"):
                documents_repo = repository
            repository = None

        self.repository = repository or get_shared_knowledge_graph_repository()
        self.boundary_repo = boundary_repo or get_workspace_boundary_repo()
        self.documents_repo = documents_repo or DocumentsRepository()
        self.retriever = retriever or KnowledgeGraphRetriever(
            repository=self.repository,
            boundary_repo=self.boundary_repo,
            documents_repo=self.documents_repo,
        )

    async def retrieve(
        self,
        query: str,
        workspace_id: str,
        plan: QueryPlan | None = None,
        limits: RetrievalLimits | None = None,
        telemetry: RequestTelemetry | None = None,
        snapshot_context: Any | None = None,
    ) -> RetrievalStrategyResult:
        if not workspace_id:
            raise ValueError("workspace_id is required for KnowledgeGraphStrategy")

        start_time = time.perf_counter()

        # 1. Check feature toggle
        if not getattr(settings, "GRAPH_ENABLED", False):
            return RetrievalStrategyResult(
                strategy_name=self.name,
                query=query,
                items=[],
                confidence=0.0,
                latency_ms=0.0,
                remote_call_counts={
                    "bedrock_embedding_calls": 0,
                    "reranker_remote_calls": 0,
                    "generation_calls": 0,
                },
                failure_reason="graph_disabled",
                debug_trace={"status": "disabled"},
            )

        # 2. Fast-path query bypass (AC-10)
        if plan and getattr(plan, "fast_path", False):
            logger.info("knowledge_graph_strategy.fast_path_bypass query=%s", query[:50])
            return RetrievalStrategyResult(
                strategy_name=self.name,
                query=query,
                items=[],
                confidence=0.0,
                latency_ms=0.0,
                remote_call_counts={
                    "bedrock_embedding_calls": 0,
                    "reranker_remote_calls": 0,
                    "generation_calls": 0,
                },
                failure_reason="fast_path_bypassed",
                debug_trace={"status": "fast_path_bypassed"},
            )

        limits = limits or RetrievalLimits(max_hops=2, top_k=10)
        max_hops = limits.max_hops or getattr(settings, "GRAPH_MAX_HOPS", 2)
        timeout_ms = getattr(settings, "GRAPH_TIMEOUT_MS", 75)

        # 3. Execute bounded multi-hop expansion with caller deadline
        lookup_res = self.retriever.expand(
            workspace_id=workspace_id,
            query=query,
            snapshot_context=snapshot_context,
            max_hops=max_hops,
            caller_deadline_ms=float(timeout_ms),
        )

        # 4. Calibrate RRF score (AC-10)
        base_weight = float(getattr(settings, "GRAPH_FUSION_WEIGHT", 1.10))
        is_ambiguous_or_conflicting = (
            lookup_res.entity_state == "ambiguous"
            or lookup_res.relationship_state == "conflicting"
        )
        ambiguous_weight = float(getattr(settings, "GRAPH_AMBIGUOUS_FUSION_WEIGHT", 0.55))
        calibrated_weight = ambiguous_weight if is_ambiguous_or_conflicting else base_weight

        evidence_items: list[EvidenceItem] = []
        linked_chunk_ids: list[str] = []

        for c in lookup_res.candidate_chunks[: limits.top_k]:
            cid = str(getattr(c, "id", ""))
            linked_chunk_ids.append(cid)
            ev = evidence_item_from_chunk(
                chunk=c,
                workspace_id=workspace_id,
                strategy=self.name,
                score=calibrated_weight,
            )
            ev.provenance["graph"] = {
                "seed_entities": list(lookup_res.seed_entities),
                "max_hops": max_hops,
                "traversed_nodes": lookup_res.traversed_node_count,
                "matched_edges": lookup_res.edge_match_count,
                "entity_state": lookup_res.entity_state,
                "relationship_state": lookup_res.relationship_state,
                "calibrated_weight": calibrated_weight,
            }
            evidence_items.append(ev)

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        graph_confidence = calibrated_weight if evidence_items else 0.0

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
            failure_reason=None if evidence_items else ("no_graph_evidence" if lookup_res.execution_status == "completed" else lookup_res.execution_status),
            debug_trace={
                "seed_entities": list(lookup_res.seed_entities),
                "expansion_depth": max_hops,
                "linked_evidence_chunks": linked_chunk_ids,
                "graph_confidence": graph_confidence,
                "calibrated_weight": calibrated_weight,
                "traversed_nodes": lookup_res.traversed_node_count,
                "matched_edges": lookup_res.edge_match_count,
                "entity_state": lookup_res.entity_state,
                "relationship_state": lookup_res.relationship_state,
                "was_truncated": lookup_res.was_truncated,
                "truncation_reason": lookup_res.truncation_reason,
                "execution_status": lookup_res.execution_status,
                "final_retained_evidence_ids": [ev.evidence_id for ev in evidence_items],
            },
        )
