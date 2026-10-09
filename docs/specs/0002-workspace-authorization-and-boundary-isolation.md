# 0002. Workspace Authorization and Boundary Isolation

**Date**: 2026-10-08
**Status**: Accepted

## Summary

This specification establishes workspace boundary isolation and authorization enforcement for the Knowledge Retrieval Engine. It introduces a server injected trusted authentication dependency for FastAPI routes, dual boundary checks across API and service layers, atomic deletion tombstones coordinated with authoritative manifest generation counters, restricted worker contexts for background jobs, and version aware cache isolation. The design operates with fixed test identities in explicitly enabled development environments, exercising ingestion and retrieval behavior through controlled fixtures to prove boundary guarantees while deferring multi user collaboration management to later project phases.

## Context

The engine processes sensitive documents across distinct client workspaces. In earlier iterations, request parameters such as workspace identifiers, document identifiers, and session identifiers were accepted directly from client query parameters or request bodies. This design conflated resource location with access authority, creating severe vulnerabilities where an unauthorized client could access data belonging to another tenant simply by guessing an identifier.

Boundary leakage risks also arise in caching, vector indexing, asynchronous workers, and document deletion. Standard caching keyed solely on query text allows answers computed for one user or workspace to be served to another. Similarly, asynchronous or delayed document deletion creates time windows where deleted documents remain visible through historical pinned snapshots, vector search results, or cached query responses. Furthermore, concurrent publication workflows risk resurrecting deleted documents if deletion does not coordinate atomically with publication manifests.

Resolving these risks requires establishing strict workspace boundaries early in the refactoring process. We must ensure that requests through all production interfaces validate workspace and principal context, that cache hits never cross workspace boundaries, that deletion tombstones block access immediately, that background worker tasks carry restricted workspace context, and that comprehensive isolation test suites pass before building downstream ingestion, retrieval, and execution slices.

## Requirements

**User stories**:
* As a system developer, I want all incoming requests to validate workspace and principal authority through shared server dependencies so that client requests cannot access data outside their authorized workspace.
* As an API caller, I want requests attempting to access another workspace or foreign resource to receive an immediate 404 response so that resource existence is never revealed across tenant boundaries.
* As a security engineer, I want document deletion to record immediate durable tombstones committed atomically with workspace manifest generation counters so that deleted documents cannot be resurrected by ongoing publications or retrieved from caches.
* As a retrieval engineer, I want cache keys to incorporate workspace identity, principal identity, snapshot identifier, and policy versions while preserving exact literal query casing, exact whitespace in quoted literals, and null session identities so that cache hits never leak data across tenants or stale access boundaries.
* As an execution engineer, I want cached query delivery gates to verify that no supporting source has been deleted and that all source versions and artifacts strictly match pinned snapshot bindings, ensuring that aggregate queries never deliver incomplete calculations silently and recomputing over surviving sources when coverage permits.
* As an infrastructure engineer, I want background pipeline workers to run under an explicit, restricted worker context strictly bound to server loaded jobs, approved operations, and target sources so that asynchronous tasks cannot access foreign data.
* As a search engineer, I want vector search adapters to validate and reject missing or mismatched workspace filters before dispatching requests to Qdrant so that searches never run without tenant boundary filters.
* As a DevOps engineer, I want startup validation to fail fatally if test authentication is configured in production so that insecure test tokens can never be exposed in live environments.
* As a test engineer, I want explicit test principal fixtures and concurrent multi workspace isolation test suites so that boundary enforcement is provably verified before multi user authentication is built.

