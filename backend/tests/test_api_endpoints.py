import pytest
from fastapi.testclient import TestClient
import sys
from pathlib import Path

# Add src directory to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import app

client = TestClient(app)


def test_auth_login_endpoint():
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "alexandra.chen@enterprise.com", "password": "password123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["user"]["email"] == "alexandra.chen@enterprise.com"
    assert data["user"]["id"] == "usr_54321"


def test_workspaces_crud_endpoints():
    # 1. Create new workspace
    new_ws_req = {
        "name": "Candidate Screening Q3",
        "industry": "Technology",
        "description": "Portfolios and technical resumes for senior engineers",
    }
    resp = client.post("/api/v1/workspaces", json=new_ws_req)
    assert resp.status_code == 201
    ws = resp.json()
    assert ws["name"] == "Candidate Screening Q3"
    assert ws["document_count"] == 0
    assert ws["id"].startswith("ws_")

    # 2. Get workspaces
    resp = client.get("/api/v1/workspaces")
    assert resp.status_code == 200
    data = resp.json()
    assert "workspaces" in data
    assert any(w["id"] == ws["id"] for w in data["workspaces"])


def test_system_benchmarks_endpoint():
    resp = client.get("/api/v1/system/benchmarks")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "PASSING ALL"
    assert data["version"] == "v2.4.1"
    kpis = data["kpis"]
    assert kpis["p95_latency"]["value"] == 3.47
    assert kpis["recall_5"]["value"] == 79.22
    assert kpis["faithfulness"]["value"] == 99.59
    assert kpis["llm_activation"]["value"] == 50.65
    assert len(data["latency_chart"]["data_points"]) >= 7


def test_knowledge_graph_endpoint():
    resp = client.get("/api/v1/workspaces/ws_test/graph")
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "edges" in data


def test_document_file_endpoint():
    resp = client.get("/api/v1/documents/doc_test/file")
    assert resp.status_code == 200
    assert len(resp.content) > 0
