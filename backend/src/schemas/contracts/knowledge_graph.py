"""Typed contracts and schemas for Bounded Knowledge Graph Enrichment.

Provides frozen Pydantic models for KnowledgeGraphEntity, KnowledgeGraphRelationship,
KnowledgeGraphManifest, PinnedDocumentVersion, deeply immutable PinnedSnapshotContext,
ChunkLookupKey, KnowledgeGraphLookupResult, and KnowledgeGraphBudgetLedger.
"""

from __future__ import annotations

from enum import StrEnum
import hashlib
import re
import unicodedata
import urllib.parse
import uuid
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

try:
    from src.schemas.contracts.location import DocumentLocation, TableLocation
except ImportError:
    from schemas.contracts.location import DocumentLocation, TableLocation


def normalize_mention(text: str) -> str:
    """Normalize mention token with NFKD normalization, lowercasing, and whitespace collapse.
    
    Transforms non-alphanumeric characters except underscore to space, collapses
    internal whitespace to single underscores, and trims leading/trailing underscores.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    cleaned = re.sub(r"[^\w\s]", " ", normalized)
    tokens = cleaned.lower().split()
    return "_".join(tokens)


def escape_delimiter(token: str) -> str:
    """Escape delimiter character '#' to '%23' to avoid collision in compound DynamoDB keys."""
    if not token:
        return ""
    return token.replace("#", "%23")


def unescape_delimiter(token: str) -> str:
    """Unescape delimiter '%23' back to '#'."""
    if not token:
        return ""
    return token.replace("%23", "#")


_ALLOWLISTED_PREDICATES: dict[str, str] = {
    "acquired": "ACQUIRED",
    "subsidiary_of": "SUBSIDIARY_OF",
    "partnered_with": "PARTNERED_WITH",
    "located_in": "LOCATED_IN",
    "founded_by": "FOUNDED_BY",
    "supplies": "SUPPLIES",
    "produces": "PRODUCES",
    "employs": "EMPLOYS",
    "invested_in": "INVESTED_IN",
    "member_of": "MEMBER_OF",
    "related_to": "RELATED_TO",
    "parent_of": "PARENT_OF",
}


def canonicalize_predicate(predicate: str) -> str:
    """Map predicate string to allowlisted upper snake case or URL-safe percent-encoded representation."""
    if not predicate:
        return "RELATED_TO"
    cleaned = predicate.strip().lower().replace(" ", "_")
    if cleaned in _ALLOWLISTED_PREDICATES:
        return _ALLOWLISTED_PREDICATES[cleaned]
    return urllib.parse.quote(cleaned, safe="")


def compute_entity_id(
    workspace_id: str,
    document_id: str | None,
    entity_type: str,
    name: str,
    disambiguation_token: str | None = None,
) -> str:
    """Compute deterministic entity identifier.
    
    Defaults to document-scoped identity to prevent homonym false joins across documents.
    Only joins cross-document when backed by a verified disambiguation token.
    """
    norm_name = normalize_mention(name)
    norm_type = entity_type.strip().lower()
    if disambiguation_token:
        norm_disambig = normalize_mention(disambiguation_token)
        canonical_key = f"{workspace_id}:{norm_type}:{norm_name}:{norm_disambig}"
        return f"ent_{hashlib.sha256(canonical_key.encode('utf-8')).hexdigest()[:16]}"
    doc_id = document_id or "global"
    doc_scoped_key = f"{workspace_id}:DOC#{doc_id}:{norm_type}:{norm_name}"
    return f"ent_{hashlib.sha256(doc_scoped_key.encode('utf-8')).hexdigest()[:16]}"


def compute_relationship_id(
    workspace_id: str,
    source_entity_id: str,
    canonical_predicate: str,
    target_entity_id: str,
    document_id: str,
    document_version: str,
    publication_id: str,
) -> str:
    """Compute deterministic relationship identifier including publication ID."""
    key = f"{workspace_id}:{source_entity_id}:{canonical_predicate}:{target_entity_id}:{document_id}:{document_version}:{publication_id}"
    return f"rel_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"


def generate_publication_id() -> str:
    """Generate fresh publication identifier for isolation staging."""
    return f"pub_{uuid.uuid4().hex[:16]}"


class GraphStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    EMPTY = "empty"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class KnowledgeGraphEntity(BaseModel):
    """Canonical entity record in a workspace."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str = Field(description="Deterministic hash: ent_{sha256(...)[:16]}")
    workspace_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=512)
    canonical_name: str = Field(..., max_length=512)
    entity_type: Literal["organization", "person", "location", "product", "concept", "table", "other"] = "concept"
    source_document_id: str | None = Field(default=None, max_length=128, description="Source document ID for document scoped entities")
    disambiguation_token: str | None = Field(default=None, max_length=128, description="Verified registration, ticker, or qualifier for cross document joins")
    aliases: tuple[str, ...] = Field(default_factory=tuple)


class PinnedDocumentVersion(BaseModel):
    """Pinned document version and publication bindings for a query snapshot context."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str = Field(..., max_length=128)
    document_version: str = Field(..., max_length=64)
    publication_id: str = Field(..., max_length=128)
    is_tombstoned: bool = False


class PinnedSnapshotContext(BaseModel):
    """Deeply immutable request-local snapshot context pinned at query start."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str = Field(..., max_length=64)
    captured_at_utc: str
    active_documents: tuple[PinnedDocumentVersion, ...] = Field(default_factory=tuple)

    def get_document(self, document_id: str) -> PinnedDocumentVersion | None:
        for doc in self.active_documents:
            if doc.document_id == document_id:
                return doc
        return None

    def is_active_publication(self, document_id: str, publication_id: str) -> bool:
        doc = self.get_document(document_id)
        return doc is not None and doc.publication_id == publication_id and not doc.is_tombstoned