**Acceptance criteria**:
* **AC-1**: Protected FastAPI endpoints resolve authenticated caller identity exclusively via server dependencies into a frozen `TrustedAuthContext` imported from Specification 0001 (using `authorized_workspace_id`, `principal_id`, `principal_roles`, and `access_policy_version`), rejecting any client attempts to declare authority via query parameters or body payloads.
* **AC-2**: Requests missing valid bearer credentials or presenting unrecognized credentials return HTTP 401 Unauthorized immediately before route handlers execute.
* **AC-3**: Requests attempting to access a workspace or resource belonging to another workspace return HTTP 404 Not Found, preventing tenant enumeration and cross workspace information leakage.
* **AC-4**: Workspace boundary scoping is validated at both API entry dependencies and service or repository methods (raising `WorkspaceAccessDeniedError` on service boundary violations), ensuring direct service invocations maintain isolation.
* **AC-5**: The system provides an explicitly enabled test authentication provider (`KRE_AUTH_MODE="test"`) mapping test bearer tokens to fixed principals (Alice and Bob); workspace ownership and active status are verified dynamically against authoritative workspace records in `workspace_repo` with zero static ownership shortcuts, supporting single user owner access while keeping collaboration deferred. Startup validation explicitly prohibits `KRE_AUTH_MODE="test"` when `ENVIRONMENT="production"`, failing fast with fatal error.
* **AC-6**: Background worker jobs execute under an explicit, restricted worker context (`WorkerExecutionContext` wrapping `TrustedAuthContext` with `principal_id="sa_pipeline_worker"` and `principal_roles=("worker",)`), strictly validated against server loaded, actively running `JobRecord` instances and bounded to that job target `authorized_workspace_id`, `operation`, and optional `(target_source_id, target_source_version)`.
* **AC-7**: Citations in query results and cached answers carry typed `evidence_id: str` and the shared typed `Location` union from Specification 0001, providing exact visual and structural provenance.
* **AC-8**: Document deletion operates on canonical source identity, recording an immutable `TombstoneRecord` in authoritative storage that immediately blocks direct resource endpoints (`GET /sources/{source_id}`), source version lookups, and direct repository queries with HTTP 404 Not Found.
* **AC-9**: Tombstone registration coordinates atomically with the authoritative workspace manifest generation counter (`WorkspaceRecord.manifest_generation`), incrementing the counter in the same storage transaction to fence in flight publications. Publication retries must catch only compare and swap conflicts (`CASConflictError`), rebuild the proposed update against the latest authoritative manifest, validate all sources in the rebuilt publication against the tombstone store, and bind its base manifest generation to the compare and swap expectation before retrying. Publication commits enforce atomic conditions preventing deleted sources from being resurrected.
* **AC-10**: Redis cache keys are computed from a canonical JSON serialization hashing `workspace_id`, `principal_id`, `session_id` (preserving null session identity as `null`), `query_hash` (derived from exact literal query text without collapsing whitespace so quoted literals such as `"A  B"` and `"A B"` produce distinct hashes, combined with sorted selection predicates), `source_version_set_hash` (derived from canonical JSON of sorted source and version pairs), `snapshot_id`, `access_policy_version`, and `model_config_version` using a full 64 character hex SHA256 string.
* **AC-11**: `CachedQueryResult` binds explicit authorization (`authorized_workspace_id`, `authorized_principal_id`, `access_policy_version`) and execution identity (`execution_id`, `snapshot_id`). Delivery gate rechecks caller authorization, workspace tombstone state, source tombstone state, artifact tombstone state, and verifies that dependent source versions and artifacts strictly match the bindings of the authoritative pinned `SnapshotRecord`; if any check fails, the cached entry is invalidated and never delivered.
* **AC-12**: In query delivery evaluation, `workspace_id`, `principal_id`, and `access_policy_version` are validated against `TrustedAuthContext` before any storage or cache access. Cache lookups use the full canonical key from `CacheKeyDescriptor`, and the retrieved entry `cache_key` is validated against that expected descriptor. The original `query` (or `QueryContract`) is preserved and passed to the execution engine for cache miss recomputation. When a cached query is invalidated due to a tombstoned source or snapshot mismatch, the system recomputes the query within the same pinned snapshot over surviving sources only if coverage and query budget permit; if an aggregate query loses a supporting source, it returns an explicit incomplete result with status `INSUFFICIENT_COVERAGE` rather than a silently altered total or an HTTP 404.
* **AC-13**: When the Redis cache is unreachable or encounters connection errors, the system logs a warning and falls back gracefully to authoritative storage with mandatory tombstone verification, returning HTTP 503 only if authoritative storage itself is unreachable.
* **AC-14**: Vector store operations and candidate retrieval enforce mandatory `workspace_id` payload filters in Qdrant. The vector adapter validates and rejects missing or mismatched workspace filters before dispatch (raising `WorkspaceAccessDeniedError`), rather than relying on unfiltered searches returning zero. All candidate matches are rechecked against authoritative tombstones before candidate fusion.
* **AC-15**: Comprehensive isolation regression test suite verifies that Alice cannot view, query, or detect Bob resources across storage repositories, cache lookups, vector adapter dispatches, and query endpoints.

## Options considered

### Option 1: Gateway only validation with asynchronous cache eviction

Validate workspace headers strictly at an external API gateway, passing unvalidated requests to backend services while evicting cache entries asynchronously following deletion events.

**Pros**:
* Keeps backend service handlers lightweight and decoupled from authentication logic.
* Faster deletion response times because cache eviction occurs in the background.

**Cons**:
* Creates an unvalidated internal perimeter where direct service calls lack boundary protection.
* Permits brief time windows where deleted data can be served from cache before eviction completes.
* Relies on external gateway configuration, making local integration testing difficult.

### Option 2: Parameter filtering inside storage repositories only

Allow API endpoints to accept client declared workspace parameters, delegating boundary filtering strictly to SQL and DynamoDB query conditions without shared middleware.

**Pros**:
* Allows flexible endpoint development without configuring shared request dependencies.
* Simple initial implementation for individual endpoints.

**Cons**:
* Trusts client supplied parameters, confusing resource identity with access authority.
* Easy for new endpoints or background tasks to accidentally omit repository boundary filters.
* Fails early rejection requirements by executing business logic before catching permission errors.

### Option 3: Defense in depth with shared dependencies, dual boundary enforcement, atomic manifest generation tombstones, and delivery gates (Chosen)

Enforce authentication and workspace scoping at API entry via shared FastAPI security dependencies, recheck scoping at service and repository boundaries, persist durable tombstones atomically with manifest generation counters, and verify dependent sources during cache delivery gates. (basis: Architecture Bible §3, §46, §47, §65; docs/reference/SECURITY.md)

**Pros**:
* Provides early rejection before route execution while protecting internal service invocations.
* Guarantees zero leakage across workspaces by returning 404 on foreign workspace access.
* Guarantees immediate deletion barriers across direct resource lookups, caches, and historical snapshots.
* Prevents resurrection by linking tombstones directly to workspace publication manifest generation checks, catching only CAS conflicts, and requiring tombstone revalidation on publication retries.
* Uses fixed test identities to establish production interfaces without throwaway code paths.
* Protects asynchronous pipeline jobs through restricted, server loaded worker execution contexts.
* Enforces pre dispatch vector filter validation, eliminating reliance on unfiltered vector queries.

