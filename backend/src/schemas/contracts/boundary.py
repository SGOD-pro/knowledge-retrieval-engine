"""Workspace boundary, isolation, and authorization contracts.

Provides frozen Pydantic models for workspace records, tombstones,
restricted worker contexts, deterministic cache key descriptors,
vector filter contracts, and error envelopes.
"""

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from src.schemas.contracts.envelope import TrustedAuthContext
from src.schemas.contracts.location import Location


class WorkspaceAccessDeniedError(PermissionError):
    """Raised when service or repository methods receive mismatched workspace credentials."""
    pass


class CASConflictError(Exception):
    """Raised specifically when compare and swap encounters a generation mismatch."""
    pass


class WorkspaceRecord(BaseModel):
    """Authoritative workspace record in DynamoDB or SQLite."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    workspace_id: str = Field(..., max_length=64, pattern=r"^ws_[a-zA-Z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=128)
    owner_principal_id: str = Field(..., max_length=64)
    manifest_generation: int = Field(default=1, ge=1)
    status: Literal["active", "tombstoned"] = "active"
    created_at: str
    updated_at: str

    def increment_manifest_generation(self) -> "WorkspaceRecord":
        return WorkspaceRecord(
            workspace_id=self.workspace_id,
            name=self.name,
            owner_principal_id=self.owner_principal_id,
            manifest_generation=self.manifest_generation + 1,
            status=self.status,
            created_at=self.created_at,
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    def transition_to_tombstoned(self) -> "WorkspaceRecord":
        return WorkspaceRecord(
            workspace_id=self.workspace_id,
            name=self.name,
            owner_principal_id=self.owner_principal_id,
            manifest_generation=self.manifest_generation + 1,
            status="tombstoned",
            created_at=self.created_at,
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )


class PrincipalRecord(BaseModel):
    """Test identity fixture record."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    principal_id: str = Field(..., max_length=64, pattern=r"^(usr|sa)_[a-zA-Z0-9_-]+$")
    email: str
    display_name: str = Field(..., min_length=1, max_length=128)
    created_at: str


class SnapshotRecord(BaseModel):
    """Authoritative snapshot record binding exact source versions and artifacts."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    snapshot_id: str = Field(..., max_length=128)
    workspace_id: str = Field(..., max_length=64)
    manifest_generation: int = Field(..., ge=1)
    source_versions: tuple[tuple[str, int], ...]
    artifact_ids: tuple[str, ...]
    status: Literal["active", "tombstoned"] = "active"
    created_at: str

    @property
    def source_version_map(self) -> dict[str, int]:
        return dict(self.source_versions)


class JobRecord(BaseModel):
    """Authoritative background job record loaded by server."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    job_id: str = Field(..., max_length=64, pattern=r"^job_[a-zA-Z0-9_-]+$")
    workspace_id: str = Field(..., max_length=64, pattern=r"^ws_[a-zA-Z0-9_-]+$")
    operation: Literal["ingest", "index", "evaluate"]
    target_source_id: str | None = None
    target_source_version: int | None = None
    status: Literal["queued", "running", "completed", "failed", "cancelled"] = "queued"
    is_trusted_system_job: bool = True
    created_at: str
    updated_at: str


class WorkerExecutionContext(BaseModel):
    """Scoped execution context for asynchronous background pipeline workers."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    job_id: str = Field(..., max_length=64, pattern=r"^job_[a-zA-Z0-9_-]+$")
    authorized_workspace_id: str = Field(..., max_length=64, pattern=r"^ws_[a-zA-Z0-9_-]+$")
    operation: Literal["ingest", "index", "evaluate"]
    target_source_id: str | None = None
    target_source_version: int | None = None
    auth_context: TrustedAuthContext

    def validate_operation_scope(
        self,
        target_workspace_id: str,
        operation: str,
        source_id: str | None = None,
        source_version: int | None = None,
    ) -> None:
        """Enforce that worker does not execute outside its server loaded job scope."""
        if target_workspace_id != self.authorized_workspace_id:
            raise WorkspaceAccessDeniedError(
                f"Worker job {self.job_id} cannot access foreign workspace {target_workspace_id}"
            )
        if operation != self.operation:
            raise WorkspaceAccessDeniedError(
                f"Worker job {self.job_id} not authorized for operation {operation}"
            )
        if self.target_source_id is not None and source_id != self.target_source_id:
            raise WorkspaceAccessDeniedError(
                f"Worker job {self.job_id} restricted to source {self.target_source_id}"
            )
        if self.target_source_version is not None and source_version != self.target_source_version:
            raise WorkspaceAccessDeniedError(
                f"Worker job {self.job_id} restricted to version {self.target_source_version}"
            )


class TombstoneRecord(BaseModel):
    """Durable deletion barrier record."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    workspace_id: str = Field(..., max_length=64)
    resource_type: Literal["workspace", "source", "artifact"]
    resource_id: str = Field(..., max_length=128)
    manifest_generation: int = Field(..., ge=1)
    tombstoned_at: str
    deleted_by_principal_id: str = Field(..., max_length=64)
    reason: str = Field(default="user_requested", max_length=256)

    @property
    def composite_sort_key(self) -> str:
        return f"tombstone#{self.resource_type}#{self.resource_id}"


