"""Evidence models, typed discriminated union payloads, and confidence hierarchy."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Union


class EvidenceType(str, Enum):
    TEXT = "text"
    STRUCTURED_ROW = "structured_row"
    TABLE_SUMMARY = "table_summary"
    FACT = "fact"
    RELATIONSHIP = "relationship"
    METADATA = "metadata"


@dataclass(frozen=True)
class TextEvidence:
    text: str
    element_type: str
    section_path: tuple[str, ...] = ()
    page_number: int | None = None
    bounding_box: dict[str, float] | None = None


@dataclass(frozen=True)
class StructuredRowEvidence:
    table_id: str
    row_id: str
    row_index: int
    headers: tuple[str, ...]
    values: tuple[Any, ...]
    sheet_name: str | None = None


@dataclass(frozen=True)
class FactEvidence:
    entity: str
    attribute: str
    value: str
    source_chunk_id: str | None = None
    confidence: float = 1.0


@dataclass(frozen=True)
class RelationshipEvidence:
    source_entity: str
    relation: str
    target_entity: str
    supporting_fact_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetadataEvidence:
    document_id: str
    attribute_name: str
    attribute_value: Any
    sheet_name: str | None = None


EvidencePayload = Union[
    TextEvidence,
    StructuredRowEvidence,
    FactEvidence,
    RelationshipEvidence,
    MetadataEvidence,
]


@dataclass(frozen=True)
class EvidenceConfidence:
    retrieval_confidence: float = 1.0
    structural_confidence: float = 1.0
    schema_binding_confidence: float = 1.0
    entity_binding_confidence: float = 1.0
    datatype_confidence: float = 1.0
    provenance_confidence: float = 1.0

    @property
    def overall_confidence(self) -> float:
        """Weighted arithmetic blend of retrieval, schema, and structural confidences."""
        return round(
            (self.retrieval_confidence * 0.3)
            + (self.schema_binding_confidence * 0.4)
            + (self.structural_confidence * 0.3),
            4,
        )


@dataclass(frozen=True)
class EvidenceEnvelope:
    evidence_id: str
    evidence_type: EvidenceType
    document_id: str
    location: dict[str, Any]
    provider: str  # "bm25" | "dense" | "page_index" | "schema_table" | "okf" | "graph" | "metadata"
    provider_score: float  # Only meaningful within that provider unless calibrated!
    payload: EvidencePayload
    confidence: EvidenceConfidence = field(default_factory=EvidenceConfidence)
    citation_text: str = ""
    workspace_id: str = ""


@dataclass(frozen=True)
class EvidenceChunk:
    id: str
    document_id: str
    source_format: str
    text: str
    evidence_type: EvidenceType = EvidenceType.TEXT
    structural_node_id: str | None = None
    page_number: int | None = None
    sheet_name: str | None = None
    slide_number: int | None = None
    section_path: tuple[str, ...] = ()
    bounding_box: dict[str, float] | None = None
    location_reference: str = ""
    # Structured references: links retrieval evidence directly to StructuredTable/TableRow
    table_id: str | None = None
    row_ids: tuple[str, ...] = ()
    # Ranking & embedding attributes
    structural_weight: float = 0.0
    similarity_score: float | None = None
    reranker_score: float | None = None
    provider: str = "dev"
    embedding_fast: list[float] | None = None
    embedding_full: list[float] | None = None
    image_s3_keys: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    workspace_id: str = ""