**Cons**:
* Requires defining explicit context objects and checking boundaries across multiple architectural layers.
* Slight latency cost from rechecking authoritative deletion state during delivery gates.

## Decision

**Chosen option**: Option 3: Defense in depth with shared dependencies, dual boundary enforcement, atomic manifest generation tombstones, and delivery gates.

We will enforce workspace authorization and boundary isolation through shared FastAPI security dependencies, dual layer scoping checks, atomic manifest generation tombstones, composite version aware cache keys, delivery gate source rechecks, and restricted worker contexts.

## Rationale

Option 3 satisfies the non negotiable security requirements defined in the Architecture Bible and the project security specification. Relying solely on gateway filtering (Option 1) or repository filtering (Option 2) introduces critical single points of failure where a forgotten filter or a delayed cache eviction can leak enterprise data across tenants.

By validating credentials at API entry and injecting a frozen `TrustedAuthContext`, the engine ensures that client parameters never dictate access authority. Tokens identify principals, while workspace ownership is verified dynamically server side against the requested workspace record. Returning 404 Not Found on cross workspace requests prevents malicious actors from discovering resource identifiers through error code differences. Persisting tombstones atomically with workspace manifest generation counters guarantees that concurrent publications cannot resurrect deleted data. Rechecking dependent source tombstones during cache delivery prevents stale or incomplete answers from reaching users, while restricted worker contexts ensure background workers cannot bypass tenant boundaries.

## Feature design

**Data model sketch**:

```python
from datetime import datetime
from typing import Any, Literal
import json
import hashlib
from pydantic import BaseModel, ConfigDict, Field
from backend.src.schemas.contracts.envelope import TrustedAuthContext
from backend.src.schemas.contracts.location import Location

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
            updated_at=datetime.utcnow().isoformat() + "Z",
        )

    def transition_to_tombstoned(self) -> "WorkspaceRecord":
        return WorkspaceRecord(
            workspace_id=self.workspace_id,
            name=self.name,
            owner_principal_id=self.owner_principal_id,
            manifest_generation=self.manifest_generation + 1,
            status="tombstoned",
            created_at=self.created_at,
            updated_at=datetime.utcnow().isoformat() + "Z",
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
```

**Publication compare and swap and retry contracts**:

```python
class PublicationManifestRecord(BaseModel):
    """Authoritative snapshot publication manifest."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    workspace_id: str
    manifest_generation: int
    active_source_ids: tuple[str, ...]
    updated_at: str

def commit_publication_with_cas(
    workspace_id: str,
    expected_manifest_generation: int,
    manifest_delta: dict[str, Any],
    table_store: Any,
) -> PublicationManifestRecord:
    """Commit publication manifest using compare and swap on manifest generation.
    
    Atomic commit condition:
    manifest_generation == expected_manifest_generation AND status == 'active'.
    Raises CASConflictError if generation changed due to concurrent publication or deletion.
    """
    return table_store.cas_update_manifest(
        workspace_id=workspace_id,
        expected_generation=expected_manifest_generation,
        delta=manifest_delta,
    )

def retry_publication_with_tombstone_revalidation(
    workspace_id: str,
    staged_source_ids: tuple[str, ...],
    manifest_builder_func: Any,
    table_store: Any,
    max_retries: int = 3,
) -> PublicationManifestRecord:
    """Retry publication after CAS conflict, rebuilding update and revalidating all sources in publication."""
    for attempt in range(max_retries):
        current_workspace = table_store.get_workspace(workspace_id)
        if not current_workspace or current_workspace.status == "tombstoned":
            raise FileNotFoundError("Workspace tombstoned during publication retry")

        base_manifest_generation = current_workspace.manifest_generation

        # Rebuild proposed update delta against latest authoritative manifest
        latest_manifest = table_store.get_manifest(workspace_id)
        rebuilt_delta = manifest_builder_func(latest_manifest, staged_source_ids)

        # Validate all sources in the rebuilt publication against the tombstone store
        all_rebuilt_source_ids = rebuilt_delta.get("active_source_ids", staged_source_ids)
        for source_id in all_rebuilt_source_ids:
            if table_store.has_tombstone(workspace_id, "source", source_id):
                raise ValueError(f"Cannot publish manifest containing tombstoned source {source_id}")

        try:
            # Bind base manifest generation to the CAS expectation
            return commit_publication_with_cas(
                workspace_id=workspace_id,
                expected_manifest_generation=base_manifest_generation,
                manifest_delta=rebuilt_delta,
                table_store=table_store,
            )
        except CASConflictError:
            # Catch ONLY compare and swap generation conflicts
            if attempt == max_retries - 1:
                raise RuntimeError("Publication retry budget exhausted due to repeated CAS conflicts")
            continue
    raise RuntimeError("Publication retry budget exhausted")
```

**Authentication, startup validation, and worker context resolution methods**:

