import pytest
import time
from fastapi.testclient import TestClient
from db.database import CloudRepository
from main import app
from schemas.models import SaveChatMessageRequest, CreateChatSessionRequest

client = TestClient(app)


def test_chat_session_crud_and_persistence():
    repo = CloudRepository()
    ws_id = f"test_ws_chat_{int(time.time() * 1000)}"
    repo.create_workspace(name="Chat Test Workspace", workspace_id=ws_id)

    # 1. Create a session
    sess = repo.create_chat_session(workspace_id=ws_id, session_id=f"sess_{ws_id}_1", title="Initial Chat")
    assert sess["id"] == f"sess_{ws_id}_1"
    assert sess["workspace_id"] == ws_id
    assert sess["title"] == "Initial Chat"

    # 2. Add messages
    user_msg = repo.save_chat_message(
        workspace_id=ws_id,
        session_id=sess["id"],
        message={
            "id": "msg_user_1",
            "sender": "user",
            "text": "What is the capital of France?",
            "timestamp": "10:00 AM",
        },
    )
    assert user_msg["id"] == "msg_user_1"
    assert user_msg["sender"] == "user"

    ai_msg = repo.save_chat_message(
        workspace_id=ws_id,
        session_id=sess["id"],
        message={
            "id": "msg_ai_1",
            "sender": "assistant",
            "text": "Paris is the capital of France.",
            "timestamp": "10:01 AM",
            "confidence": 0.95,
            "latency_ms": 340.5,
            "faithfulness": 99.59,
            "citations": [{"chunk_id": "c1", "document_id": "d1", "text": "Paris"}],
        },
    )
    assert ai_msg["id"] == "msg_ai_1"
    assert ai_msg["sender"] == "assistant"
    assert ai_msg["confidence"] == 0.95

    # 3. Retrieve session with messages
    fetched_sess = repo.get_chat_session(workspace_id=ws_id, session_id=sess["id"])
    assert fetched_sess is not None
    assert fetched_sess["id"] == sess["id"]
    assert len(fetched_sess["messages"]) == 2
    assert fetched_sess["messages"][0]["text"] == "What is the capital of France?"
    assert fetched_sess["messages"][1]["text"] == "Paris is the capital of France."

    # 4. List workspace sessions
    sessions_list = repo.get_workspace_sessions(workspace_id=ws_id)
    assert len(sessions_list) == 1
    assert sessions_list[0]["id"] == sess["id"]


def test_chat_cascade_deletion_on_workspace_delete():
    repo = CloudRepository()
    ws_id = f"test_ws_cascade_{int(time.time() * 1000)}"
    repo.create_workspace(name="Cascade Workspace", workspace_id=ws_id)

    sess = repo.create_chat_session(workspace_id=ws_id, session_id=f"sess_cas_{ws_id}", title="To Be Deleted")
    repo.save_chat_message(
        workspace_id=ws_id,
        session_id=sess["id"],
        message={"id": "m1", "sender": "user", "text": "Hello world"},
    )

    # Verify session exists
    assert repo.get_chat_session(workspace_id=ws_id, session_id=sess["id"]) is not None

    # Delete workspace
    delete_success = repo.delete_workspace(ws_id)
    assert delete_success is True

    # Verify chat session and messages are deleted
    assert repo.get_chat_session(workspace_id=ws_id, session_id=sess["id"]) is None
    assert len(repo.get_workspace_sessions(ws_id)) == 0


def test_chat_api_endpoints():
    ws_id = f"test_ws_api_{int(time.time() * 1000)}"
    client.post("/api/v1/workspaces", json={"name": "API Chat Workspace"})

    # 1. Create session via POST /api/v1/workspaces/{ws}/sessions
    create_resp = client.post(f"/api/v1/workspaces/{ws_id}/sessions", json={"title": "My API Session"})
    assert create_resp.status_code == 200
    sess_data = create_resp.json()
    sess_id = sess_data["id"]
    assert sess_data["title"] == "My API Session"

    # 2. Post message via POST /api/v1/workspaces/{ws}/sessions/{id}/messages
    msg_resp = client.post(
        f"/api/v1/workspaces/{ws_id}/sessions/{sess_id}/messages",
        json={"sender": "user", "text": "Explain quantum computing"},
    )
    assert msg_resp.status_code == 200
    assert msg_resp.json()["text"] == "Explain quantum computing"

    # 3. Get session via GET /api/v1/workspaces/{ws}/sessions/{id}
    get_resp = client.get(f"/api/v1/workspaces/{ws_id}/sessions/{sess_id}")
    assert get_resp.status_code == 200
    assert len(get_resp.json()["messages"]) == 1

    # 4. List sessions via GET /api/v1/workspaces/{ws}/sessions
    list_resp = client.get(f"/api/v1/workspaces/{ws_id}/sessions")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) >= 1

    # 5. Delete session via DELETE /api/v1/workspaces/{ws}/sessions/{id}
    del_resp = client.delete(f"/api/v1/workspaces/{ws_id}/sessions/{sess_id}")
    assert del_resp.status_code == 200

    # Verify deleted
    get_after = client.get(f"/api/v1/workspaces/{ws_id}/sessions/{sess_id}")
    # Automatic fallback creates empty session or 404
    assert len(get_after.json()["messages"]) == 0
