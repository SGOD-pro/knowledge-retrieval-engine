from fastapi import APIRouter

from db.database import CloudRepository
from ingestion.parse_service import ingest_document
from modules.auth import auth_router, auth_service
from modules.documents import documents_router, documents_service
from modules.graph import graph_router, graph_service
from modules.query import query_router, query_service
from modules.system import system_router, system_service
from modules.workspaces import workspaces_router, workspaces_service
from modules.chat import chat_router, chat_service

router = APIRouter()

# Mount all modular sub-routers
router.include_router(system_router)
router.include_router(auth_router)
router.include_router(workspaces_router)
router.include_router(chat_router)
router.include_router(documents_router)
router.include_router(query_router)
router.include_router(graph_router)


# Backward-compatibility aliases and re-exports for test suites and scripts
def repository() -> CloudRepository:
    return CloudRepository()


def _background_ingest(*args, **kwargs):
    return documents_service.background_ingest(*args, **kwargs)


from modules.documents.documents_router import (
    upload_workspace_documents_endpoint,
    ingest,
    get_document,
    get_document_file_endpoint,
)
from modules.workspaces.workspaces_router import (
    get_workspaces_endpoint,
    create_workspace_endpoint,
    delete_workspace_endpoint,
    get_workspace_documents_endpoint,
)
from modules.chat.chat_router import (
    get_workspace_sessions_endpoint,
    create_workspace_session_endpoint,
    get_chat_session_endpoint,
    save_chat_message_endpoint,
    delete_chat_session_endpoint,
)
from modules.query.query_router import query_endpoint, query_stream_endpoint
from modules.auth.auth_router import login_endpoint, oauth_login_endpoint
from modules.system.system_router import health_check, get_benchmarks_endpoint
from modules.graph.graph_router import (
    get_workspace_graph_endpoint,
    get_documents_graph_endpoint,
)

__all__ = [
    "router",
    "repository",
    "_background_ingest",
    "upload_workspace_documents_endpoint",
    "ingest",
    "get_document",
    "get_document_file_endpoint",
    "get_workspaces_endpoint",
    "create_workspace_endpoint",
    "delete_workspace_endpoint",
    "get_workspace_documents_endpoint",
    "get_workspace_sessions_endpoint",
    "create_workspace_session_endpoint",
    "get_chat_session_endpoint",
    "save_chat_message_endpoint",
    "delete_chat_session_endpoint",
    "query_endpoint",
    "login_endpoint",
    "oauth_login_endpoint",
    "health_check",
    "get_benchmarks_endpoint",
    "get_workspace_graph_endpoint",
    "get_documents_graph_endpoint",
]