```python
def validate_auth_configuration(environment: str, auth_mode: str) -> None:
    """Explicitly prohibit test authentication mode in production environments."""
    if environment.lower() == "production" and auth_mode.lower() == "test":
        raise RuntimeError(
            "FATAL CONFIGURATION ERROR: KRE_AUTH_MODE="test" is strictly prohibited when ENVIRONMENT="production". "
            "Production deployments must use verified identity providers."
        )

class TestAuthRegistry:
    """Server side registry mapping test tokens to principals and verifying ownership."""
    
    PRINCIPALS = {
        "test_token_alice": PrincipalRecord(
            principal_id="usr_alice",
            email="alice@example.com",
            display_name="Alice Test",
            created_at="2026-10-08T00:00:00Z",
        ),
        "test_token_bob": PrincipalRecord(
            principal_id="usr_bob",
            email="bob@example.com",
            display_name="Bob Test",
            created_at="2026-10-08T00:00:00Z",
        ),
    }

    @classmethod
    def resolve_caller(cls, token: str, requested_workspace_id: str, workspace_repo: Any) -> TrustedAuthContext:
        principal = cls.PRINCIPALS.get(token)
        if not principal:
            raise PermissionError("Unrecognized bearer token")
        
        # Verify ownership dynamically from authoritative workspace repository
        workspace = workspace_repo.get_workspace(requested_workspace_id)
        if not workspace or workspace.status == "tombstoned":
            raise FileNotFoundError("Workspace not found")

        if workspace.owner_principal_id != principal.principal_id:
            raise FileNotFoundError("Workspace not found or unauthorized")

        return TrustedAuthContext(
            authorized_workspace_id=requested_workspace_id,
            principal_id=principal.principal_id,
            principal_roles=("owner",),
            access_policy_version="v1",
        )

    @classmethod
    def create_worker_context(cls, job_id: str, job_repo: Any) -> WorkerExecutionContext:
        """Create restricted worker context loaded from authoritative server job storage."""
        job = job_repo.get_job(job_id)
        if not job or not job.is_trusted_system_job:
            raise PermissionError("Untrusted job cannot instantiate worker context")
        if job.status != "running":
            raise PermissionError(f"Job {job_id} must be actively running (current status: {job.status})")

        auth = TrustedAuthContext(
            authorized_workspace_id=job.workspace_id,
            principal_id="sa_pipeline_worker",
            principal_roles=("worker",),
            access_policy_version="v1",
        )
        return WorkerExecutionContext(
            job_id=job.job_id,
            authorized_workspace_id=job.workspace_id,
            operation=job.operation,
            target_source_id=job.target_source_id,
            target_source_version=job.target_source_version,
            auth_context=auth,
        )

def dispatch_qdrant_search(
    query_vector: list[float],
    filter_contract: QdrantFilterContract,
    auth_context: TrustedAuthContext,
    client: Any,
) -> list[dict[str, Any]]:
    """Dispatch vector search to Qdrant with pre dispatch filter validation."""
    payload_filter = filter_contract.validate_and_build_search_filter(auth_context.authorized_workspace_id)
    return client.search(
        collection_name="kre_vectors",
        query_vector=query_vector,
        query_filter=payload_filter,
    )
```

**Delivery gate recheck and query recomputation logic**:

```python
class QueryExecutionOutcome(BaseModel):
    """Structured outcome for query execution and delivery gate evaluation."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    status: Literal["SUCCESS", "INSUFFICIENT_COVERAGE"]
    answer: str
    citations: tuple[CitationItem, ...]
    surviving_source_ids: tuple[str, ...]
    tombstoned_source_ids: tuple[str, ...]

def delivery_gate_recheck(
    cached_result: CachedQueryResult,
    auth_context: TrustedAuthContext,
    target_snapshot_id: str,
    table_store: Any,
) -> CachedQueryResult | None:
    """Verify that cached result is authorized, not tombstoned, and strictly consistent with pinned snapshot."""
    # 1. Recheck current caller authorization and scope
    if auth_context.authorized_workspace_id != cached_result.authorized_workspace_id:
        return None
    if auth_context.principal_id != cached_result.authorized_principal_id:
        return None
    if auth_context.access_policy_version != cached_result.access_policy_version:
        return None
    
    # 2. Recheck pinned snapshot identity match
    if target_snapshot_id != cached_result.snapshot_id:
        return None

    # 3. Recheck workspace deletion
    if table_store.has_tombstone(auth_context.authorized_workspace_id, "workspace", auth_context.authorized_workspace_id):
        return None

    # 4. Load authoritative pinned snapshot and verify active status
    snapshot = table_store.get_snapshot(auth_context.authorized_workspace_id, target_snapshot_id)
    if snapshot is None or snapshot.status == "tombstoned":
        return None

    active_versions = snapshot.source_version_map
    bound_artifacts = set(snapshot.artifact_ids)

    # 5. Verify source versions against both tombstones and pinned snapshot bindings
    for source_id, source_version in cached_result.dependent_source_versions:
        # Check tombstone store
        if table_store.has_tombstone(auth_context.authorized_workspace_id, "source", source_id):
            return None
        # Verify exact source version binding in pinned snapshot
        if active_versions.get(source_id) != source_version:
            return None

    # 6. Verify dependent artifacts against both tombstones and pinned snapshot bindings
    for artifact_id in cached_result.dependent_artifact_ids:
        # Check tombstone store
        if table_store.has_tombstone(auth_context.authorized_workspace_id, "artifact", artifact_id):
            return None
        # Verify artifact is bound in pinned snapshot
        if artifact_id not in bound_artifacts:
            return None

    return cached_result

def evaluate_query_delivery_and_recomputation(
    workspace_id: str,
    query_input: str | Any,
    cache_key_descriptor: CacheKeyDescriptor,
    auth_context: TrustedAuthContext,
    cache_store: Any,
    table_store: Any,
    execution_engine: Any,
) -> QueryExecutionOutcome:
    """Reconcile AC-8 deleted resource 404 with AC-12 multi source query recomputation."""
    # 1. Validate requested workspace, descriptor principal, and access policy against TrustedAuthContext
    if workspace_id != auth_context.authorized_workspace_id or cache_key_descriptor.workspace_id != workspace_id:
        raise WorkspaceAccessDeniedError(
            f"Caller {auth_context.principal_id} unauthorized for workspace {workspace_id}"
        )
    if cache_key_descriptor.principal_id != auth_context.principal_id:
        raise WorkspaceAccessDeniedError(
            f"Descriptor principal {cache_key_descriptor.principal_id} does not match caller {auth_context.principal_id}"
        )
    if cache_key_descriptor.access_policy_version != auth_context.access_policy_version:
        raise WorkspaceAccessDeniedError(
            f"Descriptor policy {cache_key_descriptor.access_policy_version} does not match caller policy {auth_context.access_policy_version}"
        )

    # 2. If the workspace itself is tombstoned, direct lookup and query return 404
    if table_store.has_tombstone(workspace_id, "workspace", workspace_id):
        raise FileNotFoundError("Workspace not found")

    # 3. Retrieve cache entry using the full canonical cache key from CacheKeyDescriptor
    expected_cache_key = cache_key_descriptor.to_canonical_key()
    cached = cache_store.get_cached_query_by_key(expected_cache_key)
    if cached is not None:
        # Validate that returned entry key strictly matches expected descriptor key
        if cached.cache_key == expected_cache_key:
            validated = delivery_gate_recheck(
                cached,
                auth_context,
                cache_key_descriptor.snapshot_id,
                table_store,
            )
            if validated is not None:
                return QueryExecutionOutcome(
                    execution_id=validated.execution_id,
                    status="SUCCESS",
                    answer=validated.answer,
                    citations=validated.citations,
                    surviving_source_ids=tuple(s[0] for s in validated.dependent_source_versions),
                    tombstoned_source_ids=(),
                )

    # 4. Recompute over surviving sources in the pinned snapshot, preserving original query input
    return execution_engine.recompute_query(
        workspace_id=workspace_id,
        query=query_input,
        descriptor=cache_key_descriptor,
        table_store=table_store,
    )
```

