import hashlib
import logging
import tempfile
import uuid
import mimetypes
from pathlib import Path
from fastapi import BackgroundTasks, HTTPException, Response, UploadFile

from config import settings
from modules.documents.documents_repository import DocumentsRepository
from ingestion.format_router import SUPPORTED_FORMATS
from ingestion.parse_service import ingest_document, generate_deterministic_doc_id

logger = logging.getLogger(__name__)


def _resolve_media_type(filename: str) -> str:
    """Resolve media type using standard library mimetypes with utf-8 charset defaults for text."""
    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        if guessed.startswith("text/") or guessed in ("application/json", "application/javascript"):
            return f"{guessed}; charset=utf-8"
        return guessed
    suffix = Path(filename).suffix.lower()
    if suffix in (".md", ".markdown"):
        return "text/markdown; charset=utf-8"
    if suffix == ".tsv":
        return "text/tab-separated-values; charset=utf-8"
    return "application/octet-stream"


class DocumentsService:
    """Service handling multi-format document ingestion, storage, and file streaming."""

    def __init__(self, repo: DocumentsRepository | None = None):
        self.repo = repo or DocumentsRepository()

    @staticmethod
    def _enrich_page_index_snapshot_metadata(
        workspace_id: str,
        doc_id: str,
        doc_ver: str,
        filename: str,
        temp_path: Path | None,
        chunks: Any,
        boundary_repo: Any,
    ) -> tuple[str | None, int]:
        """Resolve or extract PageIndex summary and return (status, elements_count)."""
        if Path(filename).suffix.lower() != ".pdf":
            return None, 0
        try:
            pi_summary = boundary_repo.get_page_index_summary(workspace_id, doc_id, doc_ver)
            if not pi_summary:
                from ingestion.page_index_service import extract_and_persist_page_index
                pi_summary = extract_and_persist_page_index(
                    path=temp_path,
                    document_id=doc_id,
                    workspace_id=workspace_id,
                    document_version=doc_ver,
                    chunks=chunks,
                    boundary_repo=boundary_repo,
                )
            if pi_summary:
                return pi_summary.layout_status, pi_summary.structural_elements_count
            return "failed", 0
        except Exception as pi_err:
            logger.warning("Could not persist PageIndex for %s: %s", doc_id, pi_err)
            return "failed", 0

    def background_ingest(
        self,
        temp_path_str: str,
        workspace_id: str,
        doc_id: str,
        filename: str,
        content: bytes,
        size_str: str,
    ):
        temp_path = Path(temp_path_str)
        try:
            import api.routes as _api_routes

            ingest_fn = getattr(_api_routes, "ingest_document", ingest_document)

            # Re-ingestion guard: if the workspace-scoped doc_id already has a
            # persisted document (same workspace + same filename → same ID) with the
            # exact same content hash version, skip the expensive pipeline.
            # If the content hash differs (file content changed), proceed with re-ingestion
            # and replace the document, table records, and snapshot.
            incoming_version = hashlib.sha256(content).hexdigest()[:32]
            existing = self.repo.get(doc_id)
            if existing is not None:
                existing_ver = None
                try:
                    from src.db.workspace_boundary_repo import get_workspace_boundary_repo
                    boundary_repo = get_workspace_boundary_repo()
                    if boundary_repo:
                        snap = boundary_repo.get_document_snapshot(workspace_id, doc_id)
                        if snap and snap.version:
                            existing_ver = snap.version
                except Exception:
                    pass

                if not existing_ver:
                    try:
                        from db.table_store import get_shared_table_store
                        store = get_shared_table_store()
                        manifest = store.get_ingestion_manifest(doc_id, workspace_id)
                        if manifest and manifest.get("document_version"):
                            existing_ver = manifest["document_version"]
                    except Exception:
                        pass

                if existing_ver and existing_ver == incoming_version:
                    self.repo.update_document_status(
                        workspace_id=workspace_id,
                        doc_id=doc_id,
                        status="Ready",
                        chunk_count=len(existing.chunks),
                    )
                    logger.info(
                        "background_ingest.skipped_duplicate ws=%s doc_id=%s filename=%s ver=%s",
                        workspace_id,
                        doc_id,
                        filename,
                        incoming_version,
                    )
                    return

            document = ingest_fn(
                temp_path,
                document_id=doc_id,
                filename=filename,
                provider=settings.MODEL_PROVIDER,
                workspace_id=workspace_id,
            )
            self.repo.save(document)
            self.repo.add_document_to_workspace(
                workspace_id=workspace_id,
                document=document,
                raw_bytes=content,
                size_str=size_str,
            )
            self.repo.update_document_status(
                workspace_id=workspace_id,
                doc_id=doc_id,
                status="Ready",
                chunk_count=len(document.chunks),
            )
            logger.info(
                "background_ingest.success ws=%s doc_id=%s filename=%s",
                workspace_id,
                doc_id,
                filename,
            )

            # Record DocumentSnapshot and register source in boundary repo for 0004
            try:
                from datetime import datetime, timezone
                from src.schemas.contracts.boundary import DocumentSnapshot
                from src.db.workspace_boundary_repo import get_workspace_boundary_repo
                from db.table_store import get_shared_table_store

                boundary_repo = get_workspace_boundary_repo()
                boundary_repo.create_source(workspace_id=workspace_id, title=filename, source_id=doc_id)

                if getattr(document, "source_format", "").lower() == "csv":
                    store = get_shared_table_store()
                    manifest = store.get_ingestion_manifest(doc_id, workspace_id) or {}
                    if manifest:
                        doc_ver = manifest.get("document_version") or hashlib.sha256(content).hexdigest()[:32]
                        incomplete_cov = not bool(manifest.get("coverage_complete", False))
                        exclusions = tuple(manifest.get("rejection_reasons", []))
                        persisted = int(manifest.get("persisted_rows", 0))
                        total = int(manifest.get("source_rows", 0))
                    else:
                        doc_ver = hashlib.sha256(content).hexdigest()[:32]
                        incomplete_cov = bool(getattr(document, "incomplete_coverage_flag", False))
                        exclusions = tuple(getattr(document, "exclusions", ()))
                        persisted = len(document.chunks)
                        total = len(document.chunks) + len(exclusions)
                else:
                    doc_ver = hashlib.sha256(content).hexdigest()[:32]
                    incomplete_cov = bool(getattr(document, "incomplete_coverage_flag", False))
                    exclusions = tuple(getattr(document, "exclusions", ()))
                    # For unstructured documents, persisted tracks indexed chunks and total tracks chunks plus excluded units
                    persisted = len(document.chunks)
                    total = len(document.chunks) + len(exclusions)

                pi_status, pi_count = self._enrich_page_index_snapshot_metadata(
                    workspace_id=workspace_id,
                    doc_id=doc_id,
                    doc_ver=doc_ver,
                    filename=filename,
                    temp_path=temp_path,
                    chunks=document.chunks,
                    boundary_repo=boundary_repo,
                )

                snapshot = DocumentSnapshot(
                    workspace_id=workspace_id,
                    document_id=doc_id,
                    source=filename,
                    version=doc_ver,
                    format=Path(filename).suffix.lower().replace(".", ""),
                    incomplete_coverage_flag=incomplete_cov,
                    exclusions=exclusions,
                    persisted_rows=persisted,
                    total_rows=total,
                    created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    page_index_status=pi_status,
                    page_index_elements_count=pi_count,
                )
                boundary_repo.save_document_snapshot(snapshot)
            except Exception as snap_err:
                logger.warning("Could not persist DocumentSnapshot for %s: %s", doc_id, snap_err)

        except Exception as exc:
            logger.exception(
                "background_ingest.failed ws=%s doc_id=%s filename=%s error=%s",
                workspace_id,
                doc_id,
                filename,
                exc,
            )
            self.repo.update_document_status(
                workspace_id=workspace_id,
                doc_id=doc_id,
                status="Failed",
                error=str(exc),
            )
        finally:
            temp_path.unlink(missing_ok=True)

    async def upload_workspace_documents(
        self,
        workspace_id: str,
        background_tasks: BackgroundTasks,
        files: list[UploadFile],
    ) -> dict:
        uploaded = []

        for file in files:
            filename = file.filename or "unknown.pdf"
            suffix = Path(filename).suffix.lower()
            if suffix not in SUPPORTED_FORMATS:
                raise HTTPException(
                    415,
                    f"File '{filename}' format not supported. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}",
                )

            content = await file.read()
            if len(content) > 50 * 1024 * 1024:
                raise HTTPException(413, "Payload Too Large: File exceeds 50MB limit")
            size_kb = len(content) / 1024.0
            size_str = (
                f"{size_kb / 1024.0:.1f} MB" if size_kb >= 1024 else f"{size_kb:.0f} KB"
            )
            doc_id = generate_deterministic_doc_id(filename, workspace_id=workspace_id)

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
                temporary.write(content)
                temp_path = Path(temporary.name)

            self.repo.add_placeholder_document(
                workspace_id=workspace_id,
                doc_id=doc_id,
                filename=filename,
                format_str=suffix.replace(".", ""),
                size_str=size_str,
                raw_bytes=content,
            )

            background_tasks.add_task(
                self.background_ingest,
                str(temp_path),
                workspace_id,
                doc_id,
                filename,
                content,
                size_str,
            )

            doc_ver = hashlib.sha256(content).hexdigest()[:32]
            item_info = {
                "id": doc_id,
                "document_id": doc_id,
                "filename": filename,
                "format": suffix.replace(".", ""),
                "version": doc_ver,
                "status": "processing",
            }
            uploaded.append(item_info)

        return {"uploaded_documents": uploaded, "items": uploaded}

    async def ingest_direct(self, file: UploadFile, workspace_id: str = "ws_001") -> dict:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in SUPPORTED_FORMATS:
            raise HTTPException(
                415, f"Supported formats: {', '.join(sorted(SUPPORTED_FORMATS))}"
            )
        content = await file.read()
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(413, "Payload Too Large: File exceeds 50MB limit")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
            temporary.write(content)
            path = Path(temporary.name)
        try:
            import asyncio
            import api.routes as _api_routes

            ingest_fn = getattr(_api_routes, "ingest_document", ingest_document)


            def _sync_ingest_and_save():
                doc = ingest_fn(
                    path,
                    filename=file.filename,
                    provider=settings.MODEL_PROVIDER,
                    workspace_id=workspace_id,
                )
                self.repo.save(doc)
                self.repo.add_document_to_workspace(workspace_id, doc, raw_bytes=content)

                # Record DocumentSnapshot and register source in boundary repo
                incomplete_cov = False
                doc_ver = hashlib.sha256(content).hexdigest()[:32]
                try:
                    from datetime import datetime, timezone
                    from src.schemas.contracts.boundary import DocumentSnapshot
                    from src.db.workspace_boundary_repo import get_workspace_boundary_repo
                    from db.table_store import get_shared_table_store

                    boundary_repo = get_workspace_boundary_repo()
                    boundary_repo.create_source(workspace_id=workspace_id, title=file.filename or "unknown", source_id=doc.id)

                    if suffix.replace(".", "").lower() == "csv":
                        store = get_shared_table_store()
                        manifest = store.get_ingestion_manifest(doc.id, workspace_id) or {}
                        if manifest:
                            doc_ver = manifest.get("document_version") or doc_ver
                            incomplete_cov = not bool(manifest.get("coverage_complete", False))
                            exclusions = tuple(manifest.get("rejection_reasons", []))
                            persisted = int(manifest.get("persisted_rows", 0))
                            total = int(manifest.get("source_rows", 0))
                        else:
                            incomplete_cov = bool(getattr(doc, "incomplete_coverage_flag", False))
                            exclusions = tuple(getattr(doc, "exclusions", ()))
                            persisted = len(doc.chunks)
                            total = len(doc.chunks) + len(exclusions)
                    else:
                        incomplete_cov = bool(getattr(doc, "incomplete_coverage_flag", False))
                        exclusions = tuple(getattr(doc, "exclusions", ()))
                        persisted = len(doc.chunks)
                        total = len(doc.chunks) + len(exclusions)

                    pi_status, pi_count = self._enrich_page_index_snapshot_metadata(
                        workspace_id=workspace_id,
                        doc_id=doc.id,
                        doc_ver=doc_ver,
                        filename=file.filename or "unknown",
                        temp_path=path,
                        chunks=doc.chunks,
                        boundary_repo=boundary_repo,
                    )

                    snapshot = DocumentSnapshot(
                        workspace_id=workspace_id,
                        document_id=doc.id,
                        source=file.filename or "unknown",
                        version=doc_ver,
                        format=suffix.replace(".", ""),
                        incomplete_coverage_flag=incomplete_cov,
                        exclusions=exclusions,
                        persisted_rows=persisted,
                        total_rows=total,
                        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        page_index_status=pi_status,
                        page_index_elements_count=pi_count,
                    )
                    boundary_repo.save_document_snapshot(snapshot)
                except Exception as snap_err:
                    logger.warning("Could not persist DocumentSnapshot in direct ingest for %s: %s", doc.id, snap_err)

                return doc, doc_ver, incomplete_cov

            loop = asyncio.get_running_loop()
            document, version, incomplete_flag = await loop.run_in_executor(None, _sync_ingest_and_save)
            return {
                "id": document.id,
                "document_id": document.id,
                "filename": document.filename,
                "source_format": document.source_format,
                "chunk_count": len(document.chunks),
                "version": version,
                "incomplete_coverage_flag": incomplete_flag,
            }
        finally:
            path.unlink(missing_ok=True)

    def get_document(self, document_id: str) -> dict:
        document = self.repo.get(document_id)
        if document is not None:
            return document.to_dict()

        # Check in-memory / fallback workspace documents
        from db.database import _WORKSPACE_DOCS

        for ws_id, docs in _WORKSPACE_DOCS.items():
            for d in docs:
                if str(d.get("id")) == str(document_id):
                    return {
                        "id": str(document_id),
                        "filename": d.get("filename", "document"),
                        "source_format": d.get("format", ""),
                        "chunks": [],
                        "workspace_id": ws_id,
                        "status": d.get("status", "Ready"),
                        "size": d.get("size", ""),
                    }
        raise HTTPException(404, "Document not found")

    def get_document_file(self, document_id: str) -> Response:
        file_info = self.repo.get_document_file(document_id)
        if not file_info:
            target_filename = "document.pdf"
            fallback_text = ""
            doc = self.repo.get(document_id)
            if doc:
                target_filename = doc.filename
                fallback_text = "\n\n".join(c.text for c in doc.chunks)
            else:
                from db.database import _WORKSPACE_DOCS

                for ws_docs in _WORKSPACE_DOCS.values():
                    for d in ws_docs:
                        if str(d.get("id")) == str(document_id):
                            target_filename = d.get("filename", "document.pdf")
                            break

            suffix = Path(target_filename).suffix.lower()
            media_type = _resolve_media_type(target_filename)

            if suffix in [".csv", ".tsv"]:
                if not fallback_text:
                    fallback_text = "Column 1,Column 2,Column 3\nValue A,Value B,Value C\n"
                dummy_content = fallback_text.encode("utf-8")
            elif suffix in [".md", ".markdown", ".txt", ".json", ".py", ".ts", ".js"]:
                if not fallback_text:
                    fallback_text = f"# {target_filename}\nNo raw binary file cached."
                dummy_content = fallback_text.encode("utf-8")
            else:
                dummy_content = b"%PDF-1.4 ... KRE Document File"
                media_type = "application/pdf"

            return Response(
                content=dummy_content,
                media_type=media_type,
                headers={
                    "Content-Disposition": f'inline; filename="{target_filename}"',
                    "Content-Length": str(len(dummy_content)),
                    "Access-Control-Expose-Headers": "Content-Length, Content-Disposition",
                },
            )

        content, filename, media_type = file_info
        if not media_type or media_type == "application/octet-stream":
            media_type = _resolve_media_type(filename)

        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Content-Length": str(len(content)),
                "Access-Control-Expose-Headers": "Content-Length, Content-Disposition",
            },
        )


documents_service = DocumentsService()
