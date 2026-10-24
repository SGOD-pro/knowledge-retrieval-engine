"""PageIndex structural evidence retrieval strategy."""

from __future__ import annotations

import logging
import time
from typing import Any

from config import settings
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

    def __init__(
        self,
        repository: CloudRepository | None = None,
        retriever: PageIndexRetriever | None = None,
    ) -> None:
        self.repository = repository or CloudRepository()
        self.retriever = retriever or PageIndexRetriever()

    async def retrieve(
        self,
        query: str,
        workspace_id: str,
        plan: QueryPlan | None = None,
        limits: RetrievalLimits | None = None,
        telemetry: RequestTelemetry | None = None,
        document_ids: list[str] | None = None,
    ) -> RetrievalStrategyResult:
        if not workspace_id:
            raise ValueError("workspace_id is required for PageIndexStrategy")

        start_time = time.perf_counter()
        limits = limits or RetrievalLimits(top_k=10)
        top_k = limits.top_k

        # 1. Extract structural target labels from query
        target_labels = self.retriever.lookup_service.extract_target_labels(query)

        evidence_items: list[EvidenceItem] = []
        candidate_pages: list[int] = []
        chunk_ids: list[str] = []

        if target_labels:
            from src.db.workspace_boundary_repo import get_workspace_boundary_repo
            b_repo = get_workspace_boundary_repo()
            snaps = b_repo.list_document_snapshots(workspace_id)
            pdf_snaps = [s for s in snaps if s.format.lower() == "pdf"]
            if document_ids:
                doc_set = set(str(d) for d in document_ids)
                pdf_snaps = [s for s in pdf_snaps if s.document_id in doc_set]

            for snap in pdf_snaps:
                doc_id = snap.document_id
                doc_ver = snap.version
                for t_label in target_labels:
                    res, status = await self.retriever.lookup(
                        workspace_id=workspace_id,
                        document_id=doc_id,
                        document_version=doc_ver,
                        target_label=t_label,
                        deadline_ms=50.0,
                    )
                    if res and (res.candidate_pages or res.cross_reference_pages):
                        matched_p = res.candidate_pages if res.candidate_pages else res.cross_reference_pages
                        candidate_pages.extend(matched_p)
                        doc_chunks = self.repository.get_all_chunks(
                            document_ids=[doc_id],
                            workspace_id=workspace_id,
                            page_numbers=matched_p,
                        )
                        matching_chunks = [c for c in doc_chunks if c.page_number in matched_p]
                        for c in matching_chunks[:top_k]:
                            source_type = "page_region" if c.page_number is not None else "chunk"
                            ev = evidence_item_from_chunk(
                                chunk=c,
                                workspace_id=workspace_id,
                                strategy=self.name,
                                score=getattr(settings, "PAGEINDEX_STRUCTURAL_WEIGHT", 1.1),
                            )
                            ev.source_type = source_type
                            ev.locator["candidate_pages"] = list(matched_p)
                            evidence_items.append(ev)
                            chunk_ids.append(str(c.id))

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
                "candidate_pages": candidate_pages,
                "candidate_chunk_ids": chunk_ids,
                "final_retained_evidence_ids": [ev.evidence_id for ev in evidence_items],
            },
        )