**State transitions**:

```text
Workspace lifecycle:
  active -> tombstoned (increments manifest_generation, atomic storage transaction; blocks all child sources and queries with 404)

Source lifecycle:
  registered -> published -> tombstoned (increments manifest_generation, atomic storage transaction; blocks direct source endpoints with 404; invalidates cached queries and triggers recomputation)
```

**API surface**:

| Endpoint | Method | Key inputs | Key outputs | Auth | Success status | Key errors |
|---|---|---|---|---|---|---|
| `/api/v1/workspaces` | POST | `name: str` | `workspace_id, name, status` | Bearer (owner) | 201 Created | 400 invalid name, 401 unauthenticated, 503 storage unavailable |
| `/api/v1/workspaces/{workspace_id}` | GET | `workspace_id: str` (path) | `workspace_id, name, status` | Bearer (owner) | 200 OK | 401 unauthenticated, 404 not found or foreign, 503 storage unavailable |
| `/api/v1/workspaces/{workspace_id}/sources` | GET | `workspace_id: str` (path) | `items: list[SourceItem]` | Bearer (owner) | 200 OK | 401 unauthenticated, 404 not found or foreign, 503 storage unavailable |
| `/api/v1/workspaces/{workspace_id}/sources` | POST | `file, title: str` | `source_id, status` | Bearer (owner) | 201 Created | 400 invalid file, 401 unauthenticated, 404 foreign, 503 storage unavailable |
| `/api/v1/workspaces/{workspace_id}/sources/{source_id}` | GET | `workspace_id, source_id` | `source_id, title, status` | Bearer (owner) | 200 OK | 401 unauthenticated, 404 tombstoned or foreign, 503 storage unavailable |
| `/api/v1/workspaces/{workspace_id}/sources/{source_id}` | DELETE | `workspace_id, source_id` | `tombstoned_at, status` | Bearer (owner) | 200 OK | 401 unauthenticated, 404 not found or foreign, 503 storage unavailable |
| `/api/v1/workspaces/{workspace_id}/query` | POST | `query: str, session_id: opt` | `answer, citations, execution_id, status` | Bearer (owner) | 200 OK | 400 invalid query, 401 unauthenticated, 404 foreign workspace, 422 insufficient coverage, 503 storage unavailable |

**Error contracts**:

