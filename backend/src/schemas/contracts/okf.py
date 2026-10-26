"""Typed schemas and contracts for Bounded Open Knowledge Framework (OKF) integration."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


def normalize_entity_name(name: str) -> str:
    """Normalize entity display name by trimming, lowercasing, and collapsing whitespace."""
    if not name:
        return ""
    cleaned = re.sub(r"[^\w\s-]", " ", name)
    return " ".join(cleaned.lower().split())


def normalize_property_key(key: str) -> str:
    """Normalize property key by trimming and lowercasing."""
    if not key:
        return ""
    return " ".join(key.strip().lower().split())


def compute_entity_id(workspace_id: str, document_id: str, normalized_name: str) -> str:
    """Derive deterministic document scoped entity identifier.
    
    Formula: sha256(workspace_id:document_id:normalized_name)[:16]
    """
    token = f"{workspace_id}:{document_id}:{normalized_name}"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def compute_assertion_id(
    workspace_id: str,
    document_id: str,
    document_version: str,
    entity_id: str,
    property_key: str,
    property_value: str,
    source_chunk_id: str,
) -> str:
    """Derive stable assertion identifier preserving distinct provenance.
    
    Formula: sha256(workspace_id:document_id:document_version:entity_id:key:value:source_chunk_id)[:24]
    """
    norm_k = normalize_property_key(property_key)
    token = (
        f"{workspace_id}:{document_id}:{document_version}:"
        f"{entity_id}:{norm_k}:{property_value}:{source_chunk_id}"
    )
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def compute_idempotency_key(workspace_id: str, document_id: str, document_version: str) -> str:
    """Derive idempotency key for document version extraction.
    
    Formula: sha256(workspace_id:document_id:document_version)
    """
    token = f"{workspace_id}:{document_id}:{document_version}"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class OkfEntityRecord(BaseModel):
    """Document scoped entity record persisted to DynamoDB / SQLite."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str = Field(..., max_length=64)
    entity_id: str = Field(..., max_length=64)
    entity_name: str = Field(..., max_length=512)
    normalized_name: str = Field(..., max_length=512)
    entity_type: Literal["organization", "person", "location", "table", "concept", "other"] = "organization"
    document_id: str = Field(..., max_length=128)
    document_version: str = Field(..., max_length=64)
    created_at: str


class OkfPropertyAssertion(BaseModel):
    """Verified factual property assertion backed by source chunk provenance."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    assertion_id: str = Field(..., max_length=64)
    workspace_id: str = Field(..., max_length=64)
    entity_id: str = Field(..., max_length=64)
    entity_name: str = Field(..., max_length=512)
    property_key: str = Field(..., max_length=256)
    property_value: str = Field(..., max_length=2048)
    value_type: Literal["string", "number", "date", "boolean", "schema_type"] = "string"
    source_chunk_id: str = Field(..., max_length=256)
    document_id: str = Field(..., max_length=128)
    document_version: str = Field(..., max_length=64)
    page_number: int | None = Field(default=None, ge=1, description="1 indexed page number where assertion appears; None for unpaginated or tabular data")
    table_name: str | None = Field(default=None, max_length=256)
    column_name: str | None = Field(default=None, max_length=256)
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)
    validation_status: Literal["verified", "rejected"] = "verified"
    created_at: str


class OkfManifest(BaseModel):
    """Complete OKF extraction outcome for a document version."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=128)
    document_version: str = Field(..., max_length=64)
    entities: tuple[OkfEntityRecord, ...] = Field(default_factory=tuple)
    assertions: tuple[OkfPropertyAssertion, ...] = Field(default_factory=tuple)
    rejected_assertions_count: int = Field(default=0, ge=0)
    status: Literal["ready", "empty", "failed"] = "ready"
    extraction_tokens: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    idempotency_key: str = Field(..., max_length=128)
    created_at: str


class OkfLookupResult(BaseModel):
    """Result of querying OKF for factual assertions targeting entities."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    target_entity: str
    entity_state: Literal["unique", "ambiguous", "none"] = "none"
    property_state: Literal["unique", "conflicting", "none"] = "none"
    matched_assertions: tuple[OkfPropertyAssertion, ...] = Field(default_factory=tuple)
    candidate_chunk_ids: tuple[str, ...] = Field(default_factory=tuple)
    candidate_chunks: tuple[Any, ...] = Field(default_factory=tuple)
    execution_status: Literal[
        "completed", "tombstoned", "caller_timeout", "admission_rejected", "saturated", "storage_failed"
    ] = "completed"
    latency_ms: float = 0.0


import sys
if __name__ in ("schemas.contracts.okf", "src.schemas.contracts.okf"):
    sys.modules["schemas.contracts.okf"] = sys.modules[__name__]
    sys.modules["src.schemas.contracts.okf"] = sys.modules[__name__]

