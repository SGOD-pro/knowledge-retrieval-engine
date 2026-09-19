"""OKF Evidence Provider emitting first-class FactEvidence records."""

import logging
from typing import Any

from schemas.evidence import (
    EvidenceConfidence,
    EvidenceEnvelope,
    EvidenceType,
    FactEvidence,
)
from services.retrieval.evidence_providers.base import (
    EvidenceProvider,
    OptionalProviderFailure,
)
from services.retrieval.okf_retriever import OKFRetriever
from services.retrieval.planner import extract_entities

logger = logging.getLogger(__name__)


class OKFEvidenceProvider:
    provider_name = "okf"

    def __init__(self, retriever: OKFRetriever | None = None) -> None:
        self.retriever = retriever or OKFRetriever()

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
            props = self.retriever.lookup(entities)
        except Exception as exc:
            logger.warning("OKF provider failed to lookup entities %s: %s", entities, exc)
            raise OptionalProviderFailure(f"OKF lookup failed: {exc}") from exc

        envelopes: list[EvidenceEnvelope] = []
        for idx, prop in enumerate(props[:top_k]):
            ent = prop.get("entity", "")
            attr = prop.get("attribute", "")
            val = str(prop.get("value", ""))
            conf = float(prop.get("confidence", 0.95))
            src_chunk = prop.get("source_chunk_id")

            payload = FactEvidence(
                entity=ent,
                attribute=attr,
                value=val,
                source_chunk_id=src_chunk,
                confidence=conf,
            )
            citation = f"{ent}: {attr} = {val}"

            env = EvidenceEnvelope(
                evidence_id=f"okf:{ent}:{attr}:{idx}",
                evidence_type=EvidenceType.FACT,
                document_id=prop.get("document_id", "okf_store"),
                location={"entity": ent, "attribute": attr},
                provider=self.provider_name,
                provider_score=conf,
                payload=payload,
                confidence=EvidenceConfidence(retrieval_confidence=conf),
                citation_text=citation,
                workspace_id=workspace_id,
            )
            envelopes.append(env)

        return envelopes
