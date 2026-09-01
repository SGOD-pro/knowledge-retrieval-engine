import logging
import tempfile
import uuid
from pathlib import Path
from fastapi import BackgroundTasks, HTTPException, Response, UploadFile

from config import settings
from modules.documents.documents_repository import DocumentsRepository
from ingestion.format_router import SUPPORTED_FORMATS
from ingestion.parse_service import ingest_document, generate_deterministic_doc_id

logger = logging.getLogger(__name__)


MIME_MAP = {
    ".pdf": "application/pdf",
    ".csv": "text/csv; charset=utf-8",
    ".tsv": "text/tab-separated-values; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".py": "text/x-python; charset=utf-8",
    ".ts": "text/typescript; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


class DocumentsService:
    """Service handling multi-format document ingestion, storage, and file streaming."""

    def __init__(self, repo: DocumentsRepository | None = None):
        self.repo = repo or DocumentsRepository()

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
            # persisted document (same workspace + same filename → same ID), skip
            # the expensive parse/embed/OKF pipeline and just update status to Ready.
            # To force re-ingestion, the caller must delete the document first.
            existing = self.repo.get(doc_id)
            if existing is not None:
                self.repo.update_document_status(
                    workspace_id=workspace_id,
                    doc_id=doc_id,
                    status="Ready",
                    chunk_count=len(existing.chunks),
                )
                logger.info(
                    "background_ingest.skipped_duplicate ws=%s doc_id=%s filename=%s",
                    workspace_id,
                    doc_id,
                    filename,
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

            uploaded.append(
                {
                    "id": doc_id,
                    "filename": filename,
                    "format": suffix.replace(".", ""),
                    "status": "processing",
                }
            )

        return {"uploaded_documents": uploaded}

    async def ingest_direct(self, file: UploadFile) -> dict:
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
            import api.routes as _api_routes

            ingest_fn = getattr(_api_routes, "ingest_document", ingest_document)
            document = ingest_fn(
                path,
                filename=file.filename,
                provider=settings.MODEL_PROVIDER,
                workspace_id="ws_001",
            )
            self.repo.save(document)
            self.repo.add_document_to_workspace("ws_001", document, raw_bytes=content)
            return {
                "id": document.id,
                "filename": document.filename,
                "source_format": document.source_format,
                "chunk_count": len(document.chunks),
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
            media_type = MIME_MAP.get(suffix, "text/plain; charset=utf-8")

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
        suffix = Path(filename).suffix.lower()
        if suffix in MIME_MAP:
            media_type = MIME_MAP[suffix]

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
