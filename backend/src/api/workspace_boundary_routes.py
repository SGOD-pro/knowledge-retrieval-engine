"""API routes for workspace boundaries, source management, and isolated queries.

Enforces server injected authentication dependencies, returns HTTP 404 for foreign
or tombstoned resources, and supports deterministic query delivery gate rechecks.
"""

import logging
import sqlite3
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from src.schemas.contracts.boundary import (
    CacheKeyDescriptor,
    ErrorEnvelope,
    QueryExecutionOutcome,
    SourceItem,
    WorkspaceRecord,
)
from src.schemas.contracts.envelope import TrustedAuthContext
from src.db.workspace_boundary_repo import get_workspace_boundary_repo
from src.security.auth import require_auth_context
from src.services.cache_delivery import (
    ControlledFixtureExecutionEngine,
    InMemoryCacheStore,
    evaluate_query_delivery_and_recomputation,
)

router = APIRouter(prefix="", tags=["workspace-boundary"])

# In-memory cache store for boundary API requests
global_cache_store = InMemoryCacheStore()
global_execution_engine = ControlledFixtureExecutionEngine()


def get_boundary_repo() -> Any:
    """Dependency provider for authoritative workspace boundary repository."""
    try:
        return get_workspace_boundary_repo()
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
        logger.error("Authoritative storage connection failure: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authoritative storage unreachable",
        )


class CreateWorkspacePayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    industry: str | None = None
    description: str | None = None


class CreateSourcePayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    source_id: str | None = None


class WorkspaceQueryPayload(BaseModel):
    query: str = Field(..., min_length=1)
    session_id: str | None = None
    snapshot_id: str = "snap_default"


@router.post(
    "/workspaces",
    status_code=status.HTTP_201_CREATED,
    response_model=dict[str, Any],
)
async def create_workspace_boundary_endpoint(
    request: Request,
    payload: CreateWorkspacePayload,
    auth_context: TrustedAuthContext = Depends(require_auth_context),
    repo: Any = Depends(get_boundary_repo),
):
    """Create a new workspace owned by the authenticated caller.
    
    Missing or invalid credentials return 401 Unauthorized before any mutation (AC-2).
    """
    if not payload.name or not payload.name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workspace name must not be empty",
        )

    try:
        record = repo.create_workspace(
            name=payload.name.strip(),
            owner_principal_id=auth_context.principal_id,
        )
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
        logger.error("Authoritative storage failure during workspace creation: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authoritative storage unreachable",
        )
    return {
        "workspace_id": record.workspace_id,
        "name": record.name,
        "status": record.status,
        "manifest_generation": record.manifest_generation,
    }


@router.get(
    "/workspaces/{workspace_id}",
    status_code=status.HTTP_200_OK,
    response_model=dict[str, Any],
)
async def get_workspace_boundary_endpoint(
    workspace_id: str,
    auth_context: TrustedAuthContext = Depends(require_auth_context),
    repo: Any = Depends(get_boundary_repo),
):
    """Retrieve workspace metadata, returning 404 for foreign or tombstoned workspaces."""
    try:
        record = repo.get_workspace(workspace_id)
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
        logger.error("Authoritative storage failure retrieving workspace: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authoritative storage unreachable",
        )
    if not record or record.status == "tombstoned" or record.owner_principal_id != auth_context.principal_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")

    return {
        "workspace_id": record.workspace_id,
        "name": record.name,
        "status": record.status,
        "manifest_generation": record.manifest_generation,
    }


@router.get(
    "/workspaces/{workspace_id}/sources",
    status_code=status.HTTP_200_OK,
    response_model=dict[str, Any],
)
async def list_workspace_sources_endpoint(
    workspace_id: str,
    auth_context: TrustedAuthContext = Depends(require_auth_context),
    repo: Any = Depends(get_boundary_repo),
):
    """List untombstoned sources in workspace."""
    try:
        sources = repo.list_sources(workspace_id)
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
        logger.error("Authoritative storage failure listing sources: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authoritative storage unreachable",
        )
    return {"items": [s.model_dump(mode="json") for s in sources]}


