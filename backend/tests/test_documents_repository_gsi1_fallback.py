"""Offline tests for DocumentsRepository.get_all_chunks missing-GSI1 scoped DOC# fallback."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = BACKEND_ROOT / "src"
for p in (str(BACKEND_ROOT), str(SRC_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
from modules.documents.documents_repository import DocumentsRepository
from services.retrieval import bm25_retriever
from src.schemas.contracts.boundary import DocumentSnapshot


def _client_error(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Query")


def _chunk_item(doc_id: str, cid: str, ws_id: str, text: str, doc_ver: str | None = None) -> dict:
    item = {
        "PK": f"DOC#{doc_id}",
        "SK": f"CHUNK#{cid}",
        "GSI1PK": f"WORKSPACE#{ws_id}",
        "GSI1SK": f"CHUNK#{cid}",
        "id": cid,
        "document_id": doc_id,
        "workspace_id": ws_id,
        "source_format": "pdf",
        "text": text,
        "element_type": "text",
        "page_number": 1,
        "section_path": "[]",
        "metadata": json_dumps({"document_version": doc_ver} if doc_ver else {}),
    }
    return item


def json_dumps(obj: dict) -> str:
    import json

    return json.dumps(obj)


def _make_repo(table: MagicMock) -> DocumentsRepository:
    repo = DocumentsRepository.__new__(DocumentsRepository)
    repo.table = table
    repo.last_query_telemetry = {}
    return repo


def test_missing_gsi1_uses_scoped_doc_queries_zero_scans_and_enforces_isolation():
    """Missing GSI1 must query authorized DOC# partitions with pagination and zero table.scan calls."""
    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()

    ws_id = "ws_target"
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Target", "owner-1", workspace_id=ws_id)
    for did, src, ver in (
        ("doc_legacy", "legacy.xls", "v_leg"),
        ("doc_recent", "recent.pdf", "v_rec"),
        ("doc_revoked", "revoked.pdf", "v_rev"),
    ):
        b_repo.create_source(ws_id, src, source_id=did)
        b_repo.save_document_snapshot(
            DocumentSnapshot(
                workspace_id=ws_id,
                document_id=did,
                source=src,
                version=ver,
                format="pdf",
                created_at="2026-11-26T00:00:00Z",
            )
        )
    b_repo.record_source_tombstone(ws_id, "doc_revoked", "owner-1")

    table = MagicMock()
    table.scan.side_effect = AssertionError("table.scan must never be called on missing-GSI1 fallback")

    def _query(**kwargs):
        if kwargs.get("IndexName") == "GSI1":
            raise _client_error("ValidationException", "The table does not have the specified index: GSI1")
        kce = kwargs["KeyConditionExpression"]
        expr_vals = getattr(kce, "_values", None)
        # Extract PK from Key('PK').eq(...) & Key('SK').begins_with(...)
        pk_val = None
        for child in getattr(kce, "_values", ()):
            vals = getattr(child, "_values", ())
            if len(vals) == 2 and str(vals[1]).startswith(("WORKSPACE#", "DOC#")):
                pk_val = str(vals[1])
                break
        esk = kwargs.get("ExclusiveStartKey")
        if pk_val == f"WORKSPACE#{ws_id}":
            if esk is None:
                return {
                    "Items": [
                        {"PK": pk_val, "SK": "DOC#doc_legacy", "id": "doc_legacy"},
                        {"PK": pk_val, "SK": "DOC#doc_recent", "id": "doc_recent"},
                    ],
                    "LastEvaluatedKey": {"PK": pk_val, "SK": "DOC#doc_recent"},
                }
            return {
                "Items": [
                    {"PK": pk_val, "SK": "DOC#doc_revoked", "id": "doc_revoked"},
                ]
            }
        if pk_val == "DOC#doc_legacy":
            if esk is None:
                return {
                    "Items": [_chunk_item("doc_legacy", "c_leg_1", ws_id, "legacy row 1")],
                    "LastEvaluatedKey": {"PK": pk_val, "SK": "CHUNK#c_leg_1"},
                }
            return {"Items": [_chunk_item("doc_legacy", "c_leg_2", ws_id, "legacy row 2")]}
        if pk_val == "DOC#doc_recent":
            return {
                "Items": [
                    _chunk_item("doc_recent", "c_rec_1", ws_id, "recent page 1", doc_ver="v_rec"),
                    _chunk_item("doc_recent", "c_rec_stale", ws_id, "stale page", doc_ver="v_old"),
                    _chunk_item("doc_recent", "c_foreign", "ws_other", "foreign leak", doc_ver="v_rec"),
                ]
            }
        return {"Items": []}

    table.query.side_effect = _query
    repo = _make_repo(table)

    with (
        patch("modules.documents.documents_repository._is_test_env", return_value=False),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        chunks = repo.get_all_chunks(workspace_id=ws_id)

    assert [c.id for c in chunks] == ["c_leg_1", "c_leg_2", "c_rec_1"]
    assert repo.last_query_telemetry["dynamodb_scan_pages"] == 0
    assert repo.last_query_telemetry["dynamodb_query_pages"] > 0

    # Explicit document scope filters cleanly
    with (
        patch("modules.documents.documents_repository._is_test_env", return_value=False),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        scoped = repo.get_all_chunks(document_ids=["doc_recent", "doc_other_out_of_scope"], workspace_id=ws_id)
    assert [c.id for c in scoped] == ["c_rec_1"]


def test_healthy_gsi1_path_and_scoped_cache_isolation():
    """Healthy GSI1 path succeeds without fallback; document-scoped miss does not poison workspace cache."""
    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()

    ws_id = "ws_healthy"
    table = MagicMock()
    table.scan.side_effect = AssertionError("scan must not be called")

    def _query_healthy(**kwargs):
        if kwargs.get("IndexName") == "GSI1":
            return {"Items": [_chunk_item("d1", "c1", ws_id, "healthy chunk")]}
        return {"Items": []}

    table.query.side_effect = _query_healthy
    repo = _make_repo(table)
    with patch("modules.documents.documents_repository._is_test_env", return_value=False):
        chunks = repo.get_all_chunks(workspace_id=ws_id)
    assert [c.id for c in chunks] == ["c1"]
    assert repo.last_query_telemetry["dynamodb_query_pages"] == 1
    assert repo.last_query_telemetry["dynamodb_scan_pages"] == 0

    # Document-scoped call with missing GSI1 on uncached workspace must NOT populate _CHUNK_CACHE
    ws_scoped = "ws_scoped_nocache"
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Scoped", "owner-1", workspace_id=ws_scoped)

    def _query_scoped(**kwargs):
        kce = kwargs["KeyConditionExpression"]
        pk_val = ""
        for child in getattr(kce, "_values", ()):
            vals = getattr(child, "_values", ())
            if len(vals) == 2 and str(vals[1]).startswith(("WORKSPACE#", "DOC#")):
                pk_val = str(vals[1])
                break
        if pk_val == f"WORKSPACE#{ws_scoped}":
            return {"Items": [{"PK": pk_val, "SK": "DOC#d1", "id": "d1"}, {"PK": pk_val, "SK": "DOC#d2", "id": "d2"}]}
        if pk_val == "DOC#d1":
            return {"Items": [_chunk_item("d1", "c_d1", ws_scoped, "d1 text")]}
        return {"Items": []}

    table.query.side_effect = _query_scoped
    with (
        patch("modules.documents.documents_repository._is_test_env", return_value=False),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        res_d1 = repo.get_all_chunks(document_ids=["d1"], workspace_id=ws_scoped)
    assert [c.id for c in res_d1] == ["c_d1"]
    assert ws_scoped not in bm25_retriever._CHUNK_CACHE


def test_unrelated_error_incomplete_enumeration_and_later_page_failure_fail_closed():
    """Unrelated errors, incomplete doc enumeration, and later-page chunk failures must raise without caching."""
    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()

    # 1. Unrelated DynamoDB error on GSI1 raises immediately
    ws_err = "ws_unrelated_err"
    t1 = MagicMock()
    t1.query.side_effect = _client_error("ProvisionedThroughputExceededException", "Throttled")
    r1 = _make_repo(t1)
    with patch("modules.documents.documents_repository._is_test_env", return_value=False):
        with pytest.raises(ClientError):
            r1.get_all_chunks(workspace_id=ws_err)
    assert ws_err not in bm25_retriever._CHUNK_CACHE

    # 2. Incomplete document enumeration (page 2 fails) raises without caching
    ws_inc = "ws_inc_enum"
    t2 = MagicMock()

    def _q_inc(**kwargs):
        if kwargs.get("IndexName") == "GSI1":
            raise _client_error("ValidationException", "The table does not have the specified index: GSI1")
        if kwargs.get("ExclusiveStartKey"):
            raise _client_error("InternalServerError", "Page 2 failed")
        return {"Items": [{"PK": f"WORKSPACE#{ws_inc}", "SK": "DOC#d1", "id": "d1"}], "LastEvaluatedKey": {"k": 1}}

    t2.query.side_effect = _q_inc
    r2 = _make_repo(t2)
    with patch("modules.documents.documents_repository._is_test_env", return_value=False):
        with pytest.raises(ClientError):
            r2.get_all_chunks(workspace_id=ws_inc)
    assert ws_inc not in bm25_retriever._CHUNK_CACHE

    # 3. Later-page failure on DOC# chunk query raises without caching
    ws_page2 = "ws_doc_page2_fail"
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("P2", "owner-1", workspace_id=ws_page2)
    t3 = MagicMock()

    def _q_p2(**kwargs):
        if kwargs.get("IndexName") == "GSI1":
            raise _client_error("ValidationException", "The table does not have the specified index: GSI1")
        kce = kwargs["KeyConditionExpression"]
        pk_val = ""
        for child in getattr(kce, "_values", ()):
            vals = getattr(child, "_values", ())
            if len(vals) == 2 and str(vals[1]).startswith(("WORKSPACE#", "DOC#")):
                pk_val = str(vals[1])
                break
        if pk_val == f"WORKSPACE#{ws_page2}":
            return {"Items": [{"PK": pk_val, "SK": "DOC#d1", "id": "d1"}]}
        if pk_val == "DOC#d1":
            if kwargs.get("ExclusiveStartKey"):
                raise _client_error("InternalServerError", "Chunk page 2 failed")
            return {"Items": [_chunk_item("d1", "c1", ws_page2, "p1")], "LastEvaluatedKey": {"k": 2}}
        return {"Items": []}

    t3.query.side_effect = _q_p2
    r3 = _make_repo(t3)
    with (
        patch("modules.documents.documents_repository._is_test_env", return_value=False),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        with pytest.raises(ClientError):
            r3.get_all_chunks(workspace_id=ws_page2)
    assert ws_page2 not in bm25_retriever._CHUNK_CACHE
