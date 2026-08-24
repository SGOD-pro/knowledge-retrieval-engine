from fastapi import APIRouter, Query, status
from schemas.models import CreateWorkspaceRequest
from modules.workspaces.workspaces_service import workspaces_service

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("")
def get_workspaces_endpoint():
    workspaces = workspaces_service.get_workspaces()
    return {"workspaces": workspaces}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_workspace_endpoint(req: CreateWorkspaceRequest):
    return workspaces_service.create_workspace(req)


@router.delete("/{workspace_id}", status_code=status.HTTP_200_OK)
def delete_workspace_endpoint(workspace_id: str):
    return workspaces_service.delete_workspace(workspace_id)


@router.get("/{workspace_id}/documents")
def get_workspace_documents_endpoint(
    workspace_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
):
    return workspaces_service.get_workspace_documents(
        workspace_id=workspace_id, page=page, limit=limit
    )