class SourceItem(BaseModel):
    """Summary record for registered sources."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_id: str = Field(..., max_length=128)
    title: str = Field(..., min_length=1, max_length=256)
    status: Literal["registered", "published", "tombstoned"] = "registered"
    created_at: str
    updated_at: str


class CitationItem(BaseModel):
    """Citation reference within a query answer carrying typed Location union."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    evidence_id: str = Field(..., max_length=128)
    source_id: str = Field(..., max_length=128)
    source_version: int = Field(..., ge=1)
    snippet: str = Field(..., max_length=2048)
    location: Location


class CachedQueryResult(BaseModel):
    """Payload stored in Redis cache binding authorization, execution, and source dependencies."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    cache_key: str
    authorized_workspace_id: str
    authorized_principal_id: str
    access_policy_version: str
    execution_id: str
    snapshot_id: str
    dependent_source_versions: tuple[tuple[str, int], ...]
    dependent_artifact_ids: tuple[str, ...]
    answer: str
    citations: tuple[CitationItem, ...]
    cached_at: str


class CacheKeyDescriptor(BaseModel):
    """Frozen descriptor generating deterministic version aware cache keys."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    workspace_id: str
    principal_id: str
    session_id: str | None = None
    query_hash: str = Field(..., min_length=64, max_length=64)
    source_version_set_hash: str = Field(..., min_length=64, max_length=64)
    snapshot_id: str
    access_policy_version: str = "v1"
    model_config_version: str = "v1"

    @classmethod
    def compute_query_hash(cls, query_text: str, predicates: tuple[str, ...] = ()) -> str:
        """Compute SHA256 over exact literal query text and sorted selection predicates.
        
        Preserves all exact internal whitespace and character casing so quoted literals
        such as 'A  B' and 'A B' produce distinct cryptographic hashes.
        """
        sorted_preds = sorted(predicates)
        payload = {"predicates": sorted_preds, "text": query_text}
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def compute_source_version_set_hash(cls, source_versions: tuple[tuple[str, int], ...]) -> str:
        """Compute SHA256 digest over canonical JSON of sorted source version pairs."""
        sorted_pairs = sorted(source_versions, key=lambda pair: pair[0])
        payload = [{"source_id": s_id, "version": ver} for s_id, ver in sorted_pairs]
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def to_canonical_key(self) -> str:
        canonical_payload = {
            "access_policy_version": self.access_policy_version,
            "model_config_version": self.model_config_version,
            "principal_id": self.principal_id,
            "query_hash": self.query_hash,
            "session_id": self.session_id,
            "snapshot_id": self.snapshot_id,
            "source_version_set_hash": self.source_version_set_hash,
            "workspace_id": self.workspace_id,
        }
        serialized = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return f"kre:cache:{self.workspace_id}:{digest}"


class QdrantFilterContract(BaseModel):
    """Contract enforcing tenant boundary filter in vector search requests."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    authorized_workspace_id: str = Field(..., min_length=1, max_length=64)

    def validate_and_build_search_filter(self, caller_workspace_id: str) -> dict[str, Any]:
        """Validate workspace filter before query dispatch, rejecting missing or mismatched filters."""
        if not caller_workspace_id or not caller_workspace_id.strip():
            raise WorkspaceAccessDeniedError("Missing caller workspace filter")
        if caller_workspace_id != self.authorized_workspace_id:
            raise WorkspaceAccessDeniedError(
                f"Filter workspace '{self.authorized_workspace_id}' does not match caller workspace '{caller_workspace_id}'"
            )
        return {
            "must": [
                {"key": "workspace_id", "match": {"value": self.authorized_workspace_id}}
            ]
        }


class PublicationManifestRecord(BaseModel):
    """Authoritative snapshot publication manifest."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    workspace_id: str
    manifest_generation: int
    active_source_ids: tuple[str, ...]
    updated_at: str


class QueryExecutionOutcome(BaseModel):
    """Structured outcome for query execution and delivery gate evaluation."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    status: Literal["SUCCESS", "INSUFFICIENT_COVERAGE"]
    answer: str
    citations: tuple[CitationItem, ...]
    surviving_source_ids: tuple[str, ...]
    tombstoned_source_ids: tuple[str, ...]


class ErrorEnvelope(BaseModel):
    """Standard RFC 7807 error envelope format."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
