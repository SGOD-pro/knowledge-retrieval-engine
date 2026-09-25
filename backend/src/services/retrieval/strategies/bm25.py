"""Lexical BM25 evidence retrieval strategy."""

from __future__ import annotations

from collections import Counter
import logging
import time
from typing import Any

from db.database import CloudRepository
from schemas.models import Chunk
from services.retrieval.bm25_retriever import BM25Retriever, _tokenize
from services.retrieval.evidence_contract import (
    EvidenceItem,
    RetrievalLimits,
    RetrievalStrategyResult,
    evidence_item_from_chunk,
)
from services.retrieval.subgoal_decomposer import QueryPlan
from services.telemetry import RequestTelemetry

logger = logging.getLogger(__name__)


class LexicalBM25Strategy:
    """Pure lexical BM25 retrieval strategy with zero LLM and zero remote calls."""

    name: str = "bm25"

    def __init__(self, repository: CloudRepository | None = None) -> None:
        self.repository = repository or CloudRepository()
        self.retriever = BM25Retriever()

    async def retrieve(
        self,
        query: str,
        workspace_id: str,
        plan: QueryPlan | None = None,
        limits: RetrievalLimits | None = None,
        telemetry: RequestTelemetry | None = None,
    ) -> RetrievalStrategyResult:
        if not workspace_id:
            raise ValueError("workspace_id is required for LexicalBM25Strategy")

        start_time = time.perf_counter()
        limits = limits or RetrievalLimits(top_k=10)
        top_k = limits.top_k

        # 1. Fetch workspace chunks (pure local / db scan, no LLM or remote embeddings)
        all_chunks = self.repository.get_all_chunks(workspace_id=workspace_id)

        # 2. Tokenize query for term match tracking
        query_tokens = set(_tokenize(query))

        # 3. Perform BM25 ranking
        scored_results: list[tuple[Chunk, float]] = self.retriever.search(
            query=query,
            chunks=all_chunks,
            top_k=top_k,
        )

        matched_terms: set[str] = set()
        bm25_scores: dict[str, float] = {}
        doc_ids: list[str] = []
        evidence_items: list[EvidenceItem] = []

        for c, score in scored_results:
            cid = str(c.id)
            bm25_scores[cid] = round(float(score), 4)
            did = str(getattr(c, "document_id", ""))
            if did:
                doc_ids.append(did)

            # Check overlap terms
            chunk_tokens = set(_tokenize(c.text))
            matched_terms |= (query_tokens & chunk_tokens)

            ev = evidence_item_from_chunk(
                chunk=c,
                workspace_id=workspace_id,
                strategy=self.name,
                score=score,
            )
            evidence_items.append(ev)

        doc_distribution = dict(Counter(doc_ids))
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        max_score = scored_results[0][1] if scored_results else 0.0
        confidence = min(1.0, max_score / 10.0) if max_score > 0 else 0.0

        return RetrievalStrategyResult(
            strategy_name=self.name,
            query=query,
            items=evidence_items,
            confidence=round(confidence, 4),
            latency_ms=round(latency_ms, 2),
            remote_call_counts={
                "bedrock_embedding_calls": 0,
                "reranker_remote_calls": 0,
                "generation_calls": 0,
            },
            failure_reason=None if evidence_items else "no_lexical_matches",
            debug_trace={
                "matched_terms": sorted(list(matched_terms)),
                "bm25_scores": bm25_scores,
                "document_distribution": doc_distribution,
                "final_retained_evidence_ids": [ev.evidence_id for ev in evidence_items],
            },
        )