```python
class ErrorEnvelope(BaseModel):
    """Standard RFC 7807 error envelope format."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None

# Status 400 Bad Request:
# {"type": "urn:kre:error:bad_request", "title": "Bad Request", "status": 400, "detail": "Missing required field name", "instance": "/api/v1/workspaces"}

# Status 401 Unauthorized:
# {"type": "urn:kre:error:unauthorized", "title": "Unauthorized", "status": 401, "detail": "Missing or unrecognized bearer token", "instance": "/api/v1/workspaces"}

# Status 404 Not Found (AC-3, AC-8):
# Returned for foreign workspaces, non existent workspaces, tombstoned workspaces, or direct lookups of tombstoned sources.
# {"type": "urn:kre:error:not_found", "title": "Not Found", "status": 404, "detail": "Resource not found", "instance": "/api/v1/workspaces/ws_alpha/sources/src_123"}

# Status 422 Unprocessable Entity (AC-12 Insufficient Coverage):
# Returned when aggregate query loses supporting source due to deletion and cannot complete calculation.
# {"type": "urn:kre:error:insufficient_coverage", "title": "Insufficient Coverage", "status": 422, "detail": "Aggregate calculation aborted: supporting source was tombstoned", "instance": "/api/v1/workspaces/ws_alpha/query"}

# Status 503 Service Unavailable (AC-13):
# Returned when authoritative storage cannot be reached for mandatory tombstone verification.
# {"type": "urn:kre:error:service_unavailable", "title": "Service Unavailable", "status": 503, "detail": "Authoritative storage unreachable for verification", "instance": "/api/v1/workspaces/ws_alpha/query"}
```

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| Resolve auth context | `TrustedAuthContext.principal_id` | Injected by server security dependency from bearer token registry |
| Resolve auth context | `TrustedAuthContext.authorized_workspace_id` | Injected by server dependency after verifying workspace ownership |
| Resolve auth context | `TrustedAuthContext.principal_roles` | Derived as `("owner",)` for verified workspace owner |
| Resolve auth context | `TrustedAuthContext.access_policy_version` | Injected from server configuration setting (default `"v1"`) |
| Resolve worker context | `TrustedAuthContext.principal_roles` | Assigned as `("worker",)` with principal `sa_pipeline_worker` |
| Resolve worker context | `WorkerExecutionContext.job_id` | Server loaded from authoritative `JobRecord` in storage |
| Resolve worker context | `WorkerExecutionContext.operation` | Server loaded from authoritative `JobRecord.operation` |
| Validate workspace access | Status 404 on mismatch | Compared `path.workspace_id` against `TrustedAuthContext.authorized_workspace_id` |
| Create workspace | `WorkspaceRecord.workspace_id` | Server generated UUID with `ws_` prefix |
| Create workspace | `WorkspaceRecord.owner_principal_id` | Derived from `TrustedAuthContext.principal_id` |
| Create workspace | `WorkspaceRecord.manifest_generation` | Initialized to 1 |
| Record tombstone | `TombstoneRecord.tombstoned_at` | Server generated UTC ISO timestamp |
| Record tombstone | `TombstoneRecord.deleted_by_principal_id` | Derived from `TrustedAuthContext.principal_id` |
| Record tombstone | `TombstoneRecord.manifest_generation` | Copied from incremented `WorkspaceRecord.manifest_generation` |
| Atomically increment generation | `WorkspaceRecord.manifest_generation` | Incremented by 1 in the tombstone atomic storage transaction |
| Compute query hash | `CacheKeyDescriptor.query_hash` | SHA256 hex digest of exact literal query text and sorted predicates |
| Compute source version hash | `CacheKeyDescriptor.source_version_set_hash` | SHA256 of canonical JSON array of sorted source and version objects |
| Resolve snapshot identifier | `CacheKeyDescriptor.snapshot_id` | Active snapshot identifier resolved from snapshot registry |
| Resolve model config version | `CacheKeyDescriptor.model_config_version` | Active retrieval and model configuration version string |
| Build cached query result | `CachedQueryResult.dependent_source_versions` | Extracted from citations and requirement evidence bindings |
| Delivery gate check | Cached answer or miss fallback | Verification against `SnapshotRecord` bindings and `TableStore` tombstones |

**Key invariants**:
* Client authority rejection: Supplying a workspace or principal parameter in request bodies or query strings grants zero access authority.
* Strict tenant privacy: Requests attempting to read, update, or delete a resource in another workspace return HTTP 404 Not Found, never 403 or 400.
* Owner only access: For this foundation slice, each workspace is owned by a single principal, and only that owner can access its resources.
* Deletion barrier immediacy: Once a `TombstoneRecord` is written, all subsequent repository lookups, direct source endpoints, and delivery gates treat the resource as absent (returning 404 for direct lookups).
* Resurrect prevention: Writing a tombstone and incrementing `WorkspaceRecord.manifest_generation` occur in a single atomic transaction. In flight CAS publications expecting the previous manifest generation fail immediately.
* Publication retry tombstone revalidation: Publication retries catching `CASConflictError` must rebuild their updates against the latest manifest and revalidate all affected sources against the tombstone store; tombstoned sources cannot be republished.
* Delivery gate recheck: Even on a cache hit, the final delivery pipeline must recheck current caller authorization, authoritative tombstone state, and verify that dependent source versions and artifacts strictly match the pinned `SnapshotRecord`.
* Cache invalidation and recomputation reconciliation: If a dependent source of a cached query is tombstoned or snapshot bindings change, the cache entry is invalidated. Direct requests for that source return 404 (AC-8). However, query endpoints against the active workspace do not return 404 (AC-12); they recompute over surviving documents only if coverage and query budgets permit. If an aggregate calculation strictly depended on the deleted source, it emits an explicit insufficient result with status `INSUFFICIENT_COVERAGE` rather than a silently altered total.
* Pre dispatch vector boundary validation: Vector queries enforce mandatory `workspace_id` payload filters. The vector store adapter validates and rejects missing or mismatched workspace filters before dispatch, raising `WorkspaceAccessDeniedError`.
* Fail closed verification: If authoritative tombstone state cannot be verified due to database errors, the request fails with 503 rather than serving unverified data.
* Service boundary enforcement: Direct invocation of service or repository methods with mismatched workspace context raises `WorkspaceAccessDeniedError`.
* Restricted worker scoping: Background worker jobs execute with an explicit `WorkerExecutionContext` scoped strictly to server loaded, active `JobRecord` instances and cannot execute unapproved operations or cross workspace actions.
* Preserved exact query and session semantics: Cache key hashing preserves exact literal query text including exact internal whitespace in quoted literals, and preserves null session identity as `null` in canonical JSON.
* Production authentication barrier: Configuring test authentication mode in production raises a fatal runtime error at server startup.

