from fastapi import HTTPException
from db.database import CloudRepository
from schemas.models import CreateWorkspaceRequest, Workspace


class WorkspacesService:
    """Service handling workspace management, isolation, and scoped documents."""

    def __init__(self, repo: CloudRepository | None = None):
        self.repo = repo or CloudRepository()

    def get_workspaces(self) -> list[Workspace]:
        return self.repo.get_workspaces()

    def create_workspace(self, req: CreateWorkspaceRequest) -> Workspace:
        return self.repo.create_workspace(
            name=req.name, industry=req.industry, description=req.description
        )

    def delete_workspace(self, workspace_id: str) -> dict:
        success = self.repo.delete_workspace(workspace_id)
        if not success:
            raise HTTPException(404, f"Workspace '{workspace_id}' not found")
        return {"status": "deleted", "workspace_id": workspace_id}

    def get_workspace_documents(
        self, workspace_id: str, page: int = 1, limit: int = 10
    ) -> dict:
        return self.repo.get_workspace_documents(
            workspace_id=workspace_id, page=page, limit=limit
        )


workspaces_service = WorkspacesService()
