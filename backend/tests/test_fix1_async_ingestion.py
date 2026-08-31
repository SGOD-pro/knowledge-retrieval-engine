import io
import time
from pathlib import Path
from unittest.mock import patch
import pytest
from fastapi import BackgroundTasks, UploadFile
from fastapi.testclient import TestClient

from api.routes import upload_workspace_documents_endpoint, _background_ingest
from db.database import CloudRepository
from main import app
from schemas.models import Document, Chunk

client = TestClient(app)

@pytest.mark.anyio
async def test_async_ingestion_endpoint_returns_fast_and_updates_status():
    ws_id = f"test_ws_async_{int(time.time()*1000)}"
    repo = CloudRepository()

    def slow_ingest_document(path, *args, **kwargs):
        time.sleep(0.6)
        ws = kwargs.get("workspace_id", ws_id)
        return Document(
            id="doc_async_test_123",
            filename="large_paper.pdf",
            source_format="pdf",
            chunks=(
                Chunk(
                    id="doc_async_test_123:c1",
                    document_id="doc_async_test_123",
                    text="Async test chunk content",
                    source_format="pdf",
                    page_number=1,
                    element_type="paragraph",
                    section_path=("Root",),
                    embedding_fast=[0.1] * 384,
                    embedding_full=[0.1] * 1024,
                    workspace_id=ws,
                ),
            ),
            workspace_id=ws,
        )

    fake_file_content = b"%PDF-1.5 test document content"
    upload_file = UploadFile(filename="large_paper.pdf", file=io.BytesIO(fake_file_content))
    bg_tasks = BackgroundTasks()

    with patch("api.routes.ingest_document", side_effect=slow_ingest_document):
        t0 = time.perf_counter()
        resp = await upload_workspace_documents_endpoint(
            workspace_id=ws_id,
            background_tasks=bg_tasks,
            files=[upload_file],
        )
        elapsed = time.perf_counter() - t0

        # Assert POST endpoint logic returns immediately (< 500ms)
        assert elapsed < 0.5, f"Expected endpoint to return in <500ms, took {elapsed:.3f}s"
        assert "uploaded_documents" in resp
        assert len(resp["uploaded_documents"]) == 1
        doc_info = resp["uploaded_documents"][0]
        assert doc_info["status"].lower() == "processing"
        doc_id = doc_info["id"]

        # Polling: initially status is processing
        docs_resp = repo.get_workspace_documents(ws_id)
        assert len(docs_resp["documents"]) == 1
        assert docs_resp["documents"][0]["status"].lower() == "processing"
        assert docs_resp["documents"][0]["chunk_count"] == 0

        # Run the queued background tasks
        await bg_tasks()

        # Polling after completion: status is Ready and chunk_count updated
        docs_resp_after = repo.get_workspace_documents(ws_id)
        assert docs_resp_after["documents"][0]["status"] == "Ready"
        assert docs_resp_after["documents"][0]["chunk_count"] == 1


@pytest.mark.anyio
async def test_async_ingestion_failure_records_failed_status():
    ws_id = f"test_ws_fail_{int(time.time()*1000)}"
    repo = CloudRepository()

    def failing_ingest(path, *args, **kwargs):
        raise ValueError("Corrupt PDF structure or OCR timeout")

    upload_file = UploadFile(filename="corrupt.pdf", file=io.BytesIO(b"corrupt bytes"))
    bg_tasks = BackgroundTasks()

    with patch("api.routes.ingest_document", side_effect=failing_ingest):
        resp = await upload_workspace_documents_endpoint(
            workspace_id=ws_id,
            background_tasks=bg_tasks,
            files=[upload_file],
        )
        doc_id = resp["uploaded_documents"][0]["id"]
        assert resp["uploaded_documents"][0]["status"] == "processing"

        # Execute background task where exception occurs
        await bg_tasks()

        # Status must be Failed, error recorded, never lost silently
        docs_resp = repo.get_workspace_documents(ws_id)
        assert docs_resp["documents"][0]["status"] == "Failed"
        assert "Corrupt PDF structure" in docs_resp["documents"][0].get("error", "")
