import logging
import os
from qdrant_client.http import models as qmodels

from config import settings
from schemas.models import Chunk, Document, Workspace

from modules.workspaces.workspaces_repository import (
    WorkspacesRepository,
    _WORKSPACES,
    _WORKSPACE_DOCS,
)
from modules.documents.documents_repository import (
    DocumentsRepository,
    DataIntegrityError,
    _IN_MEMORY_DOCS,
    _IN_MEMORY_CHUNKS,
    _DOCUMENT_FILES,
    _is_test_env,
)
from modules.query.query_repository import QueryRepository
from modules.graph.graph_repository import GraphRepository
from modules.chat.chat_repository import (
    ChatRepository,
    _WORKSPACE_SESSIONS,
    _SESSION_MESSAGES,
)

import threading

logger = logging.getLogger(__name__)


class CloudRepository(
    WorkspacesRepository,
    DocumentsRepository,
    QueryRepository,
    GraphRepository,
    ChatRepository,
):
    """Unified Facade Repository inheriting all domain repositories:
    WorkspacesRepository, DocumentsRepository, QueryRepository, GraphRepository, ChatRepository.
    Preserves 100% backward compatibility for all test suites and external scripts.
    Implements a thread-safe singleton pattern to avoid redundant client/connection instantiation.
    """
    _instance: "CloudRepository | None" = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls, dsn: str | None = None, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self, dsn: str | None = None):
        if getattr(self, "_initialized", False):
            return
        with self._lock:
            if getattr(self, "_initialized", False):
                return
            WorkspacesRepository.__init__(self)
            DocumentsRepository.__init__(self)
            QueryRepository.__init__(self)
            GraphRepository.__init__(self)
            ChatRepository.__init__(self)
            self._initialized = True

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance (used in test fixtures)."""
        with cls._lock:
            cls._instance = None

    def initialize(self) -> None:
        try:
            if not self.qclient.collection_exists(self.collection_name):
                self.qclient.create_collection(
                    collection_name=self.collection_name,
                    vectors_config={
                        "embedding_fast": qmodels.VectorParams(
                            size=384, distance=qmodels.Distance.COSINE
                        ),
                        "embedding_full": qmodels.VectorParams(
                            size=1024, distance=qmodels.Distance.COSINE
                        ),
                    },
                )
                self.qclient.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="page_number",
                    field_schema=qmodels.PayloadSchemaType.INTEGER,
                )
                self.qclient.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="document_id",
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                )
                self.qclient.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="original_id",
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                )
                self.qclient.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="workspace_id",
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                )
        except Exception as e:
            logger.warning("Qdrant init error: %s", e)

        try:
            self.dynamodb.create_table(
                TableName=self.table_name,
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
        except Exception as e:
            logger.debug("DynamoDB table creation skipped: %s", e)

    # ── OKF Facade Delegations ───────────────────────────────────────
    def resolve_entities(self, workspace_id: str, candidate_names: Any, limit: int = 20):
        from db.okf_repo import get_okf_repo
        return get_okf_repo().resolve_entities(workspace_id, candidate_names, limit=limit)

    def get_assertions_for_entity(
        self,
        workspace_id: str,
        entity_id: str,
        property_key: str | None = None,
        active_document_ids: Any = None,
        limit: int = 20,
    ):
        from db.okf_repo import get_okf_repo
        return get_okf_repo().get_assertions_for_entity(
            workspace_id,
            entity_id,
            property_key=property_key,
            active_document_ids=active_document_ids,
            limit=limit,
        )

    def save_manifest(self, workspace_id: str, manifest: Any = None) -> None:
        from db.okf_repo import get_okf_repo
        if manifest is None and not isinstance(workspace_id, str):
            manifest = workspace_id
            workspace_id = manifest.workspace_id
        return get_okf_repo().save_manifest(workspace_id, manifest)

    def get_manifest(self, workspace_id: str, document_id: str):
        from db.okf_repo import get_okf_repo
        return get_okf_repo().get_manifest(workspace_id, document_id)

    def has_ready_okf(self, workspace_id: str, document_id: str) -> bool:
        from db.okf_repo import get_okf_repo
        return get_okf_repo().has_ready_okf(workspace_id, document_id)

    def get_okf_properties(self, concept_id: str) -> list[dict[str, Any]]:
        from db.okf_repo import get_okf_repo
        repo = get_okf_repo()
        if hasattr(repo, "get_okf_properties"):
            return repo.get_okf_properties(concept_id)
        assertions = repo.get_assertions_for_entity("default", concept_id)
        return [
            {
                "concept": a.entity_name,
                "property_name": a.property_key,
                "property_value": a.property_value,
                "source_chunk_id": a.source_chunk_id or "",
                "confidence": 1.0,
            }
            for a in assertions
        ]

