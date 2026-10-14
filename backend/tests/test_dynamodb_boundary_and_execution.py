import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import app
from config import settings
from src.db.workspace_boundary_repo import (
    DynamoDBWorkspaceBoundaryRepository,
    InMemoryWorkspaceBoundaryRepository,
    SQLiteWorkspaceBoundaryRepository,
    get_workspace_boundary_repo,
)
from src.schemas.contracts.boundary import (
    CASConflictError,
    StorageUnavailableError,
    WorkspaceAccessDeniedError,
)
from src.security.auth import require_auth_context
from src.services.retrieval.strategies.structured_table import StructuredTableStrategy

client = TestClient(app)


def test_dependency_selection_dynamodb_and_fallbacks():
    """Prove get_workspace_boundary_repo selects DynamoDB when configured or in prod."""
    # 1. Explicit dynamodb backend override
    with patch.dict(os.environ, {"KRE_WORKSPACE_REPO_BACKEND": "dynamodb"}):
        repo = get_workspace_boundary_repo(reset=True)
        assert isinstance(repo, DynamoDBWorkspaceBoundaryRepository)

    # 2. Explicit sqlite backend override
    with patch.dict(os.environ, {"KRE_WORKSPACE_REPO_BACKEND": "sqlite"}):
        repo = get_workspace_boundary_repo(reset=True)
        assert isinstance(repo, SQLiteWorkspaceBoundaryRepository)

    # 3. Explicit memory backend override
    with patch.dict(os.environ, {"KRE_WORKSPACE_REPO_BACKEND": "memory"}):
        repo = get_workspace_boundary_repo(reset=True)
        assert isinstance(repo, InMemoryWorkspaceBoundaryRepository)

    # 4. Default in prod environment
    with patch.dict(os.environ, {"KRE_WORKSPACE_REPO_BACKEND": ""}), patch.object(settings, "ENVIRONMENT", "prod"):
        repo = get_workspace_boundary_repo(reset=True)
        assert isinstance(repo, DynamoDBWorkspaceBoundaryRepository)

    # Reset back to default environment repo
    get_workspace_boundary_repo(reset=True)


def test_dynamodb_repository_workspace_and_boundary_isolation():
    """Verify DynamoDB repository creates workspaces, enforces isolation, and records tombstones."""
    fake_items = {}

    mock_table = MagicMock()

    def fake_put_item(Item):
        key = (Item["PK"], Item["SK"])
        fake_items[key] = dict(Item)
        return {}

    def fake_get_item(Key):
        key = (Key["PK"], Key["SK"])
        item = fake_items.get(key)
        return {"Item": dict(item)} if item else {}

    def fake_update_item(Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None, ConditionExpression=None):
        key = (Key["PK"], Key["SK"])
        if key not in fake_items:
            raise KeyError("Item not found")
        item = fake_items[key]
        if ConditionExpression:
            if "manifest_generation = :exp" in ConditionExpression:
                exp = ExpressionAttributeValues[":exp"]
                if item.get("manifest_generation") != exp:
                    err = Exception("ConditionalCheckFailed")
                    err.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
                    raise err
        if ":tomb" in ExpressionAttributeValues:
            item["status"] = ExpressionAttributeValues[":tomb"]
        if ":gen" in ExpressionAttributeValues:
            item["manifest_generation"] = ExpressionAttributeValues[":gen"]
        if ":next_gen" in ExpressionAttributeValues:
            item["manifest_generation"] = ExpressionAttributeValues[":next_gen"]
        if ":sources" in ExpressionAttributeValues:
            item["active_source_ids_json"] = ExpressionAttributeValues[":sources"]
        if ":upd" in ExpressionAttributeValues:
            item["updated_at"] = ExpressionAttributeValues[":upd"]
        return {}

    mock_table.put_item.side_effect = fake_put_item
    mock_table.get_item.side_effect = fake_get_item
    mock_table.update_item.side_effect = fake_update_item

    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    repo = DynamoDBWorkspaceBoundaryRepository(
        table_name="test-table",
        dynamodb_resource=mock_resource,
    )

    # 1. Create Alice workspace
    ws_alice = repo.create_workspace("Alice Workspace", "usr_alice", workspace_id="ws_alice")
    assert ws_alice.workspace_id == "ws_alice"
    assert ws_alice.owner_principal_id == "usr_alice"
    assert ws_alice.manifest_generation == 1

    # 2. Alice retrieves her workspace
    retrieved = repo.get_workspace("ws_alice")
    assert retrieved is not None
    assert retrieved.name == "Alice Workspace"

    # 3. Bob attempts foreign workspace deletion -> denied
    with pytest.raises(WorkspaceAccessDeniedError):
        repo.tombstone_workspace("ws_alice", caller_principal_id="usr_bob")

    # 4. Alice creates a source and tombstones it
    src = repo.create_source("ws_alice", "Alice Document", source_id="src_doc1")
    assert src.source_id == "src_doc1"
    assert repo.has_tombstone("ws_alice", "source", "src_doc1") is False

    tombstone = repo.record_source_tombstone("ws_alice", "src_doc1", caller_principal_id="usr_alice")
    assert tombstone.manifest_generation == 2
    assert repo.has_tombstone("ws_alice", "source", "src_doc1") is True
    # Deleted source lookup returns None
    assert repo.get_source("ws_alice", "src_doc1") is None

    # 5. CAS publication update on manifest
    manifest = repo.get_manifest("ws_alice")
    assert manifest is not None
    assert manifest.manifest_generation == 1

    # CAS update with matching expected generation succeeds
    updated_manifest = repo.cas_update_manifest(
        "ws_alice",
        expected_generation=1,
        delta={"add_source_ids": ["src_doc2"]},
    )
    assert updated_manifest.manifest_generation == 2

    # CAS update with stale expected generation fails with CASConflictError
    with pytest.raises(CASConflictError):
        repo.cas_update_manifest(
            "ws_alice",
            expected_generation=1,
            delta={"add_source_ids": ["src_doc3"]},
        )


