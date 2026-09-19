"""Dense Vector Evidence Provider emitting standardized EvidenceEnvelope records."""

import logging
from typing import Any

from db.database import CloudRepository
from schemas.evidence import (
    EvidenceConfidence,
    EvidenceEnvelope,
    EvidenceType,
    TextEvidence,
)
from services.retrieval.evidence_providers.base import (
    CriticalStorageFailure,
    DegradedProviderFailure,
    EvidenceProvider,
)
from services.retrieval.vector_retriever import VectorRetriever

logger = logging.getLogger(__name__)


class DenseEvidenceProvider:
    provider_name = "dense"

    def __init__(self, repository: CloudRepository | None = None) -> None:
        self.repo = repository or CloudRepository()
        self.retriever = VectorRetriever(repository=self.repo)

    def retrieve(
        self,
        query: str,
        workspace_id: str,
        top_k: int = 20,
        **kwargs: Any,
    ) -> list[EvidenceEnvelope]:
        if not workspace_id:
            raise CriticalStorageFailure("workspace_id must be provided for Dense vector retrieval")

        query_embedding = kwargs.get("query_embedding")
        candidate_page_ids = kwargs.get("candidate_page_ids")
        candidate_chunk_ids = kwargs.get("candidate_chunk_ids")
        document_ids = kwargs.get("document_ids")

        try:
            results = self.retriever.search(
                query=query,
                query_embedding=query_embedding,
                document_ids=document_ids,
                candidate_page_ids=candidate_page_ids,
                candidate_chunk_ids=candidate_chunk_ids,
                workspace_id=workspace_id,
                top_k=top_k,
            )
        except Exception as exc:
            logger.warning("Dense vector retrieval failed, degrading gracefully: %s", exc)
            raise DegradedProviderFailure(f"Dense retrieval degraded: {exc}") from exc

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
                evidence_id=f"dense:{chunk.id}",
                evidence_type=EvidenceType.TEXT,
                document_id=chunk.document_id,
                location={
                    "page_number": chunk.page_number,
                    "location_reference": chunk.location_reference,
                },
                provider=self.provider_name,
                provider_score=float(score),
                payload=payload,
                confidence=EvidenceConfidence(retrieval_confidence=float(score)),
                citation_text=chunk.text[:200],
                workspace_id=workspace_id,
            )
            envelopes.append(env)

        return envelopes
