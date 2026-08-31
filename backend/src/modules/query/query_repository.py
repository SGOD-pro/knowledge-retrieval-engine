import json
import logging
import os
import uuid
import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from config import settings
from schemas.models import Chunk
from modules.documents.documents_repository import DocumentsRepository, _is_test_env, _IN_MEMORY_CHUNKS
from modules.workspaces.workspaces_repository import WorkspacesRepository

logger = logging.getLogger(__name__)


class QueryRepository:
    """Domain repository for vector similarity search, chunk retrieval, and semantic caching."""

    def __init__(self):
        self.table_name = settings.DYNAMODB_TABLE_NAME
        from aws.infra import get_resource

        self.dynamodb = get_resource("dynamodb")
        self.table = self.dynamodb.Table(self.table_name)
        self.qclient = QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY,
            timeout=60.0,
            check_compatibility=False,
        )
        self.collection_name = "kre_chunks"
        self._doc_repo = DocumentsRepository()
        self._ws_repo = WorkspacesRepository()

    def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 10) -> dict:
        return self._ws_repo.get_workspace_documents(workspace_id, page=page, limit=limit)

    def get_all_chunks(self, document_ids: list[str] | None = None, workspace_id: str | None = None) -> list[Chunk]:
        return self._doc_repo.get_all_chunks(document_ids=document_ids, workspace_id=workspace_id)

    def search_vector(
        self,
        query_embedding: list[float],
        embedding_column: str = "embedding_full",
        document_ids: list[str] | None = None,
        candidate_page_ids: list[int] | None = None,
        candidate_chunk_ids: list[str] | None = None,
        workspace_id: str = "",
        limit: int = 10,
    ) -> list[tuple[Chunk, float]]:
        if not workspace_id:
            raise ValueError("workspace_id is required for vector search")

        if _is_test_env():
            results = []
            q_vec = np.array(query_embedding)
            has_page_constraint = candidate_page_ids is not None and len(candidate_page_ids) > 0
            has_chunk_constraint = candidate_chunk_ids is not None and len(candidate_chunk_ids) > 0

            for chunk in _IN_MEMORY_CHUNKS.values():
                if getattr(chunk, "workspace_id", "") != workspace_id:
                    continue
                if document_ids and str(chunk.document_id) not in document_ids:
                    continue
                if has_page_constraint or has_chunk_constraint:
                    page_match = has_page_constraint and chunk.page_number in candidate_page_ids
                    chunk_match = has_chunk_constraint and str(chunk.id) in candidate_chunk_ids
                    if not (page_match or chunk_match):
                        continue
                c_vec = chunk.embedding_fast if embedding_column == "embedding_fast" else chunk.embedding_full
                if c_vec:
                    c_arr = np.array(c_vec)
                    norm_q = np.linalg.norm(q_vec)
                    norm_c = np.linalg.norm(c_arr)
                    score = float(np.dot(q_vec, c_arr) / ((norm_q * norm_c) + 1e-9))
                    results.append((chunk, score))
            results.sort(key=lambda x: x[1], reverse=True)
            return results[:limit]

        if embedding_column not in ("embedding_fast", "embedding_full"):
            raise ValueError(f"Invalid embedding_column: {embedding_column}")

        must_filters = [
            qmodels.FieldCondition(
                key="workspace_id", match=qmodels.MatchValue(value=workspace_id)
            )
        ]
        if document_ids:
            must_filters.append(
                qmodels.FieldCondition(
                    key="document_id", match=qmodels.MatchAny(any=document_ids)
                )
            )
        should_filters = []
        if candidate_page_ids:
            should_filters.append(
                qmodels.FieldCondition(
                    key="page_number", match=qmodels.MatchAny(any=candidate_page_ids)
                )
            )
        if candidate_chunk_ids:
            should_filters.append(
                qmodels.FieldCondition(
                    key="original_id", match=qmodels.MatchAny(any=candidate_chunk_ids)
                )
            )

        if should_filters:
            must_filters.append(qmodels.Filter(should=should_filters))

        qfilter = qmodels.Filter(must=must_filters)

        try:
            response = self.qclient.query_points(
                collection_name=self.collection_name,
                query=query_embedding,
                using=embedding_column,
                query_filter=qfilter,
                limit=limit,
                with_payload=True,
            )
            results = response.points
        except Exception as e:
            logger.error("Qdrant search failed: %s", e)
            return []

        keys = []
        for hit in results:
            original_id = hit.payload.get("original_id")
            doc_id = hit.payload.get("document_id")
            if original_id and doc_id:
                keys.append({"PK": f"DOC#{doc_id}", "SK": f"CHUNK#{original_id}"})

        if not keys:
            return []

        try:
            resp = self.dynamodb.meta.client.batch_get_item(
                RequestItems={self.table_name: {"Keys": keys}}
            )
            items = resp.get("Responses", {}).get(self.table_name, [])
            item_map = {item["id"]: self._doc_repo._parse_chunk(item) for item in items}
        except Exception as e:
            logger.warning("DynamoDB batch_get_item failed: %s", e)
            item_map = {}

        final_results = []
        for hit in results:
            original_id = hit.payload.get("original_id")
            chunk = item_map.get(str(original_id))
            if chunk:
                final_results.append((chunk, hit.score))
        return final_results

    def check_semantic_cache(
        self, query_embedding: list[float], doc_scope_hash: str, provider: str
    ) -> str | None:
        try:
            dim = len(query_embedding)
            col = "kre_cache_fast" if dim == 384 else "kre_cache_full"
            response = self.qclient.query_points(
                collection_name=col,
                query=query_embedding,
                query_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="doc_scope_hash",
                            match=qmodels.MatchValue(value=doc_scope_hash),
                        ),
                        qmodels.FieldCondition(
                            key="provider", match=qmodels.MatchValue(value=provider)
                        ),
                    ]
                ),
                limit=1,
                score_threshold=0.95,
                with_payload=True,
            )
            if response.points:
                return response.points[0].payload.get("redis_key")
        except Exception:
            pass
        return None

    def save_semantic_cache(
        self,
        redis_key: str,
        query_embedding: list[float],
        doc_scope_hash: str,
        provider: str,
    ) -> None:
        try:
            dim = len(query_embedding)
            col = "kre_cache_fast" if dim == 384 else "kre_cache_full"
            if not self.qclient.collection_exists(col):
                self.qclient.create_collection(
                    collection_name=col,
                    vectors_config=qmodels.VectorParams(
                        size=dim, distance=qmodels.Distance.COSINE
                    ),
                )
            qdrant_uuid = str(uuid.uuid5(uuid.NAMESPACE_OID, redis_key))
            self.qclient.upsert(
                collection_name=col,
                points=[
                    qmodels.PointStruct(
                        id=qdrant_uuid,
                        vector=query_embedding,
                        payload={
                            "redis_key": redis_key,
                            "doc_scope_hash": doc_scope_hash,
                            "provider": provider,
                        },
                    )
                ],
            )
        except Exception as e:
            logger.warning("save_semantic_cache failed: %s", e)
