import json
import logging
import time
import os
from config import settings

logger = logging.getLogger(__name__)

# In-memory storage for test/local fallback
_WORKSPACE_SESSIONS: dict[str, list[dict]] = {}
_SESSION_MESSAGES: dict[str, list[dict]] = {}


def _is_test_env() -> bool:
    return os.environ.get("ENVIRONMENT") == "test" or settings.ENVIRONMENT == "test"


class ChatRepository:
    """Domain repository for workspace-scoped chat sessions and message persistence in DynamoDB."""

    def __init__(self):
        self.table_name = settings.DYNAMODB_TABLE_NAME
        from aws.infra import get_resource

        self.dynamodb = get_resource("dynamodb")
        self.table = self.dynamodb.Table(self.table_name)

    def get_workspace_sessions(self, workspace_id: str) -> list[dict]:
        """Fetch all chat sessions for a workspace."""
        if _is_test_env():
            return list(_WORKSPACE_SESSIONS.get(workspace_id, []))

        from boto3.dynamodb.conditions import Key

        try:
            response = self.table.query(
                KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{workspace_id}")
                & Key("SK").begins_with("SESSION#")
            )
            items = response.get("Items", [])
            while "LastEvaluatedKey" in response:
                response = self.table.query(
                    KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{workspace_id}")
                    & Key("SK").begins_with("SESSION#"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))

            # Filter for session metadata records only (exclude message records which have #MSG#)
            sessions = []
            for it in items:
                sk = it.get("SK", "")
                if "#MSG#" not in sk:
                    sessions.append(
                        {
                            "id": it.get("id", sk.replace("SESSION#", "")),
                            "workspace_id": workspace_id,
                            "title": it.get("title", "New Query Session"),
                            "category": it.get("category", "Today"),
                            "updated_at": it.get("updated_at", "Just now"),
                            "messages": [],
                        }
                    )
            # Sort newest first
            sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
            return sessions
        except Exception as e:
            logger.warning("DynamoDB get_workspace_sessions failed for %s: %s", workspace_id, e)
            return list(_WORKSPACE_SESSIONS.get(workspace_id, []))

    def create_chat_session(
        self, workspace_id: str, session_id: str, title: str = "New Query Session"
    ) -> dict:
        """Create and store a new chat session."""
        session_entry = {
            "id": session_id,
            "workspace_id": workspace_id,
            "title": title,
            "category": "Today",
            "updated_at": "Just now",
            "messages": [],
        }

        if workspace_id not in _WORKSPACE_SESSIONS:
            _WORKSPACE_SESSIONS[workspace_id] = []
        _WORKSPACE_SESSIONS[workspace_id].insert(0, session_entry)
        _SESSION_MESSAGES[session_id] = []

        if not _is_test_env():
            try:
                item = {
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": f"SESSION#{session_id}",
                    "id": session_id,
                    "workspace_id": workspace_id,
                    "title": title,
                    "category": "Today",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                self.table.put_item(Item=item)
            except Exception as e:
                logger.warning("DynamoDB create_chat_session failed for %s: %s", session_id, e)

        return session_entry

    def get_chat_session(self, workspace_id: str, session_id: str) -> dict | None:
        """Retrieve a session with all its messages."""
        if _is_test_env():
            sessions = _WORKSPACE_SESSIONS.get(workspace_id, [])
            sess = next((s for s in sessions if s.get("id") == session_id), None)
            if sess is None:
                return None
            sess_copy = dict(sess)
            sess_copy["messages"] = list(_SESSION_MESSAGES.get(session_id, []))
            return sess_copy

        from boto3.dynamodb.conditions import Key

        try:
            # Query session metadata
            sess_resp = self.table.get_item(
                Key={"PK": f"WORKSPACE#{workspace_id}", "SK": f"SESSION#{session_id}"}
            )
            sess_item = sess_resp.get("Item")
            if not sess_item:
                # Check fallback
                sessions = _WORKSPACE_SESSIONS.get(workspace_id, [])
                sess = next((s for s in sessions if s.get("id") == session_id), None)
                if not sess:
                    return None
                sess_copy = dict(sess)
                sess_copy["messages"] = list(_SESSION_MESSAGES.get(session_id, []))
                return sess_copy

            # Query all messages under this session
            msg_prefix = f"SESSION#{session_id}#MSG#"
            msg_resp = self.table.query(
                KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{workspace_id}")
                & Key("SK").begins_with(msg_prefix)
            )
            msg_items = msg_resp.get("Items", [])
            while "LastEvaluatedKey" in msg_resp:
                msg_resp = self.table.query(
                    KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{workspace_id}")
                    & Key("SK").begins_with(msg_prefix),
                    ExclusiveStartKey=msg_resp["LastEvaluatedKey"],
                )
                msg_items.extend(msg_resp.get("Items", []))

            formatted_messages = []
            for it in msg_items:
                citations_raw = it.get("citations")
                citations = json.loads(citations_raw) if citations_raw and isinstance(citations_raw, str) else (citations_raw or [])
                formatted_messages.append(
                    {
                        "id": it.get("id", ""),
                        "sender": it.get("sender", "user"),
                        "text": it.get("text", ""),
                        "timestamp": it.get("timestamp", ""),
                        "citations": citations,
                        "retrieval_path": it.get("retrieval_path"),
                        "confidence": float(it["confidence"]) if "confidence" in it and it["confidence"] is not None else None,
                        "latency_ms": float(it["latency_ms"]) if "latency_ms" in it and it["latency_ms"] is not None else None,
                        "faithfulness": float(it["faithfulness"]) if "faithfulness" in it and it["faithfulness"] is not None else None,
                    }
                )

            # Sort chronological
            formatted_messages.sort(key=lambda m: m.get("timestamp", ""))

            return {
                "id": session_id,
                "workspace_id": workspace_id,
                "title": sess_item.get("title", "New Query Session"),
                "category": sess_item.get("category", "Today"),
                "updated_at": sess_item.get("updated_at", "Just now"),
                "messages": formatted_messages,
            }
        except Exception as e:
            logger.warning("DynamoDB get_chat_session failed for %s: %s", session_id, e)
            sessions = _WORKSPACE_SESSIONS.get(workspace_id, [])
            sess = next((s for s in sessions if s.get("id") == session_id), None)
            if sess:
                sess_copy = dict(sess)
                sess_copy["messages"] = list(_SESSION_MESSAGES.get(session_id, []))
                return sess_copy
            return None

    def save_chat_message(
        self, workspace_id: str, session_id: str, message: dict
    ) -> dict:
        """Append a message to a chat session."""
        msg_id = message.get("id") or f"msg_{int(time.time() * 1000)}"
        timestamp = message.get("timestamp") or time.strftime("%H:%M", time.localtime())
        
        msg_entry = {
            "id": msg_id,
            "sender": message.get("sender", "user"),
            "text": message.get("text", ""),
            "timestamp": timestamp,
            "citations": message.get("citations", []),
            "retrieval_path": message.get("retrieval_path"),
            "confidence": message.get("confidence"),
            "latency_ms": message.get("latency_ms"),
            "faithfulness": message.get("faithfulness"),
        }

        if session_id not in _SESSION_MESSAGES:
            _SESSION_MESSAGES[session_id] = []
        _SESSION_MESSAGES[session_id].append(msg_entry)

        # Update in-memory session title if first message
        if workspace_id in _WORKSPACE_SESSIONS:
            for s in _WORKSPACE_SESSIONS[workspace_id]:
                if s.get("id") == session_id:
                    if s.get("title") in ("New Query Session", "New Chat") and message.get("sender") == "user":
                        txt = message.get("text", "")
                        s["title"] = (txt[:30] + "…") if len(txt) > 32 else txt
                    s["updated_at"] = "Just now"
                    break

        if not _is_test_env():
            try:
                # Save message
                citations_json = json.dumps(message.get("citations", []))
                sort_key = f"SESSION#{session_id}#MSG#{msg_id}"
                item = {
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": sort_key,
                    "id": msg_id,
                    "session_id": session_id,
                    "workspace_id": workspace_id,
                    "sender": msg_entry["sender"],
                    "text": msg_entry["text"],
                    "timestamp": timestamp,
                    "citations": citations_json,
                }
                if msg_entry.get("retrieval_path"):
                    item["retrieval_path"] = msg_entry["retrieval_path"]
                if msg_entry.get("confidence") is not None:
                    item["confidence"] = str(msg_entry["confidence"])
                if msg_entry.get("latency_ms") is not None:
                    item["latency_ms"] = str(msg_entry["latency_ms"])
                if msg_entry.get("faithfulness") is not None:
                    item["faithfulness"] = str(msg_entry["faithfulness"])

                self.table.put_item(Item=item)

                # Update session title if user message
                if message.get("sender") == "user":
                    txt = message.get("text", "")
                    new_title = (txt[:30] + "…") if len(txt) > 32 else txt
                    self.table.update_item(
                        Key={"PK": f"WORKSPACE#{workspace_id}", "SK": f"SESSION#{session_id}"},
                        UpdateExpression="SET #t = :t, #u = :u",
                        ExpressionAttributeNames={"#t": "title", "#u": "updated_at"},
                        ExpressionAttributeValues={
                            ":t": new_title,
                            ":u": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        },
                    )
            except Exception as e:
                logger.warning("DynamoDB save_chat_message failed for %s: %s", msg_id, e)

        return msg_entry

    def delete_chat_session(self, workspace_id: str, session_id: str) -> bool:
        """Delete a single chat session and its messages."""
        if workspace_id in _WORKSPACE_SESSIONS:
            _WORKSPACE_SESSIONS[workspace_id] = [
                s for s in _WORKSPACE_SESSIONS[workspace_id] if s.get("id") != session_id
            ]
        _SESSION_MESSAGES.pop(session_id, None)

        if not _is_test_env():
            from boto3.dynamodb.conditions import Key

            try:
                # Query all items under session
                sess_items = self.table.query(
                    KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{workspace_id}")
                    & Key("SK").begins_with(f"SESSION#{session_id}")
                ).get("Items", [])

                with self.table.batch_writer() as batch:
                    for it in sess_items:
                        batch.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})
                return True
            except Exception as e:
                logger.warning("DynamoDB delete_chat_session failed for %s: %s", session_id, e)
                return False
        return True
