"""BM25 Evidence Provider emitting standardized EvidenceEnvelope records."""

import logging
from typing import Any

from db.database import CloudRepository
from schemas.evidence import (
    EvidenceConfidence,
    EvidenceEnvelope,
    EvidenceType,
    TextEvidence,
)
from services.retrieval.bm25_retriever import BM25Retriever
from services.retrieval.evidence_providers.base import (
    CriticalStorageFailure,
    EvidenceProvider,
)

logger = logging.getLogger(__name__)


class BM25EvidenceProvider:
    provider_name = "bm25"

    def __init__(self, repository: CloudRepository | None = None) -> None:
        self.repo = repository or CloudRepository()
        self.retriever = BM25Retriever()

    def retrieve(
        self,
        query: str,
        workspace_id: str,
        top_k: int = 20,
        **kwargs: Any,
    ) -> list[EvidenceEnvelope]:
        if not workspace_id:
            raise CriticalStorageFailure("workspace_id must be provided for BM25 retrieval")

        try:
            all_chunks = self.repo.get_all_chunks(workspace_id=workspace_id)
        except Exception as exc:
            raise CriticalStorageFailure(f"BM25 repository lookup failed: {exc}") from exc

        if not all_chunks:
            return []

        results = self.retriever.search(query, all_chunks, top_k=top_k)
        envelopes: list[EvidenceEnvelope] = []

        for chunk, score in results:
            payload = TextEvidence(
                text=chunk.text,
                element_type=chunk.element_type,
                section_path=chunk.section_path,
                page_number=chunk.page_number,
                bounding_box=chunk.bounding_box,
            )
            env = EvidenceEnvelope(
                evidence_id=f"bm25:{chunk.id}",
                evidence_type=EvidenceType.TEXT,
                document_id=chunk.document_id,
                location={
                    "page_number": chunk.page_number,
                    "location_reference": chunk.location_reference,
                },
                provider=self.provider_name,
                provider_score=float(score),
                payload=payload,
                confidence=EvidenceConfidence(retrieval_confidence=min(1.0, float(score) / 10.0)),
                citation_text=chunk.text[:200],
                workspace_id=workspace_id,
            )
            envelopes.append(env)

        return envelopes