**Security model**:
* Authentication mechanism: Bearer token authorization header resolved server side against a test identity registry when `KRE_AUTH_MODE="test"`.
* Test identities: Fixed test fixtures (`test_token_alice` mapped to Alice and workspace alpha; `test_token_bob` mapped to Bob and workspace beta). Alice can create additional workspaces and is verified as owner server side.
* Internal pipeline worker identity: Asynchronous background pipeline jobs operate with a dedicated system context (`sa_pipeline_worker`) with role `worker`.
* Production startup barrier: If `ENVIRONMENT="production"` and `KRE_AUTH_MODE="test"`, the application raises a fatal `RuntimeError` at startup, blocking accidental deployment of test credentials.
* Isolation boundary: Workspace scoping enforced at both the API routing layer, within repository queries, and in vector search adapter dispatchers.
* Multi tenant defense: Resource identifiers are strictly private to their owning workspace; foreign lookups return 404 to block enumeration.

**Configuration required**:
* `KRE_AUTH_MODE`: string setting defaulting to `test` in development (accepts test bearer tokens).
* `ENVIRONMENT`: string setting (`development`, `test`, `production`). Prohibits `KRE_AUTH_MODE="test"` when set to `production`.
* `KRE_REDIS_URL`: connection string for Redis cache (e.g. `redis://localhost:6379/0`).
* `KRE_WORKSPACE_REPO_BACKEND`: storage backend selection (`sqlite` for local development, `dynamodb` for production and integration tests).
* `KRE_QDRANT_URL`: connection string for vector database (e.g. `http://localhost:6333`).

**Critical test scenarios**:
* Happy path: Alice authenticates with `test_token_alice`, creates a source in workspace alpha, and queries it successfully, verifies **AC-1**, **AC-4**, **AC-5**.
* Production startup barrier negative test: Setting `ENVIRONMENT="production"` and `KRE_AUTH_MODE="test"` triggers a fatal `RuntimeError` at startup, verifies **AC-5**.
* Unauthenticated rejection negative test: Request without an authorization header returns HTTP 401 Unauthorized immediately, verifies **AC-2**.
* Cross workspace access defense negative test: Alice authenticates with `test_token_alice` and attempts to read, delete, or query Bob workspace beta, receiving HTTP 404 Not Found, verifies **AC-3**, **AC-15**.
* Direct service boundary check negative test: Direct invocation of repository methods with mismatched workspace credentials raises `WorkspaceAccessDeniedError`, verifies **AC-4**.
* Dynamic workspace ownership: Alice creates a new workspace `ws_test1` via `POST /api/v1/workspaces` (201 Created) and successfully queries it, while Bob attempting to query `ws_test1` receives HTTP 404, verifies **AC-1**, **AC-3**, **AC-5**.
* Restricted worker context check: Worker executing with server loaded `WorkerExecutionContext` for workspace alpha successfully reads alpha sources within approved operation scope, but raises `WorkspaceAccessDeniedError` when attempting an unapproved operation or accessing workspace beta, verifies **AC-6**.
* Untrusted worker creation rejection negative test: Calling `create_worker_context` with an untrusted job or non running job raises `PermissionError`, verifies **AC-6**.
* Typed citation locator check: Query answer citations deserialize with valid `evidence_id` and typed `Location` union, preserving 64 bit float precision, verifies **AC-7**.
* Immediate direct source deletion barrier: Alice deletes a source, receiving HTTP 200 with tombstone metadata; subsequent direct requests for that source return HTTP 404 immediately, verifies **AC-8**.
* Idempotent source deletion: Deleting an already tombstoned source returns HTTP 200 with existing tombstone metadata without altering manifest generation, verifies **AC-8**.
* Atomic publication abortion negative test: Deleting a source during active ingestion increments the workspace manifest generation, causing concurrent CAS publication to fail with `CASConflictError`, verifies **AC-9**.
* Publication retry tombstone revalidation check: Publication retry catching `CASConflictError` detects tombstoned source during mandatory revalidation, rebuilds delta against latest manifest, and aborts without reviving the deleted source, verifies **AC-9**.
* Exact literal query whitespace distinction: Queries with differing internal whitespace in quoted literals (`"A  B"` vs `"A B"`) produce distinct `query_hash` values and distinct cache keys, verifies **AC-10**.
* Canonical JSON source version set hash: Computing `source_version_set_hash` over identical pairs in different orders produces the same canonical hash, and differing versions produce distinct hashes, verifies **AC-10**.
* Null session identity preservation: Cache key descriptor with `session_id=None` serializes `session_id: null` in canonical JSON, remaining distinct from a session with string identifier `"none"`, verifies **AC-10**.
* Cache delivery recheck with snapshot version mismatch: A cached query result whose dependent source version does not match the active pinned `SnapshotRecord` version invalidates the cache hit and triggers recomputation, verifies **AC-11**, **AC-12**.
* Cache delivery recheck with tombstone invalidation: A cached query result whose underlying source is subsequently tombstoned invalidates the cache; subsequent queries recompute over surviving documents or report insufficient coverage, verifies **AC-11**, **AC-12**.
* Incomplete aggregate calculation negative test: Querying an aggregate calculation whose supporting source was tombstoned returns an explicit incomplete result with status `INSUFFICIENT_COVERAGE` rather than a partial sum or an HTTP 404, verifies **AC-12**.
* Graceful cache fallback: Stopping Redis causes queries to fall back to authoritative storage with intact tombstone enforcement, verifies **AC-13**.
* Authoritative store failure negative test: Inducing a database connection error during tombstone verification returns HTTP 503 rather than serving unverified data, verifies **AC-13**.
* Qdrant pre dispatch rejection negative test: Vector search adapter rejects missing or mismatched workspace filter before dispatching, raising `WorkspaceAccessDeniedError`, verifies **AC-14**.
* Comprehensive cross workspace isolation suite: End to end test executing concurrent interleaved queries across Alice and Bob workspaces, proving zero data or metadata leakage, verifies **AC-15**.

