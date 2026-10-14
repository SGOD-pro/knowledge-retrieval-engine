"""Tests for Specification 0004: Text PDF Retrieval Slice.

Verifies:
1. Real text PDF ingestion through application API/service path, chunking, embeddings,
   and source/version provenance persistence in DocumentSnapshot (AC-2).
2. Partial failures (e.g. unreadable/corrupted pages) continuing ingestion while
   recording exact exclusions and setting incomplete_coverage_flag (AC-3).
3. BM25 and vector retrieval pipelines with bounded reranking returning verified
   evidence-backed answers citing exact DocumentLocation(page=..., element_id=...) (AC-5, AC-6).
4. Insufficient evidence returning graceful HTTP 200 OK with structured refusal
   and empty citations (AC-8).
5. Strict workspace boundary enforcement: cross-workspace leakage denial (HTTP 404)
   and tombstoned source immediate blocking (AC-1).
"""

import io
import os
from pathlib import Path
import sys
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from config import settings
from main import app
from src.db.workspace_boundary_repo import get_workspace_boundary_repo
from db.table_store import get_shared_table_store
from modules.documents.documents_repository import _IN_MEMORY_DOCS, _IN_MEMORY_CHUNKS

client = TestClient(app)

ALICE_TOKEN = "test_token_alice"
BOB_TOKEN = "test_token_bob"
ALICE_HEADERS = {"Authorization": f"Bearer {ALICE_TOKEN}"}
BOB_HEADERS = {"Authorization": f"Bearer {BOB_TOKEN}"}


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    """Ensure clean in-memory test substitutes and fast deterministic model provider."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "test")
    repo = get_workspace_boundary_repo(reset=True)
    store = get_shared_table_store(reset=True)
    _IN_MEMORY_DOCS.clear()
    _IN_MEMORY_CHUNKS.clear()
    yield repo, store
    get_workspace_boundary_repo(reset=True)
    get_shared_table_store(reset=True)
    _IN_MEMORY_DOCS.clear()
    _IN_MEMORY_CHUNKS.clear()


def _generate_sample_pdf_bytes(page_count: int = 2) -> bytes:
    """Extract sample pages from test corpus into an in-memory PDF."""
    source_pdf = Path(__file__).parent / "data" / "hdfc.pdf"
    reader = PdfReader(str(source_pdf))
    writer = PdfWriter()
    for i in range(min(page_count, len(reader.pages))):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_real_pdf_ingestion_and_provenance_snapshot():
    """Ingest actual text PDF, verify DocumentSnapshot provenance and complete coverage."""
    repo = get_workspace_boundary_repo()

    # 1. Create workspace for Alice
    res_ws = client.post(
        "/workspaces",
        json={"name": "Alice Research Workspace"},
        headers=ALICE_HEADERS,
    )
    assert res_ws.status_code == 201
    ws_id = res_ws.json()["workspace_id"]

    # 2. Upload actual text PDF
    pdf_bytes = _generate_sample_pdf_bytes(page_count=2)
    res_upload = client.post(
        f"/workspaces/{ws_id}/documents",
        files={"files": ("fund_handbook.pdf", pdf_bytes, "application/pdf")},
        headers=ALICE_HEADERS,
    )
    assert res_upload.status_code == 201
    upload_data = res_upload.json()
    items = upload_data.get("items") or upload_data.get("uploaded_documents", [])
    assert len(items) == 1
    doc_id = items[0]["id"]
    doc_version = items[0]["version"]

    # 3. Verify DocumentSnapshot persisted with provenance and complete coverage
    snapshot = repo.get_document_snapshot(ws_id, doc_id)
    assert snapshot is not None
    assert snapshot.workspace_id == ws_id
    assert snapshot.document_id == doc_id
    assert snapshot.source == "fund_handbook.pdf"
    assert snapshot.version == doc_version
    assert snapshot.format == "pdf"
    assert snapshot.incomplete_coverage_flag is False
    assert snapshot.exclusions == ()
    assert snapshot.persisted_rows > 0
    assert snapshot.total_rows == snapshot.persisted_rows


def test_pdf_retrieval_query_with_cited_document_location():
    """Execute text query against ingested PDF, returning verified cited answer with DocumentLocation."""
    # 1. Create workspace and upload PDF
    res_ws = client.post(
        "/workspaces",
        json={"name": "Alice Handbook Workspace"},
        headers=ALICE_HEADERS,
    )
    ws_id = res_ws.json()["workspace_id"]

    pdf_bytes = _generate_sample_pdf_bytes(page_count=2)
    res_upload = client.post(
        f"/workspaces/{ws_id}/documents",
        files={"files": ("handbook.pdf", pdf_bytes, "application/pdf")},
        headers=ALICE_HEADERS,
    )
    doc_id = res_upload.json()["items"][0]["id"]

    # 2. Query content that exists in the PDF (Mission statement on Page 1)
    query_payload = {
        "query": "What is the mission in the mutual fund handbook?",
        "snapshot_id": "snap_pdf_test",
    }
    res_query = client.post(
        f"/api/v1/workspaces/{ws_id}/query",
        json=query_payload,
        headers=ALICE_HEADERS,
    )
    assert res_query.status_code == 200
    outcome = res_query.json()
    assert outcome["status"] == "SUCCESS"
    assert len(outcome["answer"]) > 0
    assert len(outcome["citations"]) > 0

    # 3. Verify citation contains typed DocumentLocation
    cit = outcome["citations"][0]
    assert cit["source_id"] == doc_id
    assert len(cit["snippet"]) > 0
    loc = cit["location"]
    assert loc["type"] == "document"
    assert loc["page"] == 1
    assert loc["element_id"] is not None


def test_pdf_partial_failure_unreadable_page_tracking():
    """Simulate unreadable page in PDF: ingestion completes and records exclusions + incomplete coverage."""
    repo = get_workspace_boundary_repo()

    res_ws = client.post(
        "/workspaces",
        json={"name": "Alice Corrupted PDF Workspace"},
        headers=ALICE_HEADERS,
    )
    ws_id = res_ws.json()["workspace_id"]

    pdf_bytes = _generate_sample_pdf_bytes(page_count=2)

    # Patch extract_text on page 2 to raise an error simulating an unreadable or encrypted page
    real_extract_with_pypdf = sys.modules["ingestion.adapters.pdf_adapter"]._extract_with_pypdf

    def mocked_extract_with_pypdf(path, document_id):
        # Call original but simulate a page error on page 2
        elements, exclusions = real_extract_with_pypdf(path, document_id)
        # Keep only page 1 elements and record page 2 as excluded
        p1_elements = [e for e in elements if e.get("page_number") == 1]
        simulated_exclusions = ["Page 2: unreadable or corrupted page (Corrupted stream)"]
        return p1_elements, simulated_exclusions

    with patch("ingestion.adapters.pdf_adapter._extract_with_pypdf", side_effect=mocked_extract_with_pypdf):
        res_upload = client.post(
            f"/workspaces/{ws_id}/documents",
            files={"files": ("partial_doc.pdf", pdf_bytes, "application/pdf")},
            headers=ALICE_HEADERS,
        )
        assert res_upload.status_code == 201
        doc_id = res_upload.json()["items"][0]["id"]

    # Verify DocumentSnapshot recorded the exclusion and marked incomplete coverage
    snapshot = repo.get_document_snapshot(ws_id, doc_id)
    assert snapshot is not None
    assert snapshot.incomplete_coverage_flag is True
    assert len(snapshot.exclusions) == 1
    assert "Page 2" in snapshot.exclusions[0]
    assert snapshot.persisted_rows > 0
    assert snapshot.total_rows == snapshot.persisted_rows + 1


def test_pdf_cross_workspace_boundary_denial():
    """Bob in ws_beta cannot access Alice's PDF or receive Alice's PDF evidence."""
    # Alice uploads PDF in ws_alpha
    res_alice_ws = client.post(
        "/workspaces",
        json={"name": "Alice Private Workspace"},
        headers=ALICE_HEADERS,
    )
    alice_ws_id = res_alice_ws.json()["workspace_id"]

    pdf_bytes = _generate_sample_pdf_bytes(page_count=2)
    client.post(
        f"/workspaces/{alice_ws_id}/documents",
        files={"files": ("private_handbook.pdf", pdf_bytes, "application/pdf")},
        headers=ALICE_HEADERS,
    )

    # Bob creates his own workspace
    res_bob_ws = client.post(
        "/workspaces",
        json={"name": "Bob Separate Workspace"},
        headers=BOB_HEADERS,
    )
    bob_ws_id = res_bob_ws.json()["workspace_id"]

    # 1. Bob attempts to query Alice's workspace directly -> HTTP 404
    query_payload = {"query": "What is the mission?", "snapshot_id": "snap_default"}
    res_forbidden = client.post(
        f"/api/v1/workspaces/{alice_ws_id}/query",
        json=query_payload,
        headers=BOB_HEADERS,
    )
    assert res_forbidden.status_code == 404

    # 2. Bob queries his own empty workspace -> 200 OK with refusal and empty citations
    res_bob_query = client.post(
        f"/api/v1/workspaces/{bob_ws_id}/query",
        json=query_payload,
        headers=BOB_HEADERS,
    )
    assert res_bob_query.status_code == 200
    outcome = res_bob_query.json()
    assert outcome["status"] == "SUCCESS"
    assert outcome["citations"] == []
    assert "couldn't find" in outcome["answer"].lower()


def test_pdf_insufficient_evidence_structured_refusal():
    """Querying for facts not present in the ingested PDF returns 200 OK structured refusal."""
    res_ws = client.post(
        "/workspaces",
        json={"name": "Alice Handbook QnA"},
        headers=ALICE_HEADERS,
    )
    ws_id = res_ws.json()["workspace_id"]

    pdf_bytes = _generate_sample_pdf_bytes(page_count=2)
    client.post(
        f"/workspaces/{ws_id}/documents",
        files={"files": ("handbook.pdf", pdf_bytes, "application/pdf")},
        headers=ALICE_HEADERS,
    )

    # Ask for an unrelated fact not in the mutual fund handbook
    query_payload = {
        "query": "What is the exact recipe for baking sourdough bread?",
        "snapshot_id": "snap_handbook",
    }
    res_query = client.post(
        f"/api/v1/workspaces/{ws_id}/query",
        json=query_payload,
        headers=ALICE_HEADERS,
    )
    assert res_query.status_code == 200
    outcome = res_query.json()
    assert outcome["status"] == "SUCCESS"
    assert outcome["citations"] == []
    assert "couldn't find" in outcome["answer"].lower()


def test_pdf_tombstone_blocks_retrieval_and_citations():
    """Tombstoning a PDF source immediately blocks direct access and prevents it from being cited."""
    res_ws = client.post(
        "/workspaces",
        json={"name": "Alice Deletion Workspace"},
        headers=ALICE_HEADERS,
    )
    ws_id = res_ws.json()["workspace_id"]

    pdf_bytes = _generate_sample_pdf_bytes(page_count=2)
    res_upload = client.post(
        f"/workspaces/{ws_id}/documents",
        files={"files": ("handbook.pdf", pdf_bytes, "application/pdf")},
        headers=ALICE_HEADERS,
    )
    doc_id = res_upload.json()["items"][0]["id"]

    # Verify query initially succeeds and cites the PDF
    query_payload = {
        "query": "What is the mission in the mutual fund handbook?",
        "snapshot_id": "snap_pdf_del",
    }
    res_before = client.post(
        f"/api/v1/workspaces/{ws_id}/query",
        json=query_payload,
        headers=ALICE_HEADERS,
    )
    assert res_before.status_code == 200
    assert len(res_before.json()["citations"]) > 0

    # Delete (tombstone) the source
    del_res = client.delete(
        f"/api/v1/workspaces/{ws_id}/sources/{doc_id}",
        headers=ALICE_HEADERS,
    )
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "tombstoned"

    # Query again: tombstoned source is blocked and zero citations are returned
    res_after = client.post(
        f"/api/v1/workspaces/{ws_id}/query",
        json=query_payload,
        headers=ALICE_HEADERS,
    )
    assert res_after.status_code == 200
    outcome = res_after.json()
    assert outcome["citations"] == []
    assert "couldn't find" in outcome["answer"].lower()
