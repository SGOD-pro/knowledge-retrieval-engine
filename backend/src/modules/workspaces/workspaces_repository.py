import logging
import os
import time
from config import settings
from schemas.models import Document, Workspace

logger = logging.getLogger(__name__)

from modules.documents.documents_repository import (
    _WORKSPACES,
    _WORKSPACE_DOCS,
    _DOCUMENT_FILES,
    _IN_MEMORY_DOCS,
    _IN_MEMORY_CHUNKS,
)


def _is_test_env() -> bool:
    return os.environ.get("ENVIRONMENT") == "test" or settings.ENVIRONMENT == "test"


class WorkspacesRepository:
    """Domain repository for workspace management, document mapping, and cascade deletion."""

    def __init__(self):
        self.table_name = settings.DYNAMODB_TABLE_NAME
        from aws.infra import get_resource
        from qdrant_client import QdrantClient

        self.dynamodb = get_resource("dynamodb")
        self.table = self.dynamodb.Table(self.table_name)
        self.qclient = QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY,
            timeout=60.0,
            check_compatibility=False,
        )
        self.collection_name = "kre_chunks"

    def create_workspace(
        self,
        name: str,
        industry: str = "general",
        description: str = "",
        workspace_id: str | None = None,
    ) -> Workspace:
        import uuid

        ws_id = workspace_id or f"ws_{uuid.uuid4().hex[:8]}"
        ws_obj = {
            "id": ws_id,
            "name": name,
            "industry": industry,
            "description": description or f"Workspace for {name}",
            "document_count": 0,
            "last_active": "Just created",
            "status": "active",
            "icon_type": "general",
        }
        _WORKSPACES[ws_id] = ws_obj
        _WORKSPACE_DOCS[ws_id] = []

        if not _is_test_env():
            try:
                item = {
                    "PK": f"WORKSPACE#{ws_id}",
                    "SK": "META",
                    **ws_obj,
                }
                self.table.put_item(Item=item)
            except Exception as e:
                logger.warning("DynamoDB create_workspace failed for %s: %s", ws_id, e)

        return Workspace(
            id=ws_obj["id"],
            name=ws_obj["name"],
            industry=ws_obj["industry"],
            description=ws_obj["description"],
            document_count=ws_obj["document_count"],
            last_active=ws_obj["last_active"],
            status=ws_obj["status"],
            icon_type=ws_obj["icon_type"],
        )

    def get_workspaces(self) -> list[Workspace]:
        if _is_test_env():
            ws_list = list(_WORKSPACES.values())
            res = []
            for w in ws_list:
                docs = _WORKSPACE_DOCS.get(w["id"], [])
                res.append(
                    Workspace(
                        id=w["id"],
                        name=w["name"],
                        industry=w.get("industry", "general"),
                        description=w.get("description", ""),
                        document_count=len(docs),
                        last_active=w.get("last_active", "Active recently"),
                        status=w.get("status", "active"),
                        icon_type=w.get("icon_type", "general"),
                    )
                )
            return res

        from boto3.dynamodb.conditions import Key

        try:
            resp = self.table.scan()
            items = resp.get("Items", [])
            workspaces = []
            for it in items:
                if it.get("PK", "").startswith("WORKSPACE#") and it.get("SK") == "META":
                    ws_id = it.get("id") or it["PK"].replace("WORKSPACE#", "")
                    docs_resp = self.table.query(
                        KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{ws_id}")
                        & Key("SK").begins_with("DOC#")
                    )
                    doc_count = len(docs_resp.get("Items", []))
                    workspaces.append(
                        Workspace(
                            id=ws_id,
                            name=it.get("name", "Untitled Workspace"),
                            industry=it.get("industry", "general"),
                            description=it.get("description", ""),
                            document_count=doc_count,
                            last_active=it.get("last_active", "Active recently"),
                            status=it.get("status", "active"),
                            icon_type=it.get("icon_type", "general"),
                        )
                    )
            return workspaces
        except Exception as e:
            logger.warning("DynamoDB get_workspaces failed: %s", e)
            return [
                Workspace(
                    id=w["id"],
                    name=w["name"],
                    industry=w.get("industry", "general"),
                    description=w.get("description", ""),
                    document_count=len(_WORKSPACE_DOCS.get(w["id"], [])),
                    last_active=w.get("last_active", "Active recently"),
                    status=w.get("status", "active"),
                    icon_type=w.get("icon_type", "general"),
                )
                for w in _WORKSPACES.values()
            ]

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        workspaces = self.get_workspaces()
        return next((w for w in workspaces if w.id == workspace_id), None)

    def delete_workspace(self, workspace_id: str) -> bool:
        """Cascade delete workspace, its documents, chunks, vectors, and chat sessions/messages."""
        from modules.chat.chat_repository import _WORKSPACE_SESSIONS, _SESSION_MESSAGES
        from qdrant_client.http import models as qmodels

        if _is_test_env():
            if workspace_id not in _WORKSPACES and workspace_id not in _WORKSPACE_DOCS:
                return False
            docs = list(_WORKSPACE_DOCS.get(workspace_id, []))
            doc_ids = [d.get("id") for d in docs if d.get("id")]
            _WORKSPACES.pop(workspace_id, None)
            _WORKSPACE_DOCS.pop(workspace_id, None)
            for doc_id in doc_ids:
                doc_id_str = str(doc_id)
                _DOCUMENT_FILES.pop(doc_id_str, None)
                _IN_MEMORY_DOCS.pop(doc_id_str, None)
                chunks_to_remove = [
                    cid
                    for cid, c in _IN_MEMORY_CHUNKS.items()
                    if getattr(c, "document_id", None) == doc_id_str
                ]
                for cid in chunks_to_remove:
                    _IN_MEMORY_CHUNKS.pop(cid, None)

            # Cascade delete in-memory chat sessions & messages
            sessions = _WORKSPACE_SESSIONS.pop(workspace_id, [])
            for s in sessions:
                _SESSION_MESSAGES.pop(s.get("id"), None)
            return True

        from boto3.dynamodb.conditions import Key

        try:
            # Query all items under WORKSPACE#{workspace_id} (DOC#, SESSION#, META)
            all_ws_items_resp = self.table.query(
                KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{workspace_id}")
            )
            all_ws_items = all_ws_items_resp.get("Items", [])
            while "LastEvaluatedKey" in all_ws_items_resp:
                all_ws_items_resp = self.table.query(
                    KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{workspace_id}"),
                    ExclusiveStartKey=all_ws_items_resp["LastEvaluatedKey"],
                )
                all_ws_items.extend(all_ws_items_resp.get("Items", []))

            doc_ids = [
                it["id"] for it in all_ws_items if it.get("SK", "").startswith("DOC#") and it.get("id")
            ]

            # Clean in-memory dicts
            _WORKSPACES.pop(workspace_id, None)
            _WORKSPACE_DOCS.pop(workspace_id, None)
            for doc_id in doc_ids:
                doc_id_str = str(doc_id)
                _DOCUMENT_FILES.pop(doc_id_str, None)
                _IN_MEMORY_DOCS.pop(doc_id_str, None)

            sessions = _WORKSPACE_SESSIONS.pop(workspace_id, [])
            for s in sessions:
                _SESSION_MESSAGES.pop(s.get("id"), None)

            # 1. Batch delete ALL records under PK = WORKSPACE#{workspace_id} (META, DOC#, SESSION#, MSG#)
            if all_ws_items:
                with self.table.batch_writer() as batch:
                    for it in all_ws_items:
                        batch.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})

            # 2. Batch delete all document DOC# and CHUNK# items in DynamoDB
            for doc_id in doc_ids:
                doc_id_str = str(doc_id)
                resp = self.table.query(
                    KeyConditionExpression=Key("PK").eq(f"DOC#{doc_id_str}")
                )
                items = resp.get("Items", [])
                if items:
                    with self.table.batch_writer() as batch:
                        for it in items:
                            batch.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})

            # 3. Delete Qdrant vectors scoped to this workspace
            try:
                self.qclient.delete(
                    collection_name=self.collection_name,
                    points_selector=qmodels.FilterSelector(
                        filter=qmodels.Filter(
                            must=[
                                qmodels.FieldCondition(
                                    key="workspace_id",
                                    match=qmodels.MatchValue(value=workspace_id),
                                )
                            ]
                        )
                    ),
                    wait=True,
                )
            except Exception as qe:
                logger.warning("Qdrant workspace vector deletion failed: %s", qe)

            return True
        except Exception as e:
            logger.error("delete_workspace cascade failed for %s: %s", workspace_id, e)
            return False

    def get_workspace_documents(
        self, workspace_id: str, page: int = 1, limit: int = 10
    ) -> dict:
        if _is_test_env():
            docs = _WORKSPACE_DOCS.get(workspace_id, [])
        else:
            from boto3.dynamodb.conditions import Key

            try:
                response = self.table.query(
                    KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{workspace_id}")
                    & Key("SK").begins_with("DOC#")
                )
                items = response.get("Items", [])
                while "LastEvaluatedKey" in response:
                    response = self.table.query(
                        KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{workspace_id}")
                        & Key("SK").begins_with("DOC#"),
                        ExclusiveStartKey=response["LastEvaluatedKey"],
                    )
                    items.extend(response.get("Items", []))
                docs = []
                for it in items:
                    d_info = {
                        k: v
                        for k, v in it.items()
                        if k not in ("PK", "SK", "GSI1PK", "GSI1SK")
                    }
                    if "chunk_count" in d_info:
                        d_info["chunk_count"] = int(d_info["chunk_count"])
                    docs.append(d_info)
            except Exception as e:
                logger.warning("DynamoDB get_workspace_documents failed: %s", e)
                docs = _WORKSPACE_DOCS.get(workspace_id, [])

        total = len(docs)
        start = (page - 1) * limit
        end = start + limit
        page_docs = docs[start:end]
        total_pages = max(1, (total + limit - 1) // limit)
        return {
            "documents": page_docs,
            "total_documents": total,
            "current_page": page,
            "total_pages": total_pages,
        }

    def add_document_to_workspace(
        self,
        workspace_id: str,
        document: Document,
        raw_bytes: bytes | None = None,
        size_str: str | None = None,
    ) -> None:
        if workspace_id not in _WORKSPACES and _is_test_env():
            self.create_workspace(
                name=f"Workspace {workspace_id}", workspace_id=workspace_id
            )

        doc_id_str = str(document.id)
        existing_list = _WORKSPACE_DOCS.get(workspace_id, [])
        existing = next((d for d in existing_list if d.get("id") == doc_id_str), None)

        if existing is not None:
            existing["filename"] = document.filename
            existing["format"] = document.source_format.lower()
            existing["chunk_count"] = len(document.chunks)
            existing["status"] = "Ready"
            if size_str:
                existing["size"] = size_str
            doc_entry = existing
        else:
            doc_entry = {
                "id": doc_id_str,
                "filename": document.filename,
                "format": document.source_format.lower(),
                "upload_date": "Just now",
                "chunk_count": len(document.chunks),
                "status": "Ready",
                "size": size_str or "1.2 MB",
            }
            if workspace_id not in _WORKSPACE_DOCS:
                _WORKSPACE_DOCS[workspace_id] = []
            _WORKSPACE_DOCS[workspace_id].insert(0, doc_entry)

        if workspace_id in _WORKSPACES:
            _WORKSPACES[workspace_id]["document_count"] = len(
                _WORKSPACE_DOCS[workspace_id]
            )
            _WORKSPACES[workspace_id]["last_active"] = "Active just now"

        if not _is_test_env():
            try:
                item = {
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": f"DOC#{doc_id_str}",
                    "workspace_id": workspace_id,
                    **doc_entry,
                }
                self.table.put_item(Item=item)
            except Exception as e:
                logger.warning("DynamoDB add_document_to_workspace failed: %s", e)

        if raw_bytes:
            mime = (
                "application/pdf"
                if document.source_format.lower() == "pdf"
                else "application/octet-stream"
            )
            _DOCUMENT_FILES[doc_id_str] = (raw_bytes, document.filename, mime)
