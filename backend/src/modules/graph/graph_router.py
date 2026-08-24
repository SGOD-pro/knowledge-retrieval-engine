from fastapi import APIRouter
from modules.graph.graph_service import graph_service

router = APIRouter(tags=["graph"])


@router.get("/workspaces/{workspace_id}/graph")
def get_workspace_graph_endpoint(workspace_id: str):
    return graph_service.get_workspace_graph(workspace_id)


@router.get("/documents/graph")
def get_documents_graph_endpoint():
    return graph_service.get_workspace_graph(None)
