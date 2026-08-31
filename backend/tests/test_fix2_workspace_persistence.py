from unittest.mock import MagicMock
import pytest
import db.database as db_mod
from db.database import CloudRepository, DataIntegrityError
from schemas.models import Chunk, Document


def test_workspace_registry_persists_across_repository_instances(monkeypatch):
    """Regression Test 1: Workspace creation and retrieval persists via DynamoDB
    across fresh CloudRepository instances, even when in-memory dicts are cleared.
    """
    # Force non-test mode so it routes to DynamoDB
    monkeypatch.setattr("config.settings.ENVIRONMENT", "dev")
    monkeypatch.setenv("ENVIRONMENT", "dev")

    # Shared DynamoDB table storage mock
    dynamo_items = {}

    mock_table = MagicMock()

    def fake_put_item(Item):
        pk = Item["PK"]
        sk = Item["SK"]
        dynamo_items[(pk, sk)] = Item

    def fake_scan(**kwargs):
        # Return all items matching SK=META
        items = [
            item for (pk, sk), item in dynamo_items.items() if sk == "META" and pk.startswith("WORKSPACE#")
        ]
        return {"Items": items}

    mock_table.put_item.side_effect = fake_put_item
    mock_table.scan.side_effect = fake_scan

    # Instance 1: Create workspace
    repo1 = CloudRepository()
    repo1.table = mock_table
    created_ws = repo1.create_workspace(name="Finance Ops", industry="Finance", workspace_id="ws_fin_101")
    assert created_ws["id"] == "ws_fin_101"

    # Wipe in-memory dicts completely
    db_mod._WORKSPACES.clear()
    db_mod._WORKSPACE_DOCS.clear()
    assert len(db_mod._WORKSPACES) == 0

    # Instance 2: Retrieve workspaces from fresh repo instance
    repo2 = CloudRepository()
    repo2.table = mock_table
    workspaces = repo2.get_workspaces()

    ws_ids = [w["id"] for w in workspaces]
    assert "ws_fin_101" in ws_ids, f"Expected ws_fin_101 to persist across repo instances via DynamoDB, got {ws_ids}"


def test_document_missing_workspace_id_rejected():
    """Regression Test 2: CloudRepository.save() must raise DataIntegrityError if
    Document or any Chunk is missing workspace_id.
    """
    repo = CloudRepository()

    valid_fast_emb = [0.1] * 384
    valid_full_emb = [0.1] * 1024

    # 1. Document missing workspace_id (default empty string)
    chunk_with_ws = Chunk(
        id="c1",
        document_id="doc1",
        source_format="pdf",
        text="Sample text",
        element_type="paragraph",
        embedding_fast=valid_fast_emb,
        embedding_full=valid_full_emb,
        workspace_id="ws_001",
    )
    doc_missing_ws = Document(
        id="doc1",
        filename="doc1.pdf",
        source_format="pdf",
        chunks=(chunk_with_ws,),
        workspace_id="",  # empty / missing
    )

    with pytest.raises(DataIntegrityError, match="[Dd]ocument.*workspace_id"):
        repo.save(doc_missing_ws)

    # 2. Chunk missing workspace_id (default empty string)
    chunk_missing_ws = Chunk(
        id="c2",
        document_id="doc2",
        source_format="pdf",
        text="Sample text",
        element_type="paragraph",
        embedding_fast=valid_fast_emb,
        embedding_full=valid_full_emb,
        workspace_id="",  # empty / missing
    )
    doc_valid_ws = Document(
        id="doc2",
        filename="doc2.pdf",
        source_format="pdf",
        chunks=(chunk_missing_ws,),
        workspace_id="ws_001",
    )

    with pytest.raises(DataIntegrityError, match="[Cc]hunk.*workspace_id"):
        repo.save(doc_valid_ws)


def test_chunk_workspace_id_written_to_dynamo_and_qdrant(monkeypatch):
    """Regression Test 3: Ingesting a chunk with workspace_id writes workspace_id
    and GSI1 attributes to DynamoDB, and workspace_id to Qdrant payload.
    """
    monkeypatch.setattr("config.settings.ENVIRONMENT", "dev")
    monkeypatch.setenv("ENVIRONMENT", "dev")

    repo = CloudRepository()

    saved_dynamo_items = []
    mock_batch = MagicMock()
    mock_batch.put_item.side_effect = lambda Item: saved_dynamo_items.append(Item)

    mock_table = MagicMock()
    mock_table.batch_writer.return_value.__enter__.return_value = mock_batch
    repo.table = mock_table

    saved_qdrant_points = []
    mock_qclient = MagicMock()

    def fake_upsert(collection_name, points, wait=True):
        saved_qdrant_points.extend(points)

    mock_qclient.upsert.side_effect = fake_upsert
    repo.qclient = mock_qclient

    chunk = Chunk(
        id="chunk_test_99",
        document_id="doc_test_99",
        source_format="pdf",
        text="Workspace scoped text chunk",
        element_type="paragraph",
        page_number=3,
        embedding_fast=[0.2] * 384,
        embedding_full=[0.2] * 1024,
        workspace_id="ws_test_99",
    )
    doc = Document(
        id="doc_test_99",
        filename="test99.pdf",
        source_format="pdf",
        chunks=(chunk,),
        workspace_id="ws_test_99",
    )

    repo.save(doc)

    # 1. Verify DynamoDB item writes
    assert len(saved_dynamo_items) == 2, f"Expected 2 DynamoDB items (1 doc, 1 chunk), got {len(saved_dynamo_items)}"

    doc_item = next(it for it in saved_dynamo_items if it["SK"].startswith("DOC#"))
    chunk_item = next(it for it in saved_dynamo_items if it["SK"].startswith("CHUNK#"))

    assert doc_item["workspace_id"] == "ws_test_99"
    assert chunk_item["workspace_id"] == "ws_test_99"
    assert chunk_item["GSI1PK"] == "WORKSPACE#ws_test_99"
    assert chunk_item["GSI1SK"] == "CHUNK#chunk_test_99"

    # 2. Verify Qdrant payload write
    assert len(saved_qdrant_points) == 1, f"Expected 1 Qdrant point, got {len(saved_qdrant_points)}"
    point = saved_qdrant_points[0]
    assert point.payload.get("workspace_id") == "ws_test_99"
    assert point.payload.get("document_id") == "doc_test_99"
    assert point.payload.get("original_id") == "chunk_test_99"