## Build plan

Following the project Tracer Bullet build approach, this slice is implemented as a thin vertical thread connecting API entry, dependency injection, service boundaries, storage repositories, and caching:

1. Define core authorization, tombstone, snapshot, and cache models (`WorkspaceRecord`, `PrincipalRecord`, `SnapshotRecord`, `JobRecord`, `WorkerExecutionContext`, `TombstoneRecord`, `SourceItem`, `CitationItem`, `CachedQueryResult`, `CacheKeyDescriptor`, `QdrantFilterContract`, `ErrorEnvelope`, `QueryExecutionOutcome`), satisfies **AC-1**, **AC-5**, **AC-6**, **AC-7**, **AC-8**, **AC-10**, **AC-11**, **AC-14**.
2. Implement server side test authentication dependency resolving test bearer tokens into `TrustedAuthContext` and verifying dynamic workspace ownership against authoritative storage, with startup barrier prohibiting test auth in production, satisfies **AC-1**, **AC-2**, **AC-5**.
3. Implement API dependency and router guards enforcing workspace ownership and returning HTTP 404 on cross workspace access, satisfies **AC-3**.
4. Implement dual boundary enforcement in repository queries and service methods with `WorkspaceAccessDeniedError` and restricted server loaded worker context, satisfies **AC-4**, **AC-6**.
5. Implement atomic tombstone persistence and manifest generation counter increments in TableStore (SQLite and DynamoDB) to fence concurrent publications, and publication retry tombstone revalidation catching only `CASConflictError`, satisfies **AC-8**, **AC-9**.
6. Implement composite cache key generation and Redis caching with delivery gate tombstone and snapshot binding rechecks, query recomputation over surviving sources, insufficient coverage handling, and graceful store fallback, satisfies **AC-10**, **AC-11**, **AC-12**, **AC-13**.
7. Implement automated cross workspace leakage and boundary isolation test suites covering API routes, service methods, vector adapter pre dispatch checks, and cache isolation, satisfies **AC-3**, **AC-14**, **AC-15**.

## Consequences

**Positive**:
* Establishes provable tenant isolation before downstream ingestion and retrieval pipelines are built.
* Eliminates ambient authorization parameters, enforcing that credentials alone grant access.
* Guarantees that document deletion acts as an immediate, irreversible barrier across all direct query paths.
* Prevents resurrecting deleted documents by binding tombstones directly to publication manifest generation checks and enforcing retry revalidation.
* Reconciles direct 404 deletions with multi source query recomputation and explicit insufficient coverage reporting.
* Prevents cache poisoning and cross tenant data exposure through cryptographic composite cache keys preserving exact literal query semantics.
* Protects background jobs through explicit, server loaded worker execution context scoping.
* Prevents accidental deployment of insecure test authentication in production environments through startup validation.
* Rejects vector searches with invalid tenant filters before query dispatch.

**Negative**:
* Dual layer boundary checks introduce slight validation overhead on internal service calls.
* Rechecking tombstone and snapshot binding state on cache hits requires a fast authoritative store lookup, slightly reducing pure cache latency benefits.
* Team collaboration and granular role management are deferred until production authentication is implemented in Slice 8.

## Next steps

* Connect `WorkspaceRecord` and `TrustedAuthContext` to the snapshot registry publication workflow in Foundation Feature 3.
* Connect ingestion file registration in Slice 2 to canonical source identities and tombstone checks.
* Implement production authentication provider swap and multi user workspace membership in Slice 8 (Feature 11).

## References

### Project sources
* `KRE_ARCHITECTURE_BIBLE_REFACTOR_READY_2026-10-06.md`: §3 Data Ownership, §46 Version Aware Cache, §47 Replacement, Deletion and Concurrent Publication, §65 Security / Authorization Boundary
* `docs/reference/SECURITY.md`: Authentication Authority vs Request Context, Mandatory Final Delivery and Tombstone Barriers
* `docs/reference/TESTING.md`: Required Contract Tests, Cache Isolation, Adversarial Cases
* `docs/specs/0001-contracts-and-identity-envelope.md`: IdentityEnvelope, TrustedAuthContext, Location union
* `docs/scope/scope.md`: Foundation Feature 2 Workspace authorization and boundary isolation

### Practices and standards
* Defense in depth for multi tenant web services
* Cryptographic composite cache key derivation
* Immediate deletion barriers and tombstone enforcement
* Fail closed security boundary design
