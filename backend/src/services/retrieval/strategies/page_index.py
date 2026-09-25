"""PageIndex structural evidence retrieval strategy."""

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
from services.retrieval.page_index_retriever import PageIndexRetriever
from services.retrieval.subgoal_decomposer import QueryPlan
from services.telemetry import RequestTelemetry

logger = logging.getLogger(__name__)


class PageIndexStrategy:
    """Retrieval strategy utilizing page index and document layout hierarchy.

    Does not use LLM generative reasoning during default structural retrieval,
    but explicitly tracks and reports retrieval_llm_calls.
    """

    name: str = "page_index"

    def __init__(self, repository: CloudRepository | None = None) -> None:
        self.repository = repository or CloudRepository()
        self.retriever = PageIndexRetriever()

    async def retrieve(
        self,
        query: str,
        workspace_id: str,
        plan: QueryPlan | None = None,
        limits: RetrievalLimits | None = None,
        telemetry: RequestTelemetry | None = None,
    ) -> RetrievalStrategyResult:
        if not workspace_id:
            raise ValueError("workspace_id is required for PageIndexStrategy")

        start_time = time.perf_counter()
        limits = limits or RetrievalLimits(top_k=10)
        top_k = limits.top_k

        # 1. Fetch chunks in workspace
        all_chunks = self.repository.get_all_chunks(workspace_id=workspace_id)

        # 2. PageIndex filter and structural score
        selected_chunks, pages, chunk_ids = self.retriever.filter_and_rank(
            query=query,
            candidates=all_chunks,
            top_k=top_k,
        )

        evidence_items: list[EvidenceItem] = []
        for c in selected_chunks:
            source_type = "page_region" if c.page_number is not None else "chunk"
            ev = evidence_item_from_chunk(
                chunk=c,
                workspace_id=workspace_id,
                strategy=self.name,
                score=getattr(c, "structural_weight", 1.0) or 1.0,
            )
            # Override source_type to page_region if page is present
            ev.source_type = source_type
            ev.locator["candidate_pages"] = pages
            evidence_items.append(ev)

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        return RetrievalStrategyResult(
            strategy_name=self.name,
            query=query,
            items=evidence_items,
            confidence=round(1.0 if evidence_items else 0.0, 4),
            latency_ms=round(latency_ms, 2),
            remote_call_counts={
                "retrieval_llm_calls": 0,
                "bedrock_embedding_calls": 0,
                "reranker_remote_calls": 0,
                "generation_calls": 0,
            },
            failure_reason=None if evidence_items else "no_page_matches",
            debug_trace={
                "retrieval_llm_calls_used": False,
                "candidate_pages": pages,
                "candidate_chunk_ids": chunk_ids,
                "final_retained_evidence_ids": [ev.evidence_id for ev in evidence_items],
            },
        )
