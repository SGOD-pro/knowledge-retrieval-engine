"""Vector + Reranker evidence retrieval strategy."""

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
from services.retrieval.reranker import rerank
from services.retrieval.subgoal_decomposer import QueryPlan
from services.telemetry import (
    RequestTelemetry,
    record_bedrock_embedding,
    record_reranker,
)

logger = logging.getLogger(__name__)


class VectorRerankStrategy:
    """Retrieval strategy combining dense vector embedding search with cross-encoder reranking."""

    name: str = "vector_rerank"

    def __init__(self, repository: CloudRepository | None = None) -> None:
        self.repository = repository or CloudRepository()

    async def retrieve(
        self,
        query: str,
        workspace_id: str,
        plan: QueryPlan | None = None,
        limits: RetrievalLimits | None = None,
        telemetry: RequestTelemetry | None = None,
    ) -> RetrievalStrategyResult:
        if not workspace_id:
            raise ValueError("workspace_id is required for VectorRerankStrategy")

        start_time = time.perf_counter()
        limits = limits or RetrievalLimits(top_k=10)
        top_k = limits.top_k

        # 1. Embed query
        emb_start_calls = telemetry.bedrock_embedding_calls if telemetry else 0
        rerank_start_calls = telemetry.reranker_remote_calls if telemetry else 0

        # Generate query embedding
        if hasattr(self.repository, "generate_query_embedding"):
            query_embedding = self.repository.generate_query_embedding(query)
        else:
            from providers.embedding_provider import embed_text
            from providers.provider_client import get_active_provider

            query_embedding = embed_text(query, provider=get_active_provider())
        record_bedrock_embedding()

        # 2. Vector search in Qdrant
        raw_results: list[tuple[Chunk, float]] = self.repository.search_vector(
            query_embedding=query_embedding,
            embedding_column="embedding_full",
            workspace_id=workspace_id,
            limit=max(top_k * 3, 20),
        )

        raw_candidates_trace = [
            {"chunk_id": str(c.id), "similarity_score": round(score, 4)}
            for c, score in raw_results
        ]

        if not raw_results:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            return RetrievalStrategyResult(
                strategy_name=self.name,
                query=query,
                items=[],
                confidence=0.0,
                latency_ms=latency_ms,
                remote_call_counts={
                    "bedrock_embedding_calls": max(1, (telemetry.bedrock_embedding_calls - emb_start_calls) if telemetry else 1),
                    "reranker_remote_calls": 0,
                },
                failure_reason="no_vector_candidates",
                debug_trace={
                    "raw_vector_candidates": [],
                    "reranked_candidates": [],
                    "final_retained_evidence_ids": [],
                },
            )

        candidate_chunks = [c for c, _ in raw_results]

        # 3. Rerank candidates
        reranked_chunks: list[Chunk] = rerank(query, candidate_chunks, top_k=top_k)
        record_reranker()

        reranked_trace = [
            {
                "chunk_id": str(c.id),
                "reranker_score": round(float(getattr(c, "reranker_score", 0.0) or 0.0), 4),
            }
            for c in reranked_chunks
        ]

        # 4. Convert retained chunks to EvidenceItem
        evidence_items: list[EvidenceItem] = []
        for c in reranked_chunks:
            ev = evidence_item_from_chunk(
                chunk=c,
                workspace_id=workspace_id,
                strategy=self.name,
                score=getattr(c, "reranker_score", None),
            )
            evidence_items.append(ev)

        retained_ids = [ev.evidence_id for ev in evidence_items]

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        avg_score = (
            sum(ev.score for ev in evidence_items) / len(evidence_items)
            if evidence_items
            else 0.0
        )
        confidence = max(0.0, min(1.0, float(avg_score)))

        emb_calls = (telemetry.bedrock_embedding_calls - emb_start_calls) if telemetry else 1
        reranker_calls = (telemetry.reranker_remote_calls - rerank_start_calls) if telemetry else 1

        return RetrievalStrategyResult(
            strategy_name=self.name,
            query=query,
            items=evidence_items,
            confidence=round(confidence, 4),
            latency_ms=round(latency_ms, 2),
            remote_call_counts={
                "bedrock_embedding_calls": max(1, emb_calls),
                "reranker_remote_calls": max(1, reranker_calls),
            },
            failure_reason=None if evidence_items else "no_evidence_retained",
            debug_trace={
                "raw_vector_candidates": raw_candidates_trace,
                "reranked_candidates": reranked_trace,
                "final_retained_evidence_ids": retained_ids,
            },
        )
