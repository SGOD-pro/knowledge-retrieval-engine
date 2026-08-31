import logging
import uuid
from fastapi import HTTPException
from modules.chat.chat_repository import ChatRepository
from schemas.models import (
    ChatSessionSchema,
    ChatMessageSchema,
    CreateChatSessionRequest,
    SaveChatMessageRequest,
)

logger = logging.getLogger(__name__)


class ChatService:
    """Service managing workspace-scoped chat sessions and message history."""

    def __init__(self, repo: ChatRepository | None = None):
        self.repo = repo or ChatRepository()

    def get_sessions(self, workspace_id: str) -> list[dict]:
        return self.repo.get_workspace_sessions(workspace_id)

    def create_session(
        self, workspace_id: str, req: CreateChatSessionRequest | None = None
    ) -> dict:
        session_id = f"session_{uuid.uuid4().hex[:12]}"
        title = req.title if req and req.title else "New Query Session"
        return self.repo.create_chat_session(
            workspace_id=workspace_id, session_id=session_id, title=title
        )

    def get_session(self, workspace_id: str, session_id: str) -> dict:
        session = self.repo.get_chat_session(workspace_id=workspace_id, session_id=session_id)
        if not session:
            # If not found, create a new one automatically for resilience
            return self.repo.create_chat_session(
                workspace_id=workspace_id, session_id=session_id, title="New Query Session"
            )
        return session

    def save_message(
        self, workspace_id: str, session_id: str, req: SaveChatMessageRequest
    ) -> dict:
        msg_dict = req.model_dump()
        return self.repo.save_chat_message(
            workspace_id=workspace_id, session_id=session_id, message=msg_dict
        )

    def delete_session(self, workspace_id: str, session_id: str) -> dict:
        success = self.repo.delete_chat_session(
            workspace_id=workspace_id, session_id=session_id
        )
        if not success:
            raise HTTPException(404, f"Session '{session_id}' not found")
        return {"status": "deleted", "session_id": session_id}


chat_service = ChatService()
