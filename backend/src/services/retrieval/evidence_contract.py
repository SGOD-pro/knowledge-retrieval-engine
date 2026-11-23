"""Shared Evidence Contract for decoupled retrieval strategies.

Defines a single evidence model and strategy result model used by all
retrieval strategies, citation validators, and routing stages.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SourceType = Literal[
    "chunk",
    "table_row_set",
    "kg_node",
    "kg_edge",
    "page_region",
    "okf_fact",
]


@dataclass
class EvidenceItem:
    """Standardized atomic or aggregate unit of retrieved evidence."""

    evidence_id: str
    workspace_id: str
    document_id: str
    document_version: str | None
    source_type: SourceType
    locator: dict[str, Any]
    text: str | None
    structured_payload: dict[str, Any] | None
    score: float
    strategy: str
    provenance: dict[str, Any]
    citation_payload: dict[str, Any]
    document_filename: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceItem:
        locator = dict(data.get("locator") or {})
        citation_payload = dict(data.get("citation_payload") or {})
        if "document_filename" in data:
            doc_fname = data.get("document_filename")
        else:
            doc_fname = (
                locator.get("document_filename")
                or citation_payload.get("document_filename")
            )
        if "document_version" in data:
            doc_ver = data.get("document_version")
        else:
            doc_ver = (
                locator.get("document_version")
                or citation_payload.get("document_version")
            )
        return cls(
            evidence_id=str(data.get("evidence_id", "")),
            workspace_id=str(data.get("workspace_id", "")),
            document_id=str(data.get("document_id", "")),
            document_version=doc_ver,
            source_type=data.get("source_type", "chunk"),
            locator=locator,
            text=data.get("text"),
            structured_payload=data.get("structured_payload"),
            score=float(data.get("score", 0.0)),
            strategy=str(data.get("strategy", "")),
            provenance=dict(data.get("provenance") or {}),
            citation_payload=citation_payload,
            document_filename=doc_fname,
        )


def restore_evidence_item(item: Any) -> Any:
    """Restore an EvidenceItem from either an existing instance or serialized dictionary."""
    if isinstance(item, EvidenceItem):
        return item
    if isinstance(item, dict):
        return EvidenceItem.from_dict(item)
    return item


@dataclass
class RetrievalLimits:
    """Resource and search bounds passed to a retrieval strategy."""

    top_k: int = 10
    score_threshold: float = 0.0
    timeout_ms: float = 10000.0
    max_hops: int = 2
    max_retrieval_llm_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RetrievalStrategyResult:
    """Standard result returned by any evidence retrieval strategy."""

    strategy_name: str
    query: str
    items: list[EvidenceItem]
    confidence: float
    latency_ms: float
    remote_call_counts: dict[str, int]
    failure_reason: str | None = None
    debug_trace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["items"] = [item.to_dict() for item in self.items]
        return d


def evidence_item_from_chunk(
    chunk: Any,
    workspace_id: str,
    strategy: str,
    score: float | None = None,
    document_filename: str | None = None,
    boundary_repo: Any | None = None,
    document_version: str | None = None,
) -> EvidenceItem:
    """Convert an existing Chunk dataclass/object into an EvidenceItem."""
    from services.retrieval.response_builder import resolve_chunk_provenance

    cid = str(getattr(chunk, "id", ""))
    did = str(getattr(chunk, "document_id", ""))
    text = getattr(chunk, "text", "")
    metadata = getattr(chunk, "metadata", {}) or {}
    page_number = getattr(chunk, "page_number", None)
    if page_number is None and isinstance(metadata, dict):
        page_number = metadata.get("page")
    loc_ref = getattr(chunk, "location_reference", "") or ""

    fname, doc_ver = resolve_chunk_provenance(
        chunk=chunk,
        workspace_id=workspace_id,
        boundary_repo=boundary_repo,
        document_filename=document_filename,
        document_version=document_version,
    )

    ev_score = score if score is not None else (getattr(chunk, "reranker_score", None) or getattr(chunk, "similarity_score", None) or 0.0)

    locator = {
        "chunk_id": cid,
        "page_number": page_number,
        "location_reference": loc_ref,
        "section_path": getattr(chunk, "section_path", ()),
        "document_filename": fname,
        "document_version": doc_ver,
    }

    citation_payload = {
        "chunk_id": cid,
        "document_id": did,
        "document_filename": fname,
        "document_version": doc_ver,
        "location_reference": loc_ref,
        "page_number": page_number,
        "strategy": strategy,
    }

    provenance = {
        "source_format": getattr(chunk, "source_format", "pdf"),
        "structural_node_id": getattr(chunk, "structural_node_id", None),
        "metadata": metadata,
    }

    return EvidenceItem(
        evidence_id=cid,
        workspace_id=workspace_id,
        document_id=did,
        document_version=doc_ver,
        source_type="chunk",
        locator=locator,
        text=text,
        structured_payload=None,
        score=float(ev_score),
        strategy=strategy,
        provenance=provenance,
        citation_payload=citation_payload,
        document_filename=fname,
    )
