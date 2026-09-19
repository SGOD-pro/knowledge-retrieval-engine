"""Graph Evidence Provider emitting first-class RelationshipEvidence records."""

import logging
from typing import Any

from schemas.evidence import (
    EvidenceConfidence,
    EvidenceEnvelope,
    EvidenceType,
    RelationshipEvidence,
)
from services.retrieval.evidence_providers.base import (
    EvidenceProvider,
    OptionalProviderFailure,
)
from services.retrieval.graph_retriever import GraphRetriever
from services.retrieval.planner import extract_entities

logger = logging.getLogger(__name__)


class GraphEvidenceProvider:
    provider_name = "graph"

    def __init__(self, retriever: GraphRetriever | None = None) -> None:
        self.retriever = retriever or GraphRetriever()

    def retrieve(
        self,
        query: str,
        workspace_id: str,
        top_k: int = 20,
        **kwargs: Any,
    ) -> list[EvidenceEnvelope]:
        entities = extract_entities(query)
        if not entities:
            return []

        try:
            results = self.retriever.expand(entities)
        except Exception as exc:
            logger.warning("Graph provider failed to expand entities %s: %s", entities, exc)
            raise OptionalProviderFailure(f"Graph expansion failed: {exc}") from exc

        envelopes: list[EvidenceEnvelope] = []
        for idx, item in enumerate(results[:top_k]):
            src = item.get("source", "")
            tgt = item.get("target", "")
            rel = item.get("relation", "related_to")
            weight = float(item.get("weight", 0.8))

            payload = RelationshipEvidence(
                source_entity=src,
                relation=rel,
                target_entity=tgt,
            )
            citation = f"{src} -> {rel} -> {tgt}"

            env = EvidenceEnvelope(
                evidence_id=f"graph:{src}:{rel}:{tgt}:{idx}",
                evidence_type=EvidenceType.RELATIONSHIP,
                document_id=item.get("document_id", "knowledge_graph"),
                location={"source": src, "target": tgt},
                provider=self.provider_name,
                provider_score=weight,
                payload=payload,
                confidence=EvidenceConfidence(retrieval_confidence=weight),
                citation_text=citation,
                workspace_id=workspace_id,
            )
            envelopes.append(env)

        return envelopes
