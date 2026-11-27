"""Regression tests for bounded ingestion timeouts, partial-coverage gating, and idempotent retries."""

import time
from unittest.mock import patch


class _FakeDocRepo:
    def __init__(self) -> None:
        self.docs = {}
        self.statuses = {}

    def get(self, doc_id: str):
        return self.docs.get(doc_id)

    def save(self, doc):
        self.docs[doc.id] = doc

    def add_placeholder_document(self, workspace_id, doc_id, filename, format_str, size_str, raw_bytes=b""):
        self.statuses[(workspace_id, doc_id)] = {"status": "processing", "error": None}

    def add_document_to_workspace(self, workspace_id, document, raw_bytes=b"", size_str=""):
        self.docs[document.id] = document

    def update_document_status(self, workspace_id, doc_id, status, chunk_count=0, error=None):
        self.statuses[(workspace_id, doc_id)] = {"status": status, "error": error, "chunk_count": chunk_count}


def test_stalled_provider_call_marks_document_failed_instead_of_hanging(tmp_path):
    from db.workspace_boundary_repo import SQLiteWorkspaceBoundaryRepository
    from modules.documents.documents_service import DocumentsService

    b_repo = SQLiteWorkspaceBoundaryRepository(str(tmp_path / "boundary.db"))
    b_repo.create_workspace("Timeout WS", "usr_alice", workspace_id="ws_timeout")

    doc_repo = _FakeDocRepo()
    svc = DocumentsService(repo=doc_repo)
    svc._ingest_timeout_s = 0.2

    sample = tmp_path / "sample.csv"
    sample.write_text("col_a,col_b\n1,2\n", encoding="utf-8")
    content = sample.read_bytes()
    doc_id = "doc_stalled_1"

    doc_repo.add_placeholder_document("ws_timeout", doc_id, "sample.csv", "csv", "1 KB", raw_bytes=content)

    def _stalled_ingest(*args, **kwargs):
        time.sleep(2.0)
        raise AssertionError("Should have timed out before returning")

    t0 = time.perf_counter()
    with (
        patch("api.routes.ingest_document", side_effect=_stalled_ingest),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        svc.background_ingest(str(sample), "ws_timeout", doc_id, "sample.csv", content, "1 KB")
    elapsed = time.perf_counter() - t0

    assert elapsed < 1.5
    rec = doc_repo.statuses.get(("ws_timeout", doc_id))
    assert rec is not None
    assert rec["status"] == "Failed"


def test_partial_coverage_cannot_publish_as_ready(tmp_path):
    from db.workspace_boundary_repo import SQLiteWorkspaceBoundaryRepository
    from modules.documents.documents_service import DocumentsService
    from schemas.models import Chunk, Document

    b_repo = SQLiteWorkspaceBoundaryRepository(str(tmp_path / "boundary.db"))
    b_repo.create_workspace("Partial WS", "usr_alice", workspace_id="ws_partial")

    doc_repo = _FakeDocRepo()
    svc = DocumentsService(repo=doc_repo)

    sample = tmp_path / "partial.pdf"
    sample.write_bytes(b"%PDF-1.4\n")
    content = sample.read_bytes()
    doc_id = "doc_partial_1"

    doc_repo.add_placeholder_document("ws_partial", doc_id, "partial.pdf", "pdf", "1 KB", raw_bytes=content)

    partial_doc = Document(
        id=doc_id,
        filename="partial.pdf",
        chunks=(
            Chunk(
                id=f"{doc_id}:p1:0",
                document_id=doc_id,
                element_type="NarrativeText",
                text="Only one chunk survived.",
                page_number=1,
                source_format="pdf",
                workspace_id="ws_partial",
            ),
        ),
        source_format="pdf",
        incomplete_coverage_flag=True,
        exclusions=("page 2 parse error",),
    )

    with (
        patch("api.routes.ingest_document", return_value=partial_doc),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        svc.background_ingest(str(sample), "ws_partial", doc_id, "partial.pdf", content, "1 KB")

    rec = doc_repo.statuses.get(("ws_partial", doc_id))
    assert rec is not None
    assert rec["status"] == "Failed"


def test_bge_lambda_batch_timeout_does_not_hang():
    import pytest
    from ingestion.embed_service import _call_bge_lambda_batch

    class _SlowLambda:
        def invoke(self, **kwargs):
            time.sleep(2.0)
            return {}

    with patch("aws.infra.get_client", return_value=_SlowLambda()):
        t0 = time.perf_counter()
        with pytest.raises(TimeoutError, match="timed out"):
            _call_bge_lambda_batch(["hello world"], batch_size=16, timeout_s=0.2, max_workers=2)
        assert time.perf_counter() - t0 < 1.5


def test_upload_retry_does_not_duplicate_source_records(tmp_path):
    from db.workspace_boundary_repo import SQLiteWorkspaceBoundaryRepository
    from modules.documents.documents_service import generate_deterministic_doc_id

    b_repo = SQLiteWorkspaceBoundaryRepository(str(tmp_path / "boundary.db"))
    b_repo.create_workspace("Retry WS", "usr_alice", workspace_id="ws_retry")

    doc_id = generate_deterministic_doc_id("bill_data.csv", workspace_id="ws_retry")
    first = b_repo.create_source("ws_retry", "bill_data.csv", source_id=doc_id)
    second = b_repo.create_source("ws_retry", "bill_data.csv", source_id=doc_id)
    assert first.source_id == second.source_id == doc_id

    rows = b_repo._conn.execute(
        "SELECT source_id FROM sources WHERE workspace_id = ? AND source_id = ?",
        ("ws_retry", doc_id),
    ).fetchall()
    assert len(rows) == 1
