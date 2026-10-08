"""Canonical evidence contract representing immutable source extraction.

Enforces rebuild safe identifiers, locator and payload compatibility validation,
extraction quality metrics, deep immutability, and payload byte caps.
"""

import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.schemas.contracts.envelope import TrustedAuthContext
from src.schemas.contracts.location import Location
from src.schemas.contracts.payload import (
    MAX_PAYLOAD_BYTES,
    ContentPayload,
    QualifierItem,
)

EvidenceType = Literal[
    "text_chunk",
    "table_cell",
    "table_slice",
    "figure_asset",
    "structured_record",
]

QualityStatus = Literal["VALID", "DEGRADED", "REJECTED"]


class ExtractionQuality(BaseModel):
    """Quality assessment of extracted source evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: QualityStatus
    confidence: float = Field(..., ge=0.0, le=1.0)


class EvidenceProvenance(BaseModel):
    """Extraction provenance linking evidence to parser and content digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content_hash: str
    extraction_pipeline_version: str
    parser_element_id: str | None = None

    @field_validator("content_hash", "extraction_pipeline_version")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field must be a non empty string")
        return v


class CanonicalEvidence(BaseModel):
    """Deeply immutable canonical source evidence record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str
    source_id: str
    source_version: int = Field(..., ge=1)
    evidence_id: str
    type: EvidenceType
    location: Location
    content: ContentPayload
    unit: str | None = None
    qualifiers: tuple[QualifierItem, ...] = Field(default_factory=tuple)
    provenance: EvidenceProvenance
    quality: ExtractionQuality

    @field_validator("workspace_id", "source_id", "evidence_id")
    @classmethod
    def validate_non_empty_strings(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Identifier must be a non empty string")
        return v

    @field_validator("qualifiers", mode="before")
    @classmethod
    def coerce_qualifiers(cls, v: Any) -> tuple[QualifierItem, ...]:
        if isinstance(v, (list, tuple)):
            coerced: list[QualifierItem] = []
            for item in v:
                if isinstance(item, QualifierItem):
                    coerced.append(item)
                elif isinstance(item, dict):
                    coerced.append(QualifierItem(**item))
                else:
                    raise ValueError(f"Invalid qualifier item type: {type(item).__name__}")
            return tuple(coerced)
        return v

    @model_validator(mode="after")
    def validate_compatibility_and_size(self) -> "CanonicalEvidence":
        loc_type = self.location.type
        content_kind = self.content.kind

        # Locator and Payload Compatibility Validation Matrix (AC-10)
        if self.type == "text_chunk":
            if loc_type not in ("document", "section", "slide"):
                raise ValueError(
                    f"text_chunk evidence requires document, section, or slide location; received {loc_type}"
                )
            if content_kind != "text":
                raise ValueError(
                    f"text_chunk evidence requires text payload; received {content_kind}"
                )

        elif self.type == "table_cell":
            if loc_type != "table":
                raise ValueError(
                    f"table_cell evidence requires table location; received {loc_type}"
                )
            if content_kind not in ("cell", "formula"):
                raise ValueError(
                    f"table_cell evidence requires cell or formula payload; received {content_kind}"
                )

        elif self.type == "table_slice":
            if loc_type != "table":
                raise ValueError(
                    f"table_slice evidence requires table location; received {loc_type}"
                )
            if content_kind not in ("table", "dataset_ref"):
                raise ValueError(
                    f"table_slice evidence requires table or dataset_ref payload; received {content_kind}"
                )

        elif self.type == "figure_asset":
            if loc_type not in ("document", "slide"):
                raise ValueError(
                    f"figure_asset evidence requires document or slide location; received {loc_type}"
                )
            if content_kind != "asset":
                raise ValueError(
                    f"figure_asset evidence requires asset payload; received {content_kind}"
                )

        elif self.type == "structured_record":
            if loc_type not in ("section", "table"):
                raise ValueError(
                    f"structured_record evidence requires section or table location; received {loc_type}"
                )
            if content_kind not in ("text", "cell", "formula"):
                raise ValueError(
                    f"structured_record evidence requires text, cell, or formula payload; received {content_kind}"
                )

        # Total serialized payload size cap <= 262144 bytes (256 KB)
        serialized_bytes = len(json.dumps(self.model_dump(mode="json")).encode("utf-8"))
        if serialized_bytes > MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"CanonicalEvidence serialized size {serialized_bytes} bytes exceeds "
                f"maximum limit of {MAX_PAYLOAD_BYTES} bytes (256 KB)"
            )

        return self

    @classmethod
    def create_from_extraction(
        cls,
        auth_context: TrustedAuthContext,
        source_id: str,
        source_version: int,
        pipeline_version: str,
        chunk_index: int,
        evidence_type: EvidenceType,
        location: Location,
        content: ContentPayload,
        provenance: EvidenceProvenance,
        quality: ExtractionQuality,
        qualifiers: tuple[QualifierItem, ...] = (),
        unit: str | None = None,
    ) -> "CanonicalEvidence":
        """Factory method constructing CanonicalEvidence with rebuild safe identifier."""
        clean_digest = provenance.content_hash.replace("sha256:", "")[:16]
        evidence_id = (
            f"ev_{source_id}_v{source_version}_{pipeline_version}_{clean_digest}_{chunk_index}"
        )
        return cls(
            workspace_id=auth_context.authorized_workspace_id,
            source_id=source_id,
            source_version=source_version,
            evidence_id=evidence_id,
            type=evidence_type,
            location=location,
            content=content,
            unit=unit,
            qualifiers=qualifiers,
            provenance=provenance,
            quality=quality,
        )
