"""Tests for Specification 0004: First CSV Vertical Slice.

Verifies:
1. Real CSV ingestion through application API/service path, dataset and source/version
   provenance persistence in DocumentSnapshot, and correct cited aggregate computation.
2. Dataset large enough (250 rows > 99 row batch size) catching previous row truncation.
3. Incomplete coverage (malformed rows/rejections) strictly blocking complete aggregate claims (HTTP 422).
4. Cross-workspace access denial (HTTP 404) and deleted-source denial (tombstone blocks aggregate query).
5. Authoritative storage failure (HTTP 503) remaining strictly distinct from insufficient evidence (HTTP 200).
"""

from decimal import Decimal
import io
import os
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from config import settings
from main import app
from src.db.workspace_boundary_repo import (
    InMemoryWorkspaceBoundaryRepository,
    get_workspace_boundary_repo,
)
from db.table_store import get_shared_table_store
from src.schemas.contracts.boundary import StorageUnavailableError

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
    yield repo, store
    get_workspace_boundary_repo(reset=True)
    get_shared_table_store(reset=True)


def test_real_csv_ingestion_and_correct_cited_aggregate():
    """Ingest actual CSV via API, verify DocumentSnapshot provenance, and verify cited aggregate."""
    repo = get_workspace_boundary_repo()

    # 1. Create workspace for Alice
    res_ws = client.post(
        "/workspaces",
        json={"name": "Alice Analytics Workspace"},
        headers=ALICE_HEADERS,
    )
    assert res_ws.status_code == 201
    ws_id = res_ws.json()["workspace_id"]

    # 2. Upload actual CSV dataset
    csv_bytes = (
        b"year,amount,industry\n"
        b"2020,150,agriculture\n"
        b"2021,200,manufacturing\n"
        b"2022,350,services\n"
        b"2023,500,tech\n"
        b"2024,650,tech\n"
    )

    res_upload = client.post(
        f"/workspaces/{ws_id}/documents",
        files={"files": ("industry_growth.csv", csv_bytes, "text/csv")},
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
    assert snapshot.source == "industry_growth.csv"
    assert snapshot.version == doc_version
    assert snapshot.format == "csv"
    assert snapshot.incomplete_coverage_flag is False
    assert snapshot.exclusions == ()
    assert snapshot.persisted_rows == 5
    assert snapshot.total_rows == 5

    # 4. Execute restricted deterministic aggregate query through query delivery gate
    res_query = client.post(
        f"/workspaces/{ws_id}/query",
        json={"query": "What is the earliest and latest year in industry_growth.csv?"},
        headers=ALICE_HEADERS,
    )
    assert res_query.status_code == 200
    query_data = res_query.json()
    assert query_data["status"] == "SUCCESS"
    assert query_data["answer"] == "2020 and 2024."
    assert len(query_data["citations"]) >= 1

    citation = query_data["citations"][0]
    assert citation["source_id"] == doc_id
    assert citation["source_version"] >= 1
    assert citation["location"]["type"] == "table"
    assert citation["location"]["table_id"] == "industry_growth"
    assert "industry_growth" in citation["location"]["row_id"]
    assert doc_id in query_data["surviving_source_ids"]


def test_large_dataset_proves_no_row_truncation():
    """Verify full ingestion and aggregation over 250 rows, exceeding the 99-row batch ceiling."""
    repo = get_workspace_boundary_repo()

    res_ws = client.post(
        "/workspaces",
        json={"name": "Alice Large Dataset Workspace"},
        headers=ALICE_HEADERS,
    )
    assert res_ws.status_code == 201
    ws_id = res_ws.json()["workspace_id"]

    # Generate 250 rows: year 1800 to 2049
    lines = [b"year,amount,division"]
    for i in range(250):
        yr = 1800 + i
        amt = 10 + (i % 50)
        lines.append(f"{yr},{amt},div_{i % 5}".encode("utf-8"))
    large_csv_bytes = b"\n".join(lines) + b"\n"

    res_upload = client.post(
        f"/workspaces/{ws_id}/documents",
        files={"files": ("large_timeseries.csv", large_csv_bytes, "text/csv")},
        headers=ALICE_HEADERS,
    )
    assert res_upload.status_code == 201
    doc_id = res_upload.json()["items"][0]["id"]

    # Verify DocumentSnapshot recorded all 250 persisted rows without truncation
    snapshot = repo.get_document_snapshot(ws_id, doc_id)
    assert snapshot is not None
    assert snapshot.persisted_rows == 250
    assert snapshot.total_rows == 250
    assert snapshot.incomplete_coverage_flag is False

    # Execute aggregate: earliest and latest year across the entire 250 rows
    res_query = client.post(
        f"/workspaces/{ws_id}/query",
        json={"query": "What is the earliest and latest year in large_timeseries.csv?"},
        headers=ALICE_HEADERS,
    )
    assert res_query.status_code == 200
    query_data = res_query.json()
    assert query_data["status"] == "SUCCESS"
    # If truncated at row 99, latest would be 1898 instead of 2049
    assert query_data["answer"] == "1800 and 2049."


def test_incomplete_coverage_blocks_aggregate_claim():
    """Malformed CSV rows record exclusions and incomplete coverage flag blocks aggregate queries with HTTP 422."""
    repo = get_workspace_boundary_repo()

    res_ws = client.post(
        "/workspaces",
        json={"name": "Alice Malformed Data Workspace"},
        headers=ALICE_HEADERS,
    )
    assert res_ws.status_code == 201
    ws_id = res_ws.json()["workspace_id"]

    # Row 2 has column count mismatch (missing values)
    malformed_csv = (
        b"year,amount,industry\n"
        b"2021,100,tech\n"
        b"2022,250\n"
        b"2023,400,tech\n"
    )

    res_upload = client.post(
        f"/workspaces/{ws_id}/documents",
        files={"files": ("malformed_report.csv", malformed_csv, "text/csv")},
        headers=ALICE_HEADERS,
    )
    assert res_upload.status_code == 201
    doc_id = res_upload.json()["items"][0]["id"]

    # Ingestion recorded the malformed row exclusion and set incomplete_coverage_flag=True
    snapshot = repo.get_document_snapshot(ws_id, doc_id)
    assert snapshot is not None
    assert snapshot.incomplete_coverage_flag is True
    assert snapshot.persisted_rows == 2
    assert snapshot.total_rows == 3
    assert len(snapshot.exclusions) == 1
    assert "Column count mismatch" in snapshot.exclusions[0]

    # Querying aggregate over incomplete table fails closed with HTTP 422
    res_query = client.post(
        f"/workspaces/{ws_id}/query",
        json={"query": "What is the earliest and latest year in malformed_report.csv?"},
        headers=ALICE_HEADERS,
    )
    assert res_query.status_code == 422
    err_detail = res_query.json()["detail"]
    assert err_detail["type"] == "urn:kre:error:insufficient_coverage"
    assert err_detail["status"] == 422
    assert "incomplete" in err_detail["detail"].lower()


def test_cross_workspace_denial_and_deleted_source_denial():
    """Verify foreign callers receive HTTP 404, and deleted/tombstoned sources block queries with HTTP 422."""
    # 1. Alice creates workspace and uploads CSV
    res_ws = client.post(
        "/workspaces",
        json={"name": "Alice Private Workspace"},
        headers=ALICE_HEADERS,
    )
    assert res_ws.status_code == 201
    ws_id = res_ws.json()["workspace_id"]

    csv_data = b"year,revenue,sector\n2022,100,finance\n2023,150,finance\n"
    res_upload = client.post(
        f"/workspaces/{ws_id}/documents",
        files={"files": ("finance.csv", csv_data, "text/csv")},
        headers=ALICE_HEADERS,
    )
    assert res_upload.status_code == 201
    doc_id = res_upload.json()["items"][0]["id"]

    # 2. Cross-workspace denial: Bob attempts to query Alice's workspace -> 404 Not Found
    res_bob_query = client.post(
        f"/workspaces/{ws_id}/query",
        json={"query": "What is the earliest and latest year in finance.csv?"},
        headers=BOB_HEADERS,
    )
    assert res_bob_query.status_code == 404
    assert res_bob_query.json()["detail"] == "Resource not found"

    # Bob attempts to upload to Alice's workspace -> 404 Not Found
    res_bob_upload = client.post(
        f"/workspaces/{ws_id}/documents",
        files={"files": ("hacked.csv", csv_data, "text/csv")},
        headers=BOB_HEADERS,
    )
    assert res_bob_upload.status_code == 404
    assert res_bob_upload.json()["detail"] == "Resource not found"

    # 3. Deleted-source denial: Alice deletes (tombstones) the source
    res_del = client.delete(
        f"/workspaces/{ws_id}/sources/{doc_id}",
        headers=ALICE_HEADERS,
    )
    assert res_del.status_code == 200
    del_data = res_del.json()
    assert del_data["status"] == "tombstoned"
    assert del_data["manifest_generation"] >= 2

    # Subsequent aggregate query blocks with HTTP 422 because supporting source is tombstoned
    res_query_tombstoned = client.post(
        f"/workspaces/{ws_id}/query",
        json={"query": "What is the earliest and latest year in finance.csv?"},
        headers=ALICE_HEADERS,
    )
    assert res_query_tombstoned.status_code == 422
    err_detail = res_query_tombstoned.json()["detail"]
    assert err_detail["type"] == "urn:kre:error:insufficient_coverage"
    assert "tombstoned" in err_detail["detail"].lower()


def test_storage_failure_distinct_from_insufficient_evidence():
    """Verify insufficient evidence yields HTTP 200 refusal while storage failure returns HTTP 503."""
    # 1. Create workspace for Alice
    res_ws = client.post(
        "/workspaces",
        json={"name": "Alice Evidence Test Workspace"},
        headers=ALICE_HEADERS,
    )
    assert res_ws.status_code == 201
    ws_id = res_ws.json()["workspace_id"]

    # 2. Insufficient evidence: workspace has no documents matching the query
    res_no_data = client.post(
        f"/workspaces/{ws_id}/query",
        json={"query": "What is the earliest and latest year in missing_table.csv?"},
        headers=ALICE_HEADERS,
    )
    # Expected: HTTP 200 with polite refusal, not an error or 503
    assert res_no_data.status_code == 200
    no_data_json = res_no_data.json()
    assert no_data_json["status"] == "SUCCESS"
    assert len(no_data_json["citations"]) == 0
    assert "couldn't find any relevant documents" in no_data_json["answer"].lower()

    # 3. Storage failure: authoritative repository throws StorageUnavailableError
    with patch(
        "api.workspace_boundary_routes.evaluate_query_delivery_and_recomputation",
        side_effect=StorageUnavailableError("Authoritative storage connection failed"),
    ):
        res_fail = client.post(
            f"/workspaces/{ws_id}/query",
            json={"query": "What is the earliest and latest year in table.csv?"},
            headers=ALICE_HEADERS,
        )
        assert res_fail.status_code == 503
        assert "unreachable" in res_fail.json()["detail"].lower()
