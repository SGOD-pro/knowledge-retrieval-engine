import io
import time
import pytest
from fastapi.testclient import TestClient
from main import app
from schemas.models import Document, Chunk
from db.database import CloudRepository

client = TestClient(app)

def test_workspace_deletion_and_isolation(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    repo = CloudRepository()
    ws_a = f"ws_a_{int(time.time()*1000)}"
    ws_b = f"ws_b_{int(time.time()*1000)}"

    repo.create_workspace(name="Workspace A", workspace_id=ws_a)
    repo.create_workspace(name="Workspace B", workspace_id=ws_b)

    # Ingest document B into Workspace B
    doc_b = Document(
        id="doc_b_quantum_123",
        filename="quantum_paper.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_b_quantum_123:c1",
                document_id="doc_b_quantum_123",
                text="Quantum computing entanglement experiments demonstrate superdense coding and qubit teleportation.",
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path="1. Intro",
                embedding_fast=[0.9] * 384,
                embedding_full=[0.9] * 1024,
            ),
        ),
    )
    repo.save(doc_b)
    repo.add_document_to_workspace(ws_b, doc_b, raw_bytes=b"fake b")

    # Ingest document A into Workspace A
    doc_a = Document(
        id="doc_a_finance_123",
        filename="finance_report.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_a_finance_123:c1",
                document_id="doc_a_finance_123",
                text="Quarterly fiscal balance sheet showing revenue and EBITDA growth.",
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path="1. Summary",
                embedding_fast=[-0.8] * 384,
                embedding_full=[-0.8] * 1024,
            ),
        ),
    )
    repo.save(doc_a)
    repo.add_document_to_workspace(ws_a, doc_a, raw_bytes=b"fake a")

    # 1. Query Workspace A for Quantum concepts -> MUST NOT return Doc B (Isolation test)
    query_resp = client.post(
        "/api/v1/query",
        json={"workspace_id": ws_a, "query": "Quantum computing entanglement experiments"},
    )
    assert query_resp.status_code == 200
    q_data = query_resp.json()
    # Ensure no citation from doc_b appears in query results for workspace A
    for cit in q_data.get("citations", []):
        assert cit.get("document_id") != "doc_b_quantum_123"

    # 2. Delete Workspace B
    del_resp = client.delete(f"/api/v1/workspaces/{ws_b}")
    assert del_resp.status_code in [200, 204]

    # 3. Document B must now be unretrievable
    get_doc_resp = client.get(f"/api/v1/documents/{doc_b.id}")
    assert get_doc_resp.status_code == 404
