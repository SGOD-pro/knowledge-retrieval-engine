import logging
import tempfile
import uuid
from pathlib import Path
from fastapi import BackgroundTasks, HTTPException, Response, UploadFile

from config import settings
from db.database import CloudRepository
from ingestion.format_router import SUPPORTED_FORMATS
from ingestion.parse_service import ingest_document

logger = logging.getLogger(__name__)


class DocumentsService:
    """Service handling multi-format document ingestion, storage, and file streaming."""

    def __init__(self, repo: CloudRepository | None = None):
        self.repo = repo or CloudRepository()

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
            document = ingest_fn(
                temp_path,
                document_id=doc_id,
                filename=filename,
                provider=settings.MODEL_PROVIDER,
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
            size_kb = len(content) / 1024.0
            size_str = (
                f"{size_kb / 1024.0:.1f} MB" if size_kb >= 1024 else f"{size_kb:.0f} KB"
            )
            doc_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, filename))

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
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
            temporary.write(content)
            path = Path(temporary.name)
        try:
            import api.routes as _api_routes

            ingest_fn = getattr(_api_routes, "ingest_document", ingest_document)
            document = ingest_fn(
                path, filename=file.filename, provider=settings.MODEL_PROVIDER
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
        if document is None:
            raise HTTPException(404, "Document not found")
        return document.to_dict()

    def get_document_file(self, document_id: str) -> Response:
        file_info = self.repo.get_document_file(document_id)
        if not file_info:
            dummy_content = b"%PDF-1.4 ... KRE Document File"
            return Response(content=dummy_content, media_type="application/pdf")

        content, filename, media_type = file_info
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )


documents_service = DocumentsService()
