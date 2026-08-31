from modules.graph.graph_repository import GraphRepository


class GraphService:
    """Service handling Open Knowledge Graph (OKF) visual structure retrieval."""

    def __init__(self, repo: GraphRepository | None = None):
        self.repo = repo or GraphRepository()

    def get_workspace_graph(self, workspace_id: str | None) -> dict:
        return self.repo.get_workspace_graph(workspace_id)


graph_service = GraphService()
