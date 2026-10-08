from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_cors_headers_present_on_api_response():
    response = client.options(
        "/query",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") in [
        "http://localhost:5173",
        "*",
    ]


def test_upload_over_50mb_returns_413_before_parsing(monkeypatch):
    large_data = b"x" * (50 * 1024 * 1024 + 1)
    response = client.post(
        "/ingest",
        files={"file": ("large_file.csv", large_data, "text/csv")},
    )
    assert response.status_code == 413
    assert "Payload Too Large" in response.text


def test_unauthenticated_request_returns_401_before_retrieval(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")

    # Missing header
    res_no_auth = client.post("/query", json={"query": "test query", "workspace_id": "ws_default"})
    assert res_no_auth.status_code == 401

    # Invalid header token
    res_bad_auth = client.post(
        "/query",
        json={"query": "test query", "workspace_id": "ws_default"},
        headers={"Authorization": "Bearer invalid-secret"},
    )
    assert res_bad_auth.status_code == 401


def test_api_query_handles_not_found():
    response = client.get("/documents/nonexistent-id-12345")
    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found"


def test_api_query_success_renders_citations(monkeypatch):
    # Mock pipeline execution for predictable endpoint test
    from services.langgraph_pipeline import pipeline
    from db.database import CloudRepository
    from schemas.models import Document, Chunk

    repo = CloudRepository()
    doc = Document(
        id="doc-123",
        filename="test.pdf",
        source_format="pdf",
        workspace_id="ws_default",
        chunks=(
            Chunk(
                id="doc-123:c1",
                document_id="doc-123",
                text="Test chunk text",
                source_format="pdf",
                element_type="paragraph",
                workspace_id="ws_default",
                embedding_fast=[0.1] * 384,
                embedding_full=[0.1] * 1024,
            ),
        ),
    )
    repo.save(doc)
    repo.add_document_to_workspace("ws_default", doc)

    class MockPipelineResponse:
        answer = "This is a test answer from Phase 4."
        citations = [
            {
                "id": "cit-1",
                "snippet": "Test snippet text",
                "document_id": "doc-123",
                "location_reference": "Page 1",
                "source_format": "pdf",
            }
        ]
        confidence_score = 0.95
        fast_path = True
        top_chunks = []

    monkeypatch.setattr(
        pipeline, "run", lambda query, document_ids=None, workspace_id="ws_default": MockPipelineResponse()
    )

    response = client.post("/query", json={"query": "test query", "workspace_id": "ws_default"})
    assert response.status_code == 200
    data = response.json()
    assert data["answer"] == "This is a test answer from Phase 4."
    assert len(data["citations"]) == 1
    assert data["citations"][0]["source_format"] == "pdf"
    assert data["confidence_score"] == 0.95
