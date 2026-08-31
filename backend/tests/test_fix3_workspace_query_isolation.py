from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from main import app
from db.database import CloudRepository
from schemas.models import Chunk, Document
from ingestion.embed_service import embed_fast_local, _deterministic_vector

client = TestClient(app)


def test_query_isolated_by_workspace(monkeypatch):
    """Regression Test: Multi-tenant workspace isolation.
    Querying workspace ws_1 never returns chunks or citations from workspace ws_2,
    and vice versa.
    """
    monkeypatch.setenv("ENVIRONMENT", "test")
    import db.database as db_mod
    db_mod._IN_MEMORY_CHUNKS.clear()
    db_mod._IN_MEMORY_DOCS.clear()
    db_mod._WORKSPACES.clear()
    db_mod._WORKSPACE_DOCS.clear()
    db_mod._DOCUMENT_FILES.clear()

    repo = CloudRepository()

    ws_1 = "ws_isolation_1"
    ws_2 = "ws_isolation_2"

    repo.create_workspace(name="Workspace Alpha", workspace_id=ws_1)
    repo.create_workspace(name="Workspace Beta", workspace_id=ws_2)

    query_a = "What is the telescope telemetry policy?"
    query_b = "What is the derivatives risk policy?"

    text_a1 = query_a
    text_a2 = "The observatory optical sensors operate at cryogenic temperatures in deep space."
    text_a3 = "Primary mirror alignment is recalibrated daily using laser interferometry."
    text_a4 = "Galactic redshift measurements are cataloged in the astrophysical repository database."

    text_b1 = query_b
    text_b2 = "Financial risk modeling evaluates credit default swaps and counterparty risk exposure."
    text_b3 = "Treasury asset yield curves dictate capital adequacy ratios under Basel III regulations."
    text_b4 = "Commercial lending collateral requirements mandate quarterly audit verifications."

    doc_a = Document(
        id="doc_alpha_101",
        filename="alpha_telescope.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_alpha_101:c1",
                document_id="doc_alpha_101",
                text=text_a1,
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("Telemetry",),
                embedding_fast=embed_fast_local(query_a),
                embedding_full=_deterministic_vector(query_a, 1024),
                workspace_id=ws_1,
            ),
            Chunk(
                id="doc_alpha_101:c2",
                document_id="doc_alpha_101",
                text=text_a2,
                source_format="pdf",
                page_number=2,
                element_type="paragraph",
                section_path=("Sensors",),
                embedding_fast=embed_fast_local(text_a2),
                embedding_full=_deterministic_vector(text_a2, 1024),
                workspace_id=ws_1,
            ),
            Chunk(
                id="doc_alpha_101:c3",
                document_id="doc_alpha_101",
                text=text_a3,
                source_format="pdf",
                page_number=3,
                element_type="paragraph",
                section_path=("Mirror",),
                embedding_fast=embed_fast_local(text_a3),
                embedding_full=_deterministic_vector(text_a3, 1024),
                workspace_id=ws_1,
            ),
            Chunk(
                id="doc_alpha_101:c4",
                document_id="doc_alpha_101",
                text=text_a4,
                source_format="pdf",
                page_number=4,
                element_type="paragraph",
                section_path=("Database",),
                embedding_fast=embed_fast_local(text_a4),
                embedding_full=_deterministic_vector(text_a4, 1024),
                workspace_id=ws_1,
            ),
        ),
        workspace_id=ws_1,
    )
    repo.save(doc_a)
    repo.add_document_to_workspace(ws_1, doc_a, raw_bytes=b"telescope content")

    doc_b = Document(
        id="doc_beta_202",
        filename="beta_derivatives.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_beta_202:c1",
                document_id="doc_beta_202",
                text=text_b1,
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("Derivatives",),
                embedding_fast=embed_fast_local(query_b),
                embedding_full=_deterministic_vector(query_b, 1024),
                workspace_id=ws_2,
            ),
            Chunk(
                id="doc_beta_202:c2",
                document_id="doc_beta_202",
                text=text_b2,
                source_format="pdf",
                page_number=2,
                element_type="paragraph",
                section_path=("Risk",),
                embedding_fast=embed_fast_local(text_b2),
                embedding_full=_deterministic_vector(text_b2, 1024),
                workspace_id=ws_2,
            ),
            Chunk(
                id="doc_beta_202:c3",
                document_id="doc_beta_202",
                text=text_b3,
                source_format="pdf",
                page_number=3,
                element_type="paragraph",
                section_path=("Treasury",),
                embedding_fast=embed_fast_local(text_b3),
                embedding_full=_deterministic_vector(text_b3, 1024),
                workspace_id=ws_2,
            ),
            Chunk(
                id="doc_beta_202:c4",
                document_id="doc_beta_202",
                text=text_b4,
                source_format="pdf",
                page_number=4,
                element_type="paragraph",
                section_path=("Audit",),
                embedding_fast=embed_fast_local(text_b4),
                embedding_full=_deterministic_vector(text_b4, 1024),
                workspace_id=ws_2,
            ),
        ),
        workspace_id=ws_2,
    )
    repo.save(doc_b)
    repo.add_document_to_workspace(ws_2, doc_b, raw_bytes=b"derivatives content")

    # 1. Query ws_1 using a term that ONLY exists in doc B ("derivatives risk policy")
    resp_cross = client.post(
        "/api/v1/query",
        json={"workspace_id": ws_1, "query": "What is the derivatives risk policy?"},
    )
    assert resp_cross.status_code == 200
    data_cross = resp_cross.json()
    assert len(data_cross.get("citations", [])) == 0, f"Expected 0 citations from ws_2 doc in ws_1 query, got {data_cross.get('citations')}"
    for cit in data_cross.get("citations", []):
        assert cit["document_id"] != "doc_beta_202"

    # 2. Query ws_1 using a term from doc A ("telescope telemetry policy")
    resp_a = client.post(
        "/api/v1/query",
        json={"workspace_id": ws_1, "query": "What is the telescope telemetry policy?"},
    )
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    assert len(data_a.get("citations", [])) > 0, "Expected citations from doc A in ws_1 query"
    assert data_a["citations"][0]["document_id"] == "doc_alpha_101"

    # 3. Repeat in reverse for ws_2: query ws_2 with doc A term ("telescope telemetry policy")
    resp_cross_rev = client.post(
        "/api/v1/query",
        json={"workspace_id": ws_2, "query": "What is the telescope telemetry policy?"},
    )
    assert resp_cross_rev.status_code == 200
    data_cross_rev = resp_cross_rev.json()
    assert len(data_cross_rev.get("citations", [])) == 0, f"Expected 0 citations from ws_1 doc in ws_2 query, got {data_cross_rev.get('citations')}"
    for cit in data_cross_rev.get("citations", []):
        assert cit["document_id"] != "doc_alpha_101"

    # 4. Query ws_2 with doc B term -> assert found
    resp_b = client.post(
        "/api/v1/query",
        json={"workspace_id": ws_2, "query": "What is the derivatives risk policy?"},
    )
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    assert len(data_b.get("citations", [])) > 0, "Expected citations from doc B in ws_2 query"
    assert data_b["citations"][0]["document_id"] == "doc_beta_202"


def test_query_missing_workspace_id_rejected():
    """Regression Test: Missing workspace_id must return 400 or 422 immediately,
    and pipeline.run must never be called.
    """
    with patch("services.langgraph_pipeline.pipeline.run") as mock_pipeline_run:
        # POST without workspace_id
        resp = client.post("/api/v1/query", json={"query": "What is the refund policy?"})
        assert resp.status_code in (400, 422), f"Expected 400 or 422, got {resp.status_code}"
        mock_pipeline_run.assert_not_called()

        # POST with empty workspace_id
        resp_empty = client.post("/api/v1/query", json={"workspace_id": "", "query": "What is the refund policy?"})
        assert resp_empty.status_code in (400, 422), f"Expected 400 or 422, got {resp_empty.status_code}"
        mock_pipeline_run.assert_not_called()
