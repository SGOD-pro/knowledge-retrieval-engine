"""Security, authentication, and worker context resolution.

Implements server side authentication dependency, fixed test identity registry,
dynamic workspace ownership verification, publication compare and swap helpers,
and restricted background worker context instantiation.
"""

import os
import sqlite3
from typing import Any
from fastapi import Header, HTTPException, Request, status

from config import settings
from src.schemas.contracts.boundary import (
    CASConflictError,
    JobRecord,
    PrincipalRecord,
    PublicationManifestRecord,
    WorkerExecutionContext,
    WorkspaceRecord,
)
from src.schemas.contracts.envelope import TrustedAuthContext
from src.db.workspace_boundary_repo import workspace_boundary_repo


def validate_auth_configuration(environment: str, auth_mode: str) -> None:
    """Explicitly prohibit test authentication mode in production environments."""
    if environment.lower() in ("production", "prod") and auth_mode.lower() == "test":
        raise RuntimeError(
            'FATAL CONFIGURATION ERROR: KRE_AUTH_MODE="test" is strictly prohibited when ENVIRONMENT="production". '
            'Production deployments must use verified identity providers.'
        )


class TestAuthRegistry:
    """Server side registry mapping test tokens to principals and verifying ownership."""

    PRINCIPALS: dict[str, PrincipalRecord] = {
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
    def resolve_caller(
        cls,
        token: str,
        requested_workspace_id: str,
        workspace_repo: Any,
    ) -> TrustedAuthContext:
        """Resolve authenticated caller and dynamically verify workspace ownership."""
        principal = cls.PRINCIPALS.get(token)
        if not principal:
            raise PermissionError("Unrecognized bearer token")

        # Verify ownership dynamically from authoritative workspace repository
        workspace = workspace_repo.get_workspace(requested_workspace_id)
        if not workspace or workspace.status == "tombstoned":
            raise FileNotFoundError("Workspace not found")

        if workspace.owner_principal_id != principal.principal_id:
            # Mask foreign workspace existence to prevent tenant enumeration
            raise FileNotFoundError("Workspace not found")

        return TrustedAuthContext(
            authorized_workspace_id=requested_workspace_id,
            principal_id=principal.principal_id,
            principal_roles=("owner",),
            access_policy_version="v1",
        )

    @classmethod
    def resolve_token_principal(cls, token: str) -> PrincipalRecord:
        """Resolve principal record from token without workspace scoping."""
        principal = cls.PRINCIPALS.get(token)
        if not principal:
            raise PermissionError("Unrecognized bearer token")
        return principal

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


def extract_bearer_token(authorization: str | None) -> str:
    """Extract and validate bearer token from Authorization header."""
    if not authorization or not authorization.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization scheme; Bearer token required",
        )
    return parts[1]


async def require_auth_context(
    request: Request,
    authorization: str | None = Header(None),
) -> TrustedAuthContext:
    """FastAPI security dependency resolving caller identity and enforcing workspace scope."""
    token = extract_bearer_token(authorization)
    workspace_id = request.path_params.get("workspace_id")

    if workspace_id:
        try:
            return TestAuthRegistry.resolve_caller(
                token=token,
                requested_workspace_id=workspace_id,
                workspace_repo=workspace_boundary_repo,
            )
        except PermissionError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or unrecognized bearer token",
            )
        except FileNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Resource not found",
            )
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authoritative storage unreachable",
            )
    else:
        # Non-workspace endpoint (e.g. POST /workspaces)
        try:
            principal = TestAuthRegistry.resolve_token_principal(token)
            return TrustedAuthContext(
                authorized_workspace_id="unscoped",
                principal_id=principal.principal_id,
                principal_roles=("owner",),
                access_policy_version="v1",
            )
        except PermissionError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or unrecognized bearer token",
            )