@router.post(
    "/workspaces/{workspace_id}/sources",
    status_code=status.HTTP_201_CREATED,
    response_model=dict[str, Any],
)
async def create_workspace_source_endpoint(
    workspace_id: str,
    payload: CreateSourcePayload,
    auth_context: TrustedAuthContext = Depends(require_auth_context),
    repo: Any = Depends(get_boundary_repo),
):
    """Register a new source in the authorized workspace."""
    if not payload.title or not payload.title.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source title must not be empty",
        )

    try:
        item = repo.create_source(
            workspace_id=workspace_id,
            title=payload.title.strip(),
            source_id=payload.source_id,
        )
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
        logger.error("Authoritative storage failure creating source: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authoritative storage unreachable",
        )
    return {
        "source_id": item.source_id,
        "title": item.title,
        "status": item.status,
    }


@router.get(
    "/workspaces/{workspace_id}/sources/{source_id}",
    status_code=status.HTTP_200_OK,
    response_model=dict[str, Any],
)
async def get_workspace_source_endpoint(
    workspace_id: str,
    source_id: str,
    auth_context: TrustedAuthContext = Depends(require_auth_context),
    repo: Any = Depends(get_boundary_repo),
):
    """Get source metadata, returning 404 if deleted or foreign (AC-8)."""
    try:
        item = repo.get_source(workspace_id, source_id)
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
        logger.error("Authoritative storage failure getting source: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authoritative storage unreachable",
        )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
    return {
        "source_id": item.source_id,
        "title": item.title,
        "status": item.status,
    }


@router.delete(
    "/workspaces/{workspace_id}/sources/{source_id}",
    status_code=status.HTTP_200_OK,
    response_model=dict[str, Any],
)
async def delete_workspace_source_endpoint(
    workspace_id: str,
    source_id: str,
    auth_context: TrustedAuthContext = Depends(require_auth_context),
    repo: Any = Depends(get_boundary_repo),
):
    """Tombstone source atomically, incrementing manifest generation (AC-8, AC-9)."""
    try:
        tombstone = repo.record_source_tombstone(
            workspace_id=workspace_id,
            source_id=source_id,
            caller_principal_id=auth_context.principal_id,
        )
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
        logger.error("Authoritative storage failure deleting source: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authoritative storage unreachable",
        )
    return {
        "source_id": tombstone.resource_id,
        "status": "tombstoned",
        "tombstoned_at": tombstone.tombstoned_at,
        "manifest_generation": tombstone.manifest_generation,
    }


@router.post(
    "/workspaces/{workspace_id}/query",
    status_code=status.HTTP_200_OK,
    response_model=dict[str, Any],
)
async def query_workspace_boundary_endpoint(
    workspace_id: str,
    payload: WorkspaceQueryPayload,
    auth_context: TrustedAuthContext = Depends(require_auth_context),
    repo: Any = Depends(get_boundary_repo),
):
    """Execute query with delivery gate rechecks, literal query hashing, and recomputation (AC-10, AC-11, AC-12)."""
    # 1. Compute exact literal query hash and descriptor
    query_hash = CacheKeyDescriptor.compute_query_hash(payload.query)
    source_version_hash = CacheKeyDescriptor.compute_source_version_set_hash(())

    descriptor = CacheKeyDescriptor(
        workspace_id=workspace_id,
        principal_id=auth_context.principal_id,
        session_id=payload.session_id,
        query_hash=query_hash,
        source_version_set_hash=source_version_hash,
        snapshot_id=payload.snapshot_id,
        access_policy_version=auth_context.access_policy_version,
        model_config_version="v1",
    )

    try:
        outcome = evaluate_query_delivery_and_recomputation(
            workspace_id=workspace_id,
            query_input=payload.query,
            cache_key_descriptor=descriptor,
            auth_context=auth_context,
            cache_store=global_cache_store,
            table_store=repo,
            execution_engine=global_execution_engine,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
        logger.error("Authoritative storage failure evaluating query: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authoritative storage unreachable for verification",
        )

    if outcome.status == "INSUFFICIENT_COVERAGE":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "type": "urn:kre:error:insufficient_coverage",
                "title": "Insufficient Coverage",
                "status": 422,
                "detail": outcome.answer,
                "instance": f"/api/v1/workspaces/{workspace_id}/query",
            },
        )

    return {
        "execution_id": outcome.execution_id,
        "status": outcome.status,
        "answer": outcome.answer,
        "citations": [c.model_dump(mode="json") for c in outcome.citations],
        "surviving_source_ids": list(outcome.surviving_source_ids),
    }
