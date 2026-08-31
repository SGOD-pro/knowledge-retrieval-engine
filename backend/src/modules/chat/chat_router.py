from fastapi import APIRouter
from modules.chat.chat_service import chat_service
from schemas.models import (
    ChatSessionSchema,
    ChatMessageSchema,
    CreateChatSessionRequest,
    SaveChatMessageRequest,
)

router = APIRouter(prefix="/workspaces", tags=["chat"])


@router.get("/{workspace_id}/sessions")
def get_workspace_sessions_endpoint(workspace_id: str):
    return chat_service.get_sessions(workspace_id)


@router.post("/{workspace_id}/sessions")
def create_workspace_session_endpoint(
    workspace_id: str, req: CreateChatSessionRequest | None = None
):
    return chat_service.create_session(workspace_id, req)


@router.get("/{workspace_id}/sessions/{session_id}")
def get_chat_session_endpoint(workspace_id: str, session_id: str):
    return chat_service.get_session(workspace_id, session_id)


@router.post("/{workspace_id}/sessions/{session_id}/messages")
def save_chat_message_endpoint(
    workspace_id: str, session_id: str, req: SaveChatMessageRequest
):
    return chat_service.save_message(workspace_id, session_id, req)


@router.delete("/{workspace_id}/sessions/{session_id}")
def delete_chat_session_endpoint(workspace_id: str, session_id: str):
    return chat_service.delete_session(workspace_id, session_id)