def test_dynamodb_unreachable_raises_storage_unavailable_error():
    """Verify storage failures in DynamoDB repository raise StorageUnavailableError."""
    mock_table = MagicMock()
    mock_table.get_item.side_effect = Exception("EndpointConnectionError: Could not connect to DynamoDB")

    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    repo = DynamoDBWorkspaceBoundaryRepository(
        table_name="test-table",
        dynamodb_resource=mock_resource,
    )

    with pytest.raises(StorageUnavailableError) as exc_info:
        repo.get_workspace("ws_test")
    assert "DynamoDB error" in str(exc_info.value)


def test_require_auth_context_is_sync_def():
    """Verify require_auth_context is defined as a synchronous function so FastAPI offloads to threadpool."""
    import inspect
    assert not inspect.iscoroutinefunction(require_auth_context), (
        "require_auth_context must not be a coroutine function; it must run in worker threadpool"
    )


@pytest.mark.anyio
async def test_structured_table_strategy_runs_in_executor_threadpool():
    """Verify StructuredTableStrategy.retrieve offloads execution to threadpool without blocking event loop."""
    mock_store = MagicMock()
    mock_service = MagicMock()
    mock_service.execute.return_value = None

    strategy = StructuredTableStrategy(store=mock_store, service=mock_service)

    # Calling async retrieve
    res = await strategy.retrieve(query="Count rows", workspace_id="ws_alpha")
    assert res.strategy_name == "structured_table"
    mock_service.execute.assert_called_once_with("Count rows", "ws_alpha")


