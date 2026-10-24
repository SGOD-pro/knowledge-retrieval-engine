"""Typed structural records and contracts for PageIndex hierarchy and lookup."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class SectionSummary(BaseModel):
    """Document section or heading node summary."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(..., max_length=512)
    level: int = Field(default=1, ge=1, le=6)
    page_start: int = Field(default=1, ge=1)
    page_end: int = Field(default=1, ge=1)


class ElementLocation(BaseModel):
    """Verified structural definition target location."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    element_id: str = Field(..., max_length=256)
    page_number: int = Field(default=1, ge=1)
    element_type: Literal["table", "figure", "heading", "caption"]
    label: str = Field(..., max_length=128)
    title: str = Field(default="", max_length=512)
    bounding_box: dict[str, float] | None = None


class CrossReference(BaseModel):
    """Textual cross-reference mention in body paragraph or table cell."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    element_id: str = Field(..., max_length=256)
    page_number: int = Field(default=1, ge=1)
    label: str = Field(..., max_length=128)
    context: str = Field(default="", max_length=512)


class PageIndexDetail(BaseModel):
    """Complete structural element topology persisted to S3 on overflow."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=128)
    document_version: str = Field(..., max_length=64)
    sections: tuple[SectionSummary, ...] = Field(default_factory=tuple)
    definitions: dict[str, tuple[ElementLocation, ...]] = Field(default_factory=dict)
    cross_references: dict[str, tuple[CrossReference, ...]] = Field(default_factory=dict)


class PageIndexSummary(BaseModel):
    """Durable PageIndex summary record stored in DynamoDB or SQLite."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=128)
    document_version: str = Field(..., max_length=64)
    total_pages: int = Field(default=0, ge=0)
    layout_status: Literal["ready", "missing_layout", "failed"] = "ready"
    root_sections: tuple[SectionSummary, ...] = Field(default_factory=tuple)
    has_s3_overflow: bool = False
    s3_overflow_key: str | None = None
    inline_definitions: dict[str, tuple[ElementLocation, ...]] = Field(default_factory=dict)
    inline_cross_references: dict[str, tuple[int, ...]] = Field(default_factory=dict)
    created_at: str
    structural_elements_count: int = Field(
        default=0,
        description="Combined count of section headings and verified definition locations.",
    )
    raw_payload_bytes: int | None = None
    sections_overflowed: bool = False


class StructuralLookupResult(BaseModel):
    """Result of querying PageIndex for a structural target."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    target_label: str
    state: Literal["unique", "ambiguous", "missing_layout", "none"]
    candidate_pages: tuple[int, ...] = Field(default_factory=tuple)
    matching_locations: tuple[ElementLocation, ...] = Field(default_factory=tuple)
    has_cross_references_only: bool = False
    cross_reference_pages: tuple[int, ...] = Field(default_factory=tuple)
