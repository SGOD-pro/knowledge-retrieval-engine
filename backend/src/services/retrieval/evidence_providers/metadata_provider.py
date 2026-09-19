"""Metadata Evidence Provider for document, sheet, and workbook manifest queries."""

import logging
from typing import Any

from schemas.evidence import (
    EvidenceConfidence,
    EvidenceEnvelope,
    EvidenceType,
    MetadataEvidence,
)
from services.retrieval.evidence_providers.base import (
    CriticalStorageFailure,
    EvidenceProvider,
)

logger = logging.getLogger(__name__)


class MetadataEvidenceProvider:
    provider_name = "metadata"

    def __init__(self, repository: Any = None) -> None:
        self.repo = repository

    def retrieve(
        self,
        query: str,
        workspace_id: str,
        top_k: int = 20,
        **kwargs: Any,
    ) -> list[EvidenceEnvelope]:
        if not workspace_id:
            raise CriticalStorageFailure("workspace_id must be provided for metadata retrieval")

        q_lower = query.lower()
        manifests = kwargs.get("manifests", [])
        if not manifests:
            return []

        envelopes: list[EvidenceEnvelope] = []
        for manifest in manifests:
            doc_id = getattr(manifest, "document_id", "")
            fname = getattr(manifest, "filename", "")

            # Check hidden sheet queries
            if "hidden" in q_lower or "sheet" in q_lower:
                for sheet in getattr(manifest, "detected_sheets", ()):
                    s_vis = getattr(sheet, "visibility", "visible")
                    s_name = getattr(sheet, "sheet_name", "")
                    s_state = getattr(sheet, "state", "populated")

                    if "hidden" in q_lower and s_vis in ("hidden", "very_hidden"):
                        payload = MetadataEvidence(
                            document_id=doc_id,
                            attribute_name="hidden_sheet",
                            attribute_value={"sheet_name": s_name, "visibility": s_vis},
                            sheet_name=s_name,
                        )
                        citation = f"Document '{fname}' contains hidden sheet '{s_name}' (visibility: {s_vis})."
                        env = EvidenceEnvelope(
                            evidence_id=f"meta:{doc_id}:{s_name}:hidden",
                            evidence_type=EvidenceType.METADATA,
                            document_id=doc_id,
                            location={"sheet": s_name},
                            provider=self.provider_name,
                            provider_score=1.0,
                            payload=payload,
                            confidence=EvidenceConfidence(retrieval_confidence=1.0),
                            citation_text=citation,
                            workspace_id=workspace_id,
                        )
                        envelopes.append(env)

                    if "empty" in q_lower and s_state == "empty":
                        payload = MetadataEvidence(
                            document_id=doc_id,
                            attribute_name="empty_sheet",
                            attribute_value={"sheet_name": s_name, "state": s_state},
                            sheet_name=s_name,
                        )
                        citation = f"Document '{fname}' contains empty sheet '{s_name}' (0 rows)."
                        env = EvidenceEnvelope(
                            evidence_id=f"meta:{doc_id}:{s_name}:empty",
                            evidence_type=EvidenceType.METADATA,
                            document_id=doc_id,
                            location={"sheet": s_name},
                            provider=self.provider_name,
                            provider_score=1.0,
                            payload=payload,
                            confidence=EvidenceConfidence(retrieval_confidence=1.0),
                            citation_text=citation,
                            workspace_id=workspace_id,
                        )
                        envelopes.append(env)

        return envelopes[:top_k]
