import json
import logging
import os
import time
import uuid
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from config import settings
from schemas.models import Chunk, Document

logger = logging.getLogger(__name__)


def _is_test_env() -> bool:
    return os.environ.get("ENVIRONMENT") == "test" or settings.ENVIRONMENT == "test"


class DataIntegrityError(ValueError):
    """Raised when data fails schema or integrity constraints."""


_IN_MEMORY_DOCS: dict[str, Document] = {}
_IN_MEMORY_CHUNKS: dict[str, Chunk] = {}
_DOCUMENT_FILES: dict[str, tuple[bytes, str, str]] = {}
_WORKSPACE_DOCS: dict[str, list[dict]] = {}
_WORKSPACES: dict[str, dict] = {}


class DocumentsRepository:
    """Domain repository for document storage, chunk persistence, placeholder tracking, and binary caching."""

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

    def add_document_to_workspace(
        self,
        workspace_id: str,
        document: Document,
        raw_bytes: bytes | None = None,
        size_str: str | None = None,
    ) -> None:
        from modules.workspaces.workspaces_repository import WorkspacesRepository

        ws_repo = WorkspacesRepository()
        ws_repo.add_document_to_workspace(
            workspace_id=workspace_id,
            document=document,
            raw_bytes=raw_bytes,
            size_str=size_str,
        )

    def _parse_chunk(self, row: dict) -> Chunk:
        sw = float(row.get("structural_weight", 1.0))
        img_keys_raw = row.get("image_s3_keys")
        img_keys = tuple(json.loads(img_keys_raw)) if img_keys_raw else ()

        return Chunk(
            id=row["id"],
            document_id=row["document_id"],
            source_format=row["source_format"],
            text=row["text"],
            element_type=row.get("element_type", "text"),
            page_number=int(row["page_number"]) if row.get("page_number") is not None else None,
            section_path=tuple(json.loads(row["section_path"])) if row.get("section_path") else (),
            bounding_box=json.loads(row["bounding_box"]) if row.get("bounding_box") else None,
            location_reference=row.get("location_reference", ""),
            metadata=json.loads(row["metadata"]) if row.get("metadata") else None,
            structural_weight=sw,
            provider=row.get("provider", ""),
            embedding_fast=None,
            embedding_full=None,
            image_s3_keys=img_keys,
            workspace_id=row.get("workspace_id", ""),
        )

    def save(self, document: Document) -> None:
        if not document.chunks:
            return

        if not getattr(document, "workspace_id", None):
            raise DataIntegrityError(f"DataIntegrityError: Document {document.id} is missing workspace_id")

        for chunk in document.chunks:
            if not getattr(chunk, "workspace_id", None):
                raise DataIntegrityError(f"DataIntegrityError: Chunk {chunk.id} is missing workspace_id")
            if chunk.embedding_fast is None or chunk.embedding_full is None:
                raise DataIntegrityError(f"DataIntegrityError: Chunk {chunk.id} has null embeddings")
            if len(chunk.embedding_fast) != 384 or len(chunk.embedding_full) != 1024:
                raise DataIntegrityError(f"DataIntegrityError: Chunk {chunk.id} has invalid embedding dimensions")
            if all(x == 0.0 for x in chunk.embedding_full[384:]):
                raise DataIntegrityError(f"DataIntegrityError: Chunk {chunk.id} has zero-padded embedding_full")

        from services.retrieval.bm25_retriever import invalidate_chunk_cache
        if getattr(document, "workspace_id", None):
            invalidate_chunk_cache(document.workspace_id)

        if _is_test_env():
            _IN_MEMORY_DOCS[str(document.id)] = document
            for chunk in document.chunks:
                _IN_MEMORY_CHUNKS[str(chunk.id)] = chunk
            return

        try:
            with self.table.batch_writer() as batch:
                batch.put_item(
                    Item={
                        "PK": f"DOC#{document.id}",
                        "SK": f"DOC#{document.id}",
                        "id": str(document.id),
                        "workspace_id": document.workspace_id,
                        "filename": document.filename,
                        "source_format": document.source_format,
                    }
                )
                for chunk in document.chunks:
                    item = {
                        "PK": f"DOC#{chunk.document_id}",
                        "SK": f"CHUNK#{chunk.id}",
                        "GSI1PK": f"WORKSPACE#{chunk.workspace_id}",
                        "GSI1SK": f"CHUNK#{chunk.id}",
                        "id": str(chunk.id),
                        "document_id": str(chunk.document_id),
                        "workspace_id": chunk.workspace_id,
                        "source_format": chunk.source_format,
                        "text": chunk.text,
                        "element_type": chunk.element_type,
                        "page_number": chunk.page_number,
                        "section_path": json.dumps(list(chunk.section_path)),
                        "bounding_box": (
                            json.dumps(chunk.bounding_box)
                            if chunk.bounding_box
                            else None
                        ),
                        "location_reference": chunk.location_reference,
                        "metadata": (
                            json.dumps(chunk.metadata) if chunk.metadata else None
                        ),
                        "structural_weight": str(chunk.structural_weight),
                        "provider": chunk.provider,
                        "image_s3_keys": json.dumps(list(chunk.image_s3_keys)),
                    }
                    item = {k: v for k, v in item.items() if v is not None}
                    batch.put_item(Item=item)

            points = []
            for chunk in document.chunks:
                vectors = {
                    "embedding_fast": chunk.embedding_fast,
                    "embedding_full": chunk.embedding_full,
                }
                qdrant_uuid = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
                payload = {
                    "original_id": str(chunk.id),
                    "document_id": str(chunk.document_id),
                    "workspace_id": str(chunk.workspace_id),
                    "page_number": chunk.page_number,
                }
                points.append(
                    qmodels.PointStruct(id=qdrant_uuid, vector=vectors, payload=payload)
                )

            if points:
                batch_size = 50
                for i in range(0, len(points), batch_size):
                    batch = points[i : i + batch_size]
                    for attempt in range(3):
                        try:
                            self.qclient.upsert(
                                collection_name=self.collection_name,
                                points=batch,
                                wait=True,
                            )
                            time.sleep(0.2)
                            break
                        except Exception as qe:
                            if "429" in str(qe) or "Too Many Requests" in str(qe) or "timed out" in str(qe).lower():
                                logger.warning("Qdrant rate limit (attempt %d/3). Sleeping 5s...", attempt + 1)
                                time.sleep(5)
                            else:
                                raise qe
                    else:
                        raise RuntimeError("Qdrant upsert failed after 3 attempts")
        except Exception as e:
            logger.error("Failed to save to cloud database: %s", e)
            raise RuntimeError(f"Failed to save document to cloud database: {e}") from e

    def get(self, document_id: str) -> Document | None:
        if _is_test_env():
            return _IN_MEMORY_DOCS.get(document_id)

        from boto3.dynamodb.conditions import Key

        def _fetch(did: str) -> Document | None:
            try:
                response = self.table.query(
                    KeyConditionExpression=Key("PK").eq(f"DOC#{did}")
                )
                items = response.get("Items", [])
                if not items:
                    return None

                doc_item = next(
                    (item for item in items if item["SK"].startswith("DOC#")), None
                )
                if not doc_item:
                    return None

                chunk_items = [item for item in items if item["SK"].startswith("CHUNK#")]
                chunks = [self._parse_chunk(row) for row in chunk_items]
                return Document(
                    id=str(doc_item["id"]),
                    filename=doc_item["filename"],
                    source_format=doc_item["source_format"],
                    chunks=tuple(chunks),
                    workspace_id=doc_item.get("workspace_id", ""),
                )
            except Exception:
                return None

        # Primary: try exactly the supplied ID (workspace-scoped uuid5 for new docs).
        doc = _fetch(document_id)
        if doc is not None:
            return doc

        # Transition-window fallback: if the supplied ID was produced by the old
        # bare-filename seed (pre-fix legacy record), it will already be found above.
        # If the caller passes a workspace-scoped ID that doesn't exist yet (because
        # the doc was ingested before the fix), we cannot reverse-engineer the filename,
        # so we can't automatically try the legacy ID here.
        # This fallback is intentionally a no-op placeholder; remove once corpus is
        # fully re-ingested under the new scheme.
        return None

    def get_all_chunks(
        self,
        document_ids: list[str] | None = None,
        workspace_id: str | None = None,
    ) -> list[Chunk]:
        if _is_test_env():
            chunks = list(_IN_MEMORY_CHUNKS.values())
            if workspace_id:
                chunks = [c for c in chunks if c.workspace_id == workspace_id]
            if document_ids is not None:
                doc_id_set = set(document_ids)
                chunks = [c for c in chunks if str(c.document_id) in doc_id_set]
            return chunks

        from boto3.dynamodb.conditions import Key

        if workspace_id:
            from services.retrieval.bm25_retriever import get_cached_chunks

            def _fetch_workspace_chunks() -> list[Chunk]:
                try:
                    response = self.table.query(
                        IndexName="GSI1",
                        KeyConditionExpression=Key("GSI1PK").eq(f"WORKSPACE#{workspace_id}")
                        & Key("GSI1SK").begins_with("CHUNK#"),
                    )
                    items = response.get("Items", [])
                    while "LastEvaluatedKey" in response:
                        response = self.table.query(
                            IndexName="GSI1",
                            KeyConditionExpression=Key("GSI1PK").eq(f"WORKSPACE#{workspace_id}")
                            & Key("GSI1SK").begins_with("CHUNK#"),
                            ExclusiveStartKey=response["LastEvaluatedKey"],
                        )
                        items.extend(response.get("Items", []))
                    return [self._parse_chunk(row) for row in items]
                except Exception as e:
                    logger.warning("DynamoDB GSI1 query failed for workspace %s: %s", workspace_id, e)
                    from boto3.dynamodb.conditions import Attr

                    resp = self.table.scan(
                        FilterExpression=Attr("workspace_id").eq(workspace_id)
                        & Attr("SK").begins_with("CHUNK#")
                    )
                    items = resp.get("Items", [])
                    while "LastEvaluatedKey" in resp:
                        resp = self.table.scan(
                            FilterExpression=Attr("workspace_id").eq(workspace_id)
                            & Attr("SK").begins_with("CHUNK#"),
                            ExclusiveStartKey=resp["LastEvaluatedKey"],
                        )
                        items.extend(resp.get("Items", []))
                    return [self._parse_chunk(row) for row in items]

            chunks = get_cached_chunks(workspace_id, _fetch_workspace_chunks)
            if document_ids is not None:
                doc_id_set = set(document_ids)
                chunks = [c for c in chunks if str(c.document_id) in doc_id_set]
            return chunks
        elif document_ids is not None:
            chunks = []
            for d in document_ids:
                doc = self.get(d)
                if doc:
                    chunks.extend(doc.chunks)
            return chunks
        else:
            raise ValueError("workspace_id or document_ids is required to retrieve chunks")

    def add_placeholder_document(
        self,
        workspace_id: str,
        doc_id: str,
        filename: str,
        format_str: str,
        size_str: str,
        raw_bytes: bytes | None = None,
    ) -> dict:
        doc_entry = {
            "id": str(doc_id),
            "filename": filename,
            "format": format_str.lower(),
            "upload_date": "Just now",
            "chunk_count": 0,
            "status": "Processing",
            "size": size_str,
        }
        if workspace_id not in _WORKSPACE_DOCS:
            _WORKSPACE_DOCS[workspace_id] = []
        _WORKSPACE_DOCS[workspace_id].insert(0, doc_entry)

        # Immediately update the in-memory workspace doc_count so the
        # workspace card reflects the upload before background ingestion completes.
        if workspace_id in _WORKSPACES:
            _WORKSPACES[workspace_id]["document_count"] = len(_WORKSPACE_DOCS[workspace_id])
            _WORKSPACES[workspace_id]["last_active"] = "Active just now"

        if not _is_test_env():
            try:
                item = {
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": f"DOC#{doc_id}",
                    "workspace_id": workspace_id,
                    **doc_entry,
                }
                self.table.put_item(Item=item)
            except Exception as e:
                logger.warning("DynamoDB add_placeholder_document failed for %s: %s", doc_id, e)

        if raw_bytes:
            mime = "application/pdf" if format_str.lower() == "pdf" else "application/octet-stream"
            _DOCUMENT_FILES[str(doc_id)] = (raw_bytes, filename, mime)

        return doc_entry

    def update_document_status(
        self,
        workspace_id: str,
        doc_id: str,
        status: str,
        chunk_count: int | None = None,
        error: str | None = None,
    ) -> None:
        docs = _WORKSPACE_DOCS.get(workspace_id, [])
        for d in docs:
            if d.get("id") == str(doc_id):
                d["status"] = status
                if chunk_count is not None:
                    d["chunk_count"] = chunk_count
                if error:
                    d["error"] = error
                break

        if not _is_test_env():
            try:
                update_expr = "SET #st = :st"
                expr_names = {"#st": "status"}
                expr_vals = {":st": status}
                if chunk_count is not None:
                    update_expr += ", #cc = :cc"
                    expr_names["#cc"] = "chunk_count"
                    expr_vals[":cc"] = chunk_count
                if error:
                    update_expr += ", #err = :err"
                    expr_names["#err"] = "error"
                    expr_vals[":err"] = error
                self.table.update_item(
                    Key={"PK": f"WORKSPACE#{workspace_id}", "SK": f"DOC#{doc_id}"},
                    UpdateExpression=update_expr,
                    ExpressionAttributeNames=expr_names,
                    ExpressionAttributeValues=expr_vals,
                )
            except Exception as e:
                logger.warning("DynamoDB update_document_status failed for %s: %s", doc_id, e)

    def get_document_file(self, document_id: str) -> tuple[bytes, str, str] | None:
        if document_id in _DOCUMENT_FILES:
            return _DOCUMENT_FILES[document_id]

        target_filename = ""
        doc = self.get(document_id)
        if doc:
            target_filename = doc.filename
        else:
            for ws_docs in _WORKSPACE_DOCS.values():
                for d in ws_docs:
                    if str(d.get("id")) == str(document_id):
                        target_filename = d.get("filename", "")
                        break
                if target_filename:
                    break

        for base_dir in [Path("data"), Path("tests/data"), Path("backend/tests/data")]:
            if base_dir.exists():
                for p in base_dir.rglob("*"):
                    if p.is_file():
                        if target_filename and (p.name.lower() == target_filename.lower() or Path(target_filename).stem.lower() in p.name.lower()):
                            mime = "application/pdf" if p.suffix.lower() == ".pdf" else "application/octet-stream"
                            return (p.read_bytes(), p.name, mime)
                        if document_id and document_id in p.name:
                            mime = "application/pdf" if p.suffix.lower() == ".pdf" else "application/octet-stream"
                            return (p.read_bytes(), p.name, mime)
        return None
