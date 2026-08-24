from db.database import CloudRepository


class GraphService:
    """Service handling Open Knowledge Graph (OKF) visual structure retrieval."""

    def __init__(self, repo: CloudRepository | None = None):
        self.repo = repo or CloudRepository()

    def get_workspace_graph(self, workspace_id: str | None) -> dict:
        return self.repo.get_workspace_graph(workspace_id)


graph_service = GraphService()