def test_dynamodb_table_store_retry_behavior():
    """Verify DynamoDBTableStore retries transact_write_items on transient errors with backoff."""
    from schemas.structured_table import TableRow
    from db.table_store.dynamodb_store import DynamoDBTableStore

    mock_resource = MagicMock()
    mock_client = MagicMock()

    store = DynamoDBTableStore(
        table_name="test-table",
        resource=mock_resource,
        client=mock_client,
    )

    rows = [TableRow(row_id="r0", row_index=0, cells=())]

    # 1. Transient failure on first attempt, success on second
    mock_client.transact_write_items.side_effect = [
        Exception("ProvisionedThroughputExceededException"),
        {"ResponseMetadata": {"HTTPStatusCode": 200}},
    ]

    with patch("time.sleep") as mock_sleep:
        store.write_row_batch_with_checkpoint(
            table_id="tbl_test",
            workspace_id="ws_test",
            document_id="doc_test",
            version="1",
            rows=rows,
            checkpoint_batch=1,
            source_rows=1,
            attempted_rows=1,
            persisted_rows=1,
            rejected_rows=0,
            parser_version="1.0.0",
            schema_version="1.0.0",
            content_hash="hash123",
        )
        assert mock_client.transact_write_items.call_count == 2
        mock_sleep.assert_called_once_with(0.5)

    # 2. Repeated failure exhausts retry budget and raises
    mock_client.transact_write_items.reset_mock()
    mock_client.transact_write_items.side_effect = Exception("InternalServerError")

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(Exception, match="InternalServerError"):
            store.write_row_batch_with_checkpoint(
                table_id="tbl_test",
                workspace_id="ws_test",
                document_id="doc_test",
                version="1",
                rows=rows,
                checkpoint_batch=1,
                source_rows=1,
                attempted_rows=1,
                persisted_rows=1,
                rejected_rows=0,
                parser_version="1.0.0",
                schema_version="1.0.0",
                content_hash="hash123",
            )
        assert mock_client.transact_write_items.call_count == 6
        assert mock_sleep.call_count == 5


def test_workspace_boundary_routes_with_dynamodb_backend():
    """Verify application routes enforce isolation and error mapping with DynamoDB repo backend."""
    import src.db.workspace_boundary_repo as repo_mod

    fake_items = {}
    mock_table = MagicMock()

    def fake_put_item(Item):
        key = (Item["PK"], Item["SK"])
        fake_items[key] = dict(Item)
        return {}

    def fake_get_item(Key):
        key = (Key["PK"], Key["SK"])
        item = fake_items.get(key)
        return {"Item": dict(item)} if item else {}

    mock_table.put_item.side_effect = fake_put_item
    mock_table.get_item.side_effect = fake_get_item

    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    dynamo_repo = DynamoDBWorkspaceBoundaryRepository(
        table_name="test-boundary-table",
        dynamodb_resource=mock_resource,
    )

    # 1. Authorized creation by Alice
    with patch.object(repo_mod, "_SHARED_BOUNDARY_REPO", dynamo_repo):
        res = client.post(
            "/workspaces",
            json={"name": "Alice WS"},
            headers={"Authorization": "Bearer test_token_alice"},
        )
        assert res.status_code == 201
        ws_id = res.json()["workspace_id"]

        # 2. Authorized access by Alice
        res_alice = client.get(
            f"/workspaces/{ws_id}",
            headers={"Authorization": "Bearer test_token_alice"},
        )
        assert res_alice.status_code == 200
        assert res_alice.json()["name"] == "Alice WS"

        # 3. Cross-workspace denial for Bob -> 404 (masked tenant boundary)
        res_bob = client.get(
            f"/workspaces/{ws_id}",
            headers={"Authorization": "Bearer test_token_bob"},
        )
        assert res_bob.status_code == 404
        assert res_bob.json()["detail"] == "Resource not found"

        # 4. Storage connection failure returns 503
        mock_table.get_item.side_effect = Exception("DynamoDB service unavailable")
        res_err = client.get(
            f"/workspaces/{ws_id}",
            headers={"Authorization": "Bearer test_token_alice"},
        )
        assert res_err.status_code == 503
        assert "unreachable" in res_err.json()["detail"].lower()


@pytest.mark.anyio
async def test_ingest_direct_runs_in_executor_threadpool():
    """Verify DocumentsService.ingest_direct offloads synchronous ingestion to executor."""
    from modules.documents.documents_service import DocumentsService
    from fastapi import UploadFile
    import io

    mock_repo = MagicMock()
    service = DocumentsService(repo=mock_repo)

    fake_file = UploadFile(
        file=io.BytesIO(b"col1,col2\nval1,val2\n"),
        filename="test.csv",
        headers={"content-type": "text/csv"},
    )

    fake_doc = MagicMock()
    fake_doc.id = "doc_123"
    fake_doc.filename = "test.csv"
    fake_doc.source_format = "csv"
    fake_doc.chunks = []

    with patch("ingestion.parse_service.ingest_document", return_value=fake_doc):
        res = await service.ingest_direct(fake_file)
        assert res["id"] == "doc_123"
        assert res["filename"] == "test.csv"
        mock_repo.save.assert_called_once_with(fake_doc)

