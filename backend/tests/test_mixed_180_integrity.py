"""End-to-end integration and integrity tests for Mixed-180 generic routing and evaluator."""

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from config import settings
from main import app
from db.workspace_boundary_repo import get_workspace_boundary_repo
from db.table_store import get_shared_table_store

client = TestClient(app)
ROOT_DIR = Path(__file__).resolve().parents[2]

ALICE_TOKEN = "test_token_alice"
ALICE_HEADERS = {"Authorization": f"Bearer {ALICE_TOKEN}"}


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    """Ensure in-memory repositories and test settings."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "test")
    monkeypatch.setattr(settings, "TABLE_STORE_BACKEND", "memory")
    monkeypatch.setenv("KRE_TABLE_STORE_BACKEND", "memory")
    monkeypatch.setattr(settings, "WORKSPACE_REPO_BACKEND", "memory")
    monkeypatch.setenv("KRE_WORKSPACE_REPO_BACKEND", "memory")
    repo = get_workspace_boundary_repo(reset=True)
    store = get_shared_table_store(reset=True)
    yield repo, store
    get_workspace_boundary_repo(reset=True)
    get_shared_table_store(reset=True)


def _setup_synthetic_workspace() -> str:
    """Create workspace and upload synthetic workforce_roster.csv."""
    res_ws = client.post(
        "/workspaces",
        json={"name": "Generic Routing Workspace"},
        headers=ALICE_HEADERS,
    )
    assert res_ws.status_code == 201
    ws_id = res_ws.json()["workspace_id"]

    csv_bytes = (
        b"employee_id,department,base_salary,bonus,hire_year\n"
        b"E1,Engineering,120000,15000,2021\n"
        b"E2,Engineering,140000,20000,2022\n"
        b"E3,Marketing,90000,10000,2022\n"
    )
    res_upload = client.post(
        f"/workspaces/{ws_id}/documents",
        files={"files": ("workforce_roster.csv", csv_bytes, "text/csv")},
        headers=ALICE_HEADERS,
    )
    assert res_upload.status_code == 201
    return ws_id


def test_generic_dataset_binding_on_renamed_dataset():
    """Verify aggregate query routes and executes generically on renamed dataset with zero model calls."""
    ws_id = _setup_synthetic_workspace()

    # Paraphrased aggregate question referencing the renamed dataset
    query = "What was the total base_salary for hire_year 2022 in the workforce roster spreadsheet?"
    resp = client.post(
        "/api/v1/query",
        json={
            "query": query,
            "workspace_id": ws_id,
            "cache": False,
            "benchmark_mode": True,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["executed_path"] == "structured_aggregate"
    # Expected sum: 140,000 + 90,000 = 230,000
    assert "230" in data["answer"]
    assert any("workforce_roster" in c.get("document_filename", "") for c in data.get("citations", []))
    assert data.get("synthesis_calls", 0) == 0
    token_usage = data.get("token_usage") or {}
    assert token_usage.get("output_tokens", 0) == 0


def test_paraphrased_delta_query_on_renamed_dataset():
    """Verify period change question routes and executes on renamed dataset with entity filter."""
    ws_id = _setup_synthetic_workspace()

    query = "How did total base_salary change from 2021 to 2022 for Engineering in workforce_roster.csv?"
    resp = client.post(
        "/api/v1/query",
        json={
            "query": query,
            "workspace_id": ws_id,
            "cache": False,
            "benchmark_mode": True,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["executed_path"] == "structured_aggregate"
    # Delta for Engineering: 140,000 - 120,000 = 20,000
    assert "20" in data["answer"]
    assert data.get("synthesis_calls", 0) == 0
    token_usage = data.get("token_usage") or {}
    assert token_usage.get("output_tokens", 0) == 0


def test_ambiguous_binding_asks_for_clarification():
    """When multiple datasets match without disambiguation, system must ask for clarification."""
    ws_id = _setup_synthetic_workspace()

    # Upload second competing dataset with base_salary and hire_year columns
    contractor_csv = (
        b"contractor_id,department,base_salary,bonus,hire_year\n"
        b"C1,Operations,75000,5000,2022\n"
    )
    res_upload2 = client.post(
        f"/workspaces/{ws_id}/documents",
        files={"files": ("contractor_roster.csv", contractor_csv, "text/csv")},
        headers=ALICE_HEADERS,
    )
    assert res_upload2.status_code == 201

    # Ambiguous query matching both workforce_roster and contractor_roster equally
    query = "What was the total base_salary for hire_year 2022 in the dataset?"
    resp = client.post(
        "/api/v1/query",
        json={
            "query": query,
            "workspace_id": ws_id,
            "cache": False,
            "benchmark_mode": True,
        },
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["executed_path"] == "structured_aggregate_ambiguous"
    assert any(term in data["answer"].lower() for term in ("clarif", "specify", "ambiguous", "multiple"))


def test_questions_only_json_isolation():
    """Verify that kre_questions_only_180.json contains zero gold data."""
    q_only_p = ROOT_DIR / "data" / "kre_questions_only_180.json"
    assert q_only_p.exists()
    data = json.loads(q_only_p.read_text(encoding="utf-8"))
    questions = data.get("questions", [])
    assert len(questions) == 180

    disallowed_keys = {
        "expected_sources",
        "preferred_answer",
        "expected_state",
        "locator",
        "ground_truth",
        "gold",
    }
    for q in questions:
        assert set(q.keys()) == {"id", "question"}
        assert not (set(q.keys()) & disallowed_keys)