class ChunkLookupKey(BaseModel):
    """Targeted chunk identifier and version binding for batch hydration."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(..., max_length=128)
    document_id: str = Field(..., max_length=128)
    document_version: str = Field(..., max_length=64)


class KnowledgeGraphRelationship(BaseModel):
    """Verified directed relationship edge grounded in source document evidence."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    relationship_id: str = Field(description="Deterministic hash: rel_{sha256(ws:src:pred:tgt:doc:ver:pub)[:24]}")
    workspace_id: str = Field(..., max_length=64)
    source_entity_id: str = Field(..., max_length=64)
    source_entity_name: str = Field(..., max_length=512)
    predicate: str = Field(..., max_length=128)
    canonical_predicate: str = Field(..., max_length=128)
    target_entity_id: str = Field(..., max_length=64)
    target_entity_name: str = Field(..., max_length=512)
    directed: bool = True
    confidence: float = Field(ge=0.0, le=1.0)
    source_document_id: str = Field(..., max_length=128)
    source_document_version: str = Field(..., max_length=64)
    publication_id: str = Field(..., max_length=128)
    source_chunk_ids: tuple[str, ...] = Field(default_factory=tuple)
    location: DocumentLocation | TableLocation
    verbatim_evidence: str = Field(..., max_length=4096)
    relationship_state: Literal["unique", "conflicting", "ambiguous"] = "unique"
    created_at_utc: str


class KnowledgeGraphManifest(BaseModel):
    """Manifest record certifying active publication outcome for a document version."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=128)
    document_version: str = Field(..., max_length=64)
    publication_id: str = Field(..., max_length=128)
    base_publication_id: str | None = Field(default=None, max_length=128)
    status: Literal["ready", "empty", "failed", "unavailable"] = "ready"
    entity_count: int = Field(default=0, ge=0)
    relationship_count: int = Field(default=0, ge=0)
    ingested_at_utc: str
    content_hash: str = Field(..., max_length=128)


class KnowledgeGraphLookupResult(BaseModel):
    """Outcome of multi hop graph traversal and chunk hydration."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    seed_entities: tuple[str, ...] = Field(default_factory=tuple)
    pinned_snapshot_captured_at: str | None = None
    entity_state: Literal["unique", "ambiguous", "none"] = "none"
    relationship_state: Literal["unique", "conflicting", "none"] = "none"
    traversed_node_count: int = 0
    edge_match_count: int = 0
    matched_chunk_ids: tuple[str, ...] = Field(default_factory=tuple)
    candidate_chunks: tuple[Any, ...] = Field(default_factory=tuple)
    was_truncated: bool = False
    truncation_reason: str | None = None
    execution_status: Literal[
        "completed", "truncated", "caller_timeout", "admission_rejected", "tombstoned", "storage_failed"
    ] = "completed"
    latency_ms: float = 0.0


class KnowledgeGraphBudgetLedger(BaseModel):
    """Durable budget ledger record tracking call reservations and spent tokens."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=128)
    document_version: str = Field(..., max_length=64)
    reserved_calls: int = Field(default=0, ge=0)
    completed_calls: int = Field(default=0, ge=0)
    reserved_tokens: int = Field(default=0, ge=0)
    spent_tokens: int = Field(default=0, ge=0)
    reserved_prompt_tokens: int = Field(default=0, ge=0)
    spent_prompt_tokens: int = Field(default=0, ge=0)
    reserved_output_tokens: int = Field(default=0, ge=0)
    spent_output_tokens: int = Field(default=0, ge=0)
    lease_owner_id: str | None = Field(default=None, max_length=128)
    lease_expires_at: float = Field(default=0.0, ge=0.0)
    fence_token: int = Field(default=0, ge=0)
    reconciled_fences: tuple[int, ...] = Field(default_factory=tuple)
    status: Literal["active", "completed", "exhausted", "failed"] = "active"
    updated_at: str


class KnowledgeGraphReservationResult(BaseModel):
    """Outcome of attempting to reserve an extraction call permit from durable ledger."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    granted: bool
    fence_token: int = 0
    call_number: int = 0
    remaining_calls: int = 0
    reason: Literal[
        "granted",
        "calls_exhausted",
        "tokens_exhausted",
        "active_lease_held",
        "already_completed",
        "already_exhausted",
        "already_failed",
        "storage_error",
    ]
    ledger: KnowledgeGraphBudgetLedger | None = None


class KnowledgeGraphReconciliationResult(BaseModel):
    """Outcome of reconciling a completed or failed extraction call."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    reconciled: bool
    fenced_out: bool = False
    reason: Literal["reconciled", "already_reconciled", "fenced_out", "not_found", "storage_error"]
    ledger: KnowledgeGraphBudgetLedger | None = None


import sys
if __name__ in ("schemas.contracts.knowledge_graph", "src.schemas.contracts.knowledge_graph"):
    sys.modules["schemas.contracts.knowledge_graph"] = sys.modules[__name__]
    sys.modules["src.schemas.contracts.knowledge_graph"] = sys.modules[__name__]

