"""Offline regression tests for canonical Qdrant resume preflight and verification.

Verifies:
1. verify_source_version fails closed on missing snapshot, repo resolution failure, and hash mismatch.
2. verify_dynamodb_chunk_records fails closed on read errors, missing chunk IDs, and provenance mismatches.
3. inspect_existing_qdrant_points validates point identity, payload, and vector dimensions, treating unverified model provenance as unresolved.
4. retrieve_landed_point_ids correctly handles timeout read-back by validating landed records.
5. verify_qdrant_post_write fails on missing, unexpected, or invalid vector records.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch
import uuid

import pytest

# Ensure backend/src is on sys.path
import sys
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from resume_canonical_qdrant_points import (
    PointValidationResult,
    inspect_existing_qdrant_points,
    retrieve_landed_point_ids,
    validate_batch_before_upsert,
    verify_dynamodb_chunk_records,
    verify_provider_availability,
    verify_qdrant_post_write,
    verify_source_version,
)


class DummyChunk:
    def __init__(self, chunk_id: str, document_id: str, workspace_id: str, text: str = "sample text"):
        self.id = chunk_id
        self.document_id = document_id
        self.workspace_id = workspace_id
        self.text = text
        self.page_number = None


class DummySnapshot:
    def __init__(self, version: str):
        self.version = version


# ---------------------------------------------------------------------------
# 1. verify_source_version tests
# ---------------------------------------------------------------------------

def test_verify_source_version_success(tmp_path: Path):
    sample_csv = tmp_path / "test.csv"
    content = b"col1,col2\nval1,val2\n"
    sample_csv.write_bytes(content)
    expected_version = hashlib.sha256(content).hexdigest()[:32]

    mock_repo = MagicMock()
    mock_repo.get_document_snapshot.return_value = DummySnapshot(expected_version)

    res = verify_source_version(
        csv_path=sample_csv,
        workspace_id="ws_test",
        document_id="doc_test",
        boundary_repo=mock_repo,
    )
    assert res == expected_version
    mock_repo.get_document_snapshot.assert_called_once_with(
        workspace_id="ws_test", document_id="doc_test"
    )


def test_verify_source_version_missing_snapshot_fails_closed(tmp_path: Path):
    sample_csv = tmp_path / "test.csv"
    sample_csv.write_bytes(b"col1,col2\nval1,val2\n")

    mock_repo = MagicMock()
    mock_repo.get_document_snapshot.return_value = None

    with pytest.raises(RuntimeError, match="missing DocumentSnapshot"):
        verify_source_version(
            csv_path=sample_csv,
            workspace_id="ws_test",
            document_id="doc_test",
            boundary_repo=mock_repo,
        )


def test_verify_source_version_hash_mismatch_fails_closed(tmp_path: Path):
    sample_csv = tmp_path / "test.csv"
    sample_csv.write_bytes(b"col1,col2\nval1,val2\n")

    mock_repo = MagicMock()
    mock_repo.get_document_snapshot.return_value = DummySnapshot("00000000000000000000000000000000")

    with pytest.raises(ValueError, match="Source version mismatch"):
        verify_source_version(
            csv_path=sample_csv,
            workspace_id="ws_test",
            document_id="doc_test",
            boundary_repo=mock_repo,
        )


def test_verify_source_version_repo_resolution_failure(tmp_path: Path):
    sample_csv = tmp_path / "test.csv"
    sample_csv.write_bytes(b"data")

    with patch("db.workspace_boundary_repo.get_workspace_boundary_repo", side_effect=RuntimeError("db connection failed")):
        with pytest.raises(RuntimeError, match="Cannot resolve configured workspace boundary repository"):
            verify_source_version(
                csv_path=sample_csv,
                workspace_id="ws_test",
                document_id="doc_test",
                boundary_repo=None,
            )


# ---------------------------------------------------------------------------
# 2. verify_dynamodb_chunk_records tests
# ---------------------------------------------------------------------------

def test_verify_dynamodb_chunk_records_success():
    expected_ids = {"doc1:csv:row1", "doc1:csv:row2"}
    mock_dynamo = MagicMock()
    mock_table = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    mock_table.query.return_value = {
        "Items": [
            {"id": "doc1:csv:row1", "workspace_id": "ws_1", "document_id": "doc1", "source_format": "csv"},
            {"id": "doc1:csv:row2", "workspace_id": "ws_1", "document_id": "doc1", "source_format": "csv"},
        ]
    }

    assert verify_dynamodb_chunk_records(
        workspace_id="ws_1",
        document_id="doc1",
        expected_chunk_ids=expected_ids,
        expected_source_format="csv",
        dynamodb_resource=mock_dynamo,
    ) is True


def test_verify_dynamodb_chunk_records_read_error_fails_closed():
    mock_dynamo = MagicMock()
    mock_table = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    mock_table.query.side_effect = Exception("DynamoDB service unavailable")

    with pytest.raises(RuntimeError, match="Preflight DynamoDB chunk verification failed"):
        verify_dynamodb_chunk_records(
            workspace_id="ws_1",
            document_id="doc1",
            expected_chunk_ids={"doc1:csv:row1"},
            expected_source_format="csv",
            dynamodb_resource=mock_dynamo,
        )


def test_verify_dynamodb_chunk_records_missing_chunks_fails_closed():
    expected_ids = {"doc1:csv:row1", "doc1:csv:row2"}
    mock_dynamo = MagicMock()
    mock_table = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    mock_table.query.return_value = {
        "Items": [
            {"id": "doc1:csv:row1", "workspace_id": "ws_1", "document_id": "doc1", "source_format": "csv"},
        ]
    }

    with pytest.raises(RuntimeError, match="1 chunk records missing"):
        verify_dynamodb_chunk_records(
            workspace_id="ws_1",
            document_id="doc1",
            expected_chunk_ids=expected_ids,
            expected_source_format="csv",
            dynamodb_resource=mock_dynamo,
        )


def test_verify_dynamodb_chunk_records_provenance_mismatch_fails_closed():
    expected_ids = {"doc1:csv:row1"}
    mock_dynamo = MagicMock()
    mock_table = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    mock_table.query.return_value = {
        "Items": [
            {"id": "doc1:csv:row1", "workspace_id": "ws_wrong", "document_id": "doc1", "source_format": "csv"},
        ]
    }

    with pytest.raises(RuntimeError, match="workspace_id expected ws_1, found ws_wrong"):
        verify_dynamodb_chunk_records(
            workspace_id="ws_1",
            document_id="doc1",
            expected_chunk_ids=expected_ids,
            expected_source_format="csv",
            dynamodb_resource=mock_dynamo,
        )


# ---------------------------------------------------------------------------
# 3. inspect_existing_qdrant_points tests
# ---------------------------------------------------------------------------

def test_inspect_existing_qdrant_points_unresolved_provenance():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    point_id = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {point_id: chunk}

    mock_record = MagicMock()
    mock_record.id = point_id
    mock_record.payload = {
        "original_id": str(chunk.id),
        "document_id": "doc1",
        "workspace_id": "ws_1",
        "page_number": None,
    }
    mock_record.vector = {
        "embedding_fast": [0.1] * 384,
        "embedding_full": [0.2] * 1024,
    }

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([mock_record], None)

    results, unexpected = inspect_existing_qdrant_points(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )

    assert not unexpected
    assert point_id in results
    res = results[point_id]
    assert res.is_present is True
    assert res.identity_valid is True
    assert res.payload_valid is True
    assert res.vector_dims_valid is True
    assert res.model_provenance_verified is False
    assert res.status == "valid_unresolved_provenance"


def test_inspect_existing_qdrant_points_verified_provenance():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    point_id = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {point_id: chunk}

    mock_record = MagicMock()
    mock_record.id = point_id
    mock_record.payload = {
        "original_id": str(chunk.id),
        "document_id": "doc1",
        "workspace_id": "ws_1",
        "fast_model": "BAAI/bge-small-en-v1.5",
        "full_model": "amazon.titan-embed-text-v2:0",
    }
    mock_record.vector = {
        "embedding_fast": [0.1] * 384,
        "embedding_full": [0.2] * 1024,
    }

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([mock_record], None)

    results, unexpected = inspect_existing_qdrant_points(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )

    assert point_id in results
    res = results[point_id]
    assert res.model_provenance_verified is True
    assert res.status == "valid_verified"


def test_inspect_existing_qdrant_points_corrupted_payload():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    point_id = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {point_id: chunk}

    mock_record = MagicMock()
    mock_record.id = point_id
    mock_record.payload = {
        "original_id": "wrong_chunk_id",
        "document_id": "doc1",
        "workspace_id": "ws_1",
    }
    mock_record.vector = {
        "embedding_fast": [0.1] * 384,
        "embedding_full": [0.2] * 1024,
    }

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([mock_record], None)

    results, _ = inspect_existing_qdrant_points(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )

    assert results[point_id].status == "corrupted"
    assert "Payload mismatch" in (results[point_id].reason or "")


def test_inspect_existing_qdrant_points_corrupted_vectors():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    point_id = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {point_id: chunk}

    mock_record = MagicMock()
    mock_record.id = point_id
    mock_record.payload = {
        "original_id": str(chunk.id),
        "document_id": "doc1",
        "workspace_id": "ws_1",
    }
    mock_record.vector = {
        "embedding_fast": [0.1] * 128,  # Invalid dimension
        "embedding_full": [0.2] * 1024,
    }

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([mock_record], None)

    results, _ = inspect_existing_qdrant_points(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )

    assert results[point_id].status == "corrupted"
    assert "Vector dimensions mismatch" in (results[point_id].reason or "")


def test_inspect_existing_qdrant_points_missing():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    point_id = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {point_id: chunk}

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([], None)

    results, unexpected = inspect_existing_qdrant_points(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )

    assert point_id in results
    assert results[point_id].is_present is False
    assert results[point_id].status == "missing"


# ---------------------------------------------------------------------------
# 4. Timeout read-back & Post-write verification tests
# ---------------------------------------------------------------------------

def test_retrieve_landed_point_ids_timeout_readback():
    chunk1 = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    chunk2 = DummyChunk("doc1:csv:row2", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk1.id)))
    p2 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk2.id)))
    expected_map = {p1: chunk1, p2: chunk2}

    rec1 = MagicMock()
    rec1.id = p1
    rec1.payload = {"original_id": str(chunk1.id), "document_id": "doc1", "workspace_id": "ws_1"}
    rec1.vector = {"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024}

    # rec2 is not returned (did not land)
    mock_client = MagicMock()
    mock_client.retrieve.return_value = [rec1]

    landed = retrieve_landed_point_ids(
        client=mock_client,
        collection_name="kre_chunks",
        point_ids=[p1, p2],
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )

    assert landed == {p1}


def test_verify_qdrant_post_write_success():
    chunk1 = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk1.id)))
    expected_map = {p1: chunk1}

    rec1 = MagicMock()
    rec1.id = p1
    rec1.payload = {"original_id": str(chunk1.id), "document_id": "doc1", "workspace_id": "ws_1"}
    rec1.vector = {"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024}

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([rec1], None)

    ok, msg = verify_qdrant_post_write(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
        wait=True,
    )
    assert ok is True
    assert "all 1 points verified" in msg


def test_verify_qdrant_post_write_missing_point_fails():
    chunk1 = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk1.id)))
    expected_map = {p1: chunk1}

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([], None)

    ok, msg = verify_qdrant_post_write(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
        wait=True,
    )
    assert ok is False
    assert "1 points remain missing" in msg


def test_verify_dynamodb_chunk_records_missing_id_fails_closed():
    expected_ids = {"doc1:csv:row1"}
    mock_dynamo = MagicMock()
    mock_table = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    mock_table.query.return_value = {
        "Items": [
            {"workspace_id": "ws_1", "document_id": "doc1", "source_format": "csv"},
        ]
    }

    with pytest.raises(RuntimeError, match="missing required 'id' attribute"):
        verify_dynamodb_chunk_records(
            workspace_id="ws_1",
            document_id="doc1",
            expected_chunk_ids=expected_ids,
            expected_source_format="csv",
            dynamodb_resource=mock_dynamo,
        )


def test_verify_dynamodb_chunk_records_unexpected_chunks_fails_closed():
    expected_ids = {"doc1:csv:row1"}
    mock_dynamo = MagicMock()
    mock_table = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    mock_table.query.return_value = {
        "Items": [
            {"id": "doc1:csv:row1", "workspace_id": "ws_1", "document_id": "doc1", "source_format": "csv"},
            {"id": "doc1:csv:row_unexpected", "workspace_id": "ws_1", "document_id": "doc1", "source_format": "csv"},
        ]
    }

    with pytest.raises(RuntimeError, match="1 unexpected chunk records"):
        verify_dynamodb_chunk_records(
            workspace_id="ws_1",
            document_id="doc1",
            expected_chunk_ids=expected_ids,
            expected_source_format="csv",
            dynamodb_resource=mock_dynamo,
        )


def test_inspect_existing_qdrant_points_unexpected_point_collected():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {p1: chunk}

    rec_unexpected = MagicMock()
    rec_unexpected.id = "99999999-9999-9999-9999-999999999999"

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([rec_unexpected], None)

    results, unexpected = inspect_existing_qdrant_points(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )

    assert "99999999-9999-9999-9999-999999999999" in unexpected
    assert p1 in results
    assert results[p1].status == "missing"


def test_verify_qdrant_post_write_unexpected_point_fails():
    chunk1 = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk1.id)))
    expected_map = {p1: chunk1}

    rec1 = MagicMock()
    rec1.id = p1
    rec1.payload = {"original_id": str(chunk1.id), "document_id": "doc1", "workspace_id": "ws_1"}
    rec1.vector = {"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024}

    rec_unexp = MagicMock()
    rec_unexp.id = "99999999-9999-9999-9999-999999999999"
    rec_unexp.payload = {}
    rec_unexp.vector = {}

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([rec1, rec_unexp], None)

    ok, msg = verify_qdrant_post_write(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
        wait=True,
    )
    assert ok is False
    assert "unexpected points found in Qdrant" in msg


def test_verify_qdrant_post_write_invalid_payload_fails():
    chunk1 = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk1.id)))
    expected_map = {p1: chunk1}

    rec1 = MagicMock()
    rec1.id = p1
    rec1.payload = {"original_id": "wrong_id", "document_id": "doc1", "workspace_id": "ws_1"}
    rec1.vector = {"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024}

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([rec1], None)

    ok, msg = verify_qdrant_post_write(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
        wait=True,
    )
    assert ok is False
    assert "has invalid payload" in msg


def test_verify_qdrant_post_write_invalid_vector_dims_fails():
    chunk1 = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk1.id)))
    expected_map = {p1: chunk1}

    rec1 = MagicMock()
    rec1.id = p1
    rec1.payload = {"original_id": str(chunk1.id), "document_id": "doc1", "workspace_id": "ws_1"}
    rec1.vector = {"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 512}

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([rec1], None)

    ok, msg = verify_qdrant_post_write(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
        wait=True,
    )
    assert ok is False
    assert "has invalid vector dimensions" in msg


def test_verify_qdrant_post_write_missing_provenance_when_required_fails():
    chunk1 = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk1.id)))
    expected_map = {p1: chunk1}

    rec1 = MagicMock()
    rec1.id = p1
    rec1.payload = {"original_id": str(chunk1.id), "document_id": "doc1", "workspace_id": "ws_1"}
    rec1.vector = {"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024}

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([rec1], None)

    ok, msg = verify_qdrant_post_write(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
        wait=True,
        require_verified_provenance=True,
    )
    assert ok is False
    assert "lacks exact verified model provenance" in msg


def test_verify_qdrant_post_write_verified_provenance_succeeds():
    chunk1 = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk1.id)))
    expected_map = {p1: chunk1}

    rec1 = MagicMock()
    rec1.id = p1
    rec1.payload = {
        "original_id": str(chunk1.id),
        "document_id": "doc1",
        "workspace_id": "ws_1",
        "fast_model": "BAAI/bge-small-en-v1.5",
        "full_model": "amazon.titan-embed-text-v2:0",
    }
    rec1.vector = {"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024}

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([rec1], None)

    ok, msg = verify_qdrant_post_write(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
        wait=True,
        require_verified_provenance=True,
    )
    assert ok is True
    assert "all 1 points verified" in msg


# ---------------------------------------------------------------------------
# 5. Exact Model Provenance & Embedding Origin Tracing Tests
# ---------------------------------------------------------------------------

def test_inspect_existing_qdrant_points_wrong_fast_model_rejected():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    point_id = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {point_id: chunk}

    mock_record = MagicMock()
    mock_record.id = point_id
    mock_record.payload = {
        "original_id": str(chunk.id),
        "document_id": "doc1",
        "workspace_id": "ws_1",
        "fast_model": "text-embedding-3-small",
        "full_model": "amazon.titan-embed-text-v2:0",
    }
    mock_record.vector = {
        "embedding_fast": [0.1] * 384,
        "embedding_full": [0.2] * 1024,
    }

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([mock_record], None)

    results, _ = inspect_existing_qdrant_points(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )

    assert results[point_id].status == "corrupted"
    assert "Incompatible or unverified model provenance" in (results[point_id].reason or "")
    assert results[point_id].model_provenance_verified is False


def test_inspect_existing_qdrant_points_wrong_full_model_rejected():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    point_id = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {point_id: chunk}

    mock_record = MagicMock()
    mock_record.id = point_id
    mock_record.payload = {
        "original_id": str(chunk.id),
        "document_id": "doc1",
        "workspace_id": "ws_1",
        "fast_model": "BAAI/bge-small-en-v1.5",
        "full_model": "text-embedding-ada-002",
    }
    mock_record.vector = {
        "embedding_fast": [0.1] * 384,
        "embedding_full": [0.2] * 1024,
    }

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([mock_record], None)

    results, _ = inspect_existing_qdrant_points(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )

    assert results[point_id].status == "corrupted"
    assert "Incompatible or unverified model provenance" in (results[point_id].reason or "")
    assert results[point_id].model_provenance_verified is False


def test_inspect_existing_qdrant_points_fallback_model_tag_rejected():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    point_id = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {point_id: chunk}

    mock_record = MagicMock()
    mock_record.id = point_id
    mock_record.payload = {
        "original_id": str(chunk.id),
        "document_id": "doc1",
        "workspace_id": "ws_1",
        "fast_model": "fallback:deterministic",
        "full_model": "fallback:deterministic",
    }
    mock_record.vector = {
        "embedding_fast": [0.1] * 384,
        "embedding_full": [0.2] * 1024,
    }

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([mock_record], None)

    results, _ = inspect_existing_qdrant_points(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )

    assert results[point_id].status == "corrupted"
    assert "Incompatible or unverified model provenance" in (results[point_id].reason or "")
    assert results[point_id].model_provenance_verified is False


def test_embed_chunks_dual_records_model_provenance():
    from ingestion.embed_service import embed_chunks_dual
    from schemas.models import Chunk

    chunk = Chunk(
        id="c1",
        document_id="doc1",
        source_format="txt",
        text="test content",
        element_type="text",
        workspace_id="ws_1",
    )

    with patch("ingestion.embed_service.embed_fast_batch_with_provenance") as mock_fast, \
         patch("providers.embedding_provider.embed_batch_with_provenance") as mock_full:
        mock_fast.return_value = ([[0.1] * 384], "BAAI/bge-small-en-v1.5")
        mock_full.return_value = ([[0.2] * 1024], "amazon.titan-embed-text-v2:0")

        results = embed_chunks_dual([chunk], provider="prod")

    assert len(results) == 1
    assert results[0].metadata is not None
    assert results[0].metadata["fast_model"] == "BAAI/bge-small-en-v1.5"
    assert results[0].metadata["full_model"] == "amazon.titan-embed-text-v2:0"
    assert results[0].metadata["embedding_provider"] == "prod"


def test_embed_chunks_dual_records_fallback_provenance():
    from ingestion.embed_service import embed_chunks_dual
    from schemas.models import Chunk

    chunk = Chunk(
        id="c1",
        document_id="doc1",
        source_format="txt",
        text="test content",
        element_type="text",
        workspace_id="ws_1",
    )

    # In test environment, actual implementation should return fallback:deterministic
    results = embed_chunks_dual([chunk], provider="test")

    assert len(results) == 1
    assert results[0].metadata is not None
    assert results[0].metadata["fast_model"] == "fallback:deterministic"
    assert results[0].metadata["full_model"] == "fallback:deterministic"
    assert results[0].metadata["embedding_provider"] == "test"


def test_embed_batch_with_provenance_rejects_test_environment_as_titan():
    from providers.embedding_provider import embed_batch_with_provenance

    # When provider is test (or environment test with default provider), it returns fallback provenance
    with patch("config.settings.ENVIRONMENT", "test"), patch.dict("os.environ", {"ENVIRONMENT": "test"}):
        embeddings, model_name = embed_batch_with_provenance(["test line"], provider="test")

    assert model_name == "fallback:deterministic"
    assert len(embeddings) == 1
    assert len(embeddings[0]) == 1024


def test_verify_qdrant_post_write_rejects_fallback_provenance():
    chunk1 = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk1.id)))
    expected_map = {p1: chunk1}

    rec1 = MagicMock()
    rec1.id = p1
    rec1.payload = {
        "original_id": str(chunk1.id),
        "document_id": "doc1",
        "workspace_id": "ws_1",
        "fast_model": "fallback:deterministic",
        "full_model": "fallback:deterministic",
    }
    rec1.vector = {"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024}

    mock_client = MagicMock()
    mock_client.scroll.return_value = ([rec1], None)

    ok, msg = verify_qdrant_post_write(
        client=mock_client,
        collection_name="kre_chunks",
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
        wait=True,
        require_verified_provenance=True,
    )
    assert ok is False
    assert "lacks exact verified model provenance" in msg


def test_retrieve_landed_point_ids_rejects_fallback_provenance_when_required():
    chunk1 = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk1.id)))
    expected_map = {p1: chunk1}

    rec1 = MagicMock()
    rec1.id = p1
    rec1.payload = {
        "original_id": str(chunk1.id),
        "document_id": "doc1",
        "workspace_id": "ws_1",
        "fast_model": "fallback:deterministic",
        "full_model": "fallback:deterministic",
    }
    rec1.vector = {"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024}

    mock_client = MagicMock()
    mock_client.retrieve.return_value = [rec1]

    landed = retrieve_landed_point_ids(
        client=mock_client,
        collection_name="kre_chunks",
        point_ids=[p1],
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
        require_verified_provenance=True,
    )
    assert p1 not in landed
    assert len(landed) == 0


# ---------------------------------------------------------------------------
# 6. Pre-upsert Validation & Provider Availability Preflight Tests
# ---------------------------------------------------------------------------

class DummyPointStruct:
    def __init__(self, id: str, vector: dict, payload: dict):
        self.id = id
        self.vector = vector
        self.payload = payload


def test_validate_batch_before_upsert_valid_succeeds():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {p1: chunk}

    point = DummyPointStruct(
        id=p1,
        vector={"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024},
        payload={
            "original_id": str(chunk.id),
            "document_id": "doc1",
            "workspace_id": "ws_1",
            "page_number": None,
            "fast_model": "BAAI/bge-small-en-v1.5",
            "full_model": "amazon.titan-embed-text-v2:0",
        },
    )

    ok, reason = validate_batch_before_upsert(
        points=[point],
        chunk_batch=[chunk],
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )
    assert ok is True
    assert "All 1 points in batch verified for upsert" in reason


def test_validate_batch_before_upsert_bge_failure_fallback_rejected():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {p1: chunk}

    point = DummyPointStruct(
        id=p1,
        vector={"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024},
        payload={
            "original_id": str(chunk.id),
            "document_id": "doc1",
            "workspace_id": "ws_1",
            "page_number": None,
            "fast_model": "fallback:deterministic",
            "full_model": "amazon.titan-embed-text-v2:0",
        },
    )

    ok, reason = validate_batch_before_upsert(
        points=[point],
        chunk_batch=[chunk],
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )
    assert ok is False
    assert "invalid or fallback fast_model provenance" in reason


def test_validate_batch_before_upsert_test_mode_fallback_rejected():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {p1: chunk}

    point = DummyPointStruct(
        id=p1,
        vector={"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024},
        payload={
            "original_id": str(chunk.id),
            "document_id": "doc1",
            "workspace_id": "ws_1",
            "page_number": None,
            "fast_model": "BAAI/bge-small-en-v1.5",
            "full_model": "fallback:deterministic",
        },
    )

    ok, reason = validate_batch_before_upsert(
        points=[point],
        chunk_batch=[chunk],
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )
    assert ok is False
    assert "invalid, fallback, or mixed full_model provenance" in reason


def test_validate_batch_before_upsert_wrong_model_rejected():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {p1: chunk}

    point = DummyPointStruct(
        id=p1,
        vector={"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024},
        payload={
            "original_id": str(chunk.id),
            "document_id": "doc1",
            "workspace_id": "ws_1",
            "page_number": None,
            "fast_model": "BAAI/bge-small-en-v1.5",
            "full_model": "text-embedding-ada-002",
        },
    )

    ok, reason = validate_batch_before_upsert(
        points=[point],
        chunk_batch=[chunk],
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )
    assert ok is False
    assert "invalid, fallback, or mixed full_model provenance" in reason


def test_validate_batch_before_upsert_mixed_batch_rejected():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {p1: chunk}

    point = DummyPointStruct(
        id=p1,
        vector={"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024},
        payload={
            "original_id": str(chunk.id),
            "document_id": "doc1",
            "workspace_id": "ws_1",
            "page_number": None,
            "fast_model": "BAAI/bge-small-en-v1.5",
            "full_model": "mixed:amazon.titan-embed-text-v2:0,fallback:deterministic",
        },
    )

    ok, reason = validate_batch_before_upsert(
        points=[point],
        chunk_batch=[chunk],
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )
    assert ok is False
    assert "invalid, fallback, or mixed full_model provenance" in reason


def test_validate_batch_before_upsert_incomplete_batch_rejected():
    chunk1 = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    chunk2 = DummyChunk("doc1:csv:row2", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk1.id)))
    expected_map = {p1: chunk1}

    point1 = DummyPointStruct(
        id=p1,
        vector={"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024},
        payload={
            "original_id": str(chunk1.id),
            "document_id": "doc1",
            "workspace_id": "ws_1",
            "fast_model": "BAAI/bge-small-en-v1.5",
            "full_model": "amazon.titan-embed-text-v2:0",
        },
    )

    # 1 point for 2 chunks -> incomplete
    ok, reason = validate_batch_before_upsert(
        points=[point1],
        chunk_batch=[chunk1, chunk2],
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )
    assert ok is False
    assert "Incomplete batch" in reason


def test_validate_batch_before_upsert_missing_tags_rejected():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {p1: chunk}

    point = DummyPointStruct(
        id=p1,
        vector={"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024},
        payload={
            "original_id": str(chunk.id),
            "document_id": "doc1",
            "workspace_id": "ws_1",
            # missing fast_model and full_model
        },
    )

    ok, reason = validate_batch_before_upsert(
        points=[point],
        chunk_batch=[chunk],
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )
    assert ok is False
    assert "lacks fast_model provenance tag" in reason


def test_validate_batch_before_upsert_dimensions_mismatch_rejected():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    expected_map = {p1: chunk}

    point = DummyPointStruct(
        id=p1,
        vector={"embedding_fast": [0.1] * 128, "embedding_full": [0.2] * 1024},
        payload={
            "original_id": str(chunk.id),
            "document_id": "doc1",
            "workspace_id": "ws_1",
            "fast_model": "BAAI/bge-small-en-v1.5",
            "full_model": "amazon.titan-embed-text-v2:0",
        },
    )

    ok, reason = validate_batch_before_upsert(
        points=[point],
        chunk_batch=[chunk],
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )
    assert ok is False
    assert "fast embedding dimension invalid" in reason


def test_validate_batch_before_upsert_identity_mismatch_rejected():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    wrong_id = "00000000-0000-0000-0000-000000000000"
    expected_map = {wrong_id: chunk}

    point = DummyPointStruct(
        id=wrong_id,
        vector={"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024},
        payload={
            "original_id": str(chunk.id),
            "document_id": "doc1",
            "workspace_id": "ws_1",
            "fast_model": "BAAI/bge-small-en-v1.5",
            "full_model": "amazon.titan-embed-text-v2:0",
        },
    )

    ok, reason = validate_batch_before_upsert(
        points=[point],
        chunk_batch=[chunk],
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )
    assert ok is False
    assert "Point ID mismatch" in reason


def test_validate_batch_before_upsert_missing_from_expected_point_map_rejected():
    chunk = DummyChunk("doc1:csv:row1", "doc1", "ws_1")
    p1 = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
    # expected_map is empty -> point not found in expected point map
    expected_map = {}

    point = DummyPointStruct(
        id=p1,
        vector={"embedding_fast": [0.1] * 384, "embedding_full": [0.2] * 1024},
        payload={
            "original_id": str(chunk.id),
            "document_id": "doc1",
            "workspace_id": "ws_1",
            "fast_model": "BAAI/bge-small-en-v1.5",
            "full_model": "amazon.titan-embed-text-v2:0",
        },
    )

    ok, reason = validate_batch_before_upsert(
        points=[point],
        chunk_batch=[chunk],
        workspace_id="ws_1",
        document_id="doc1",
        expected_point_map=expected_map,
    )
    assert ok is False
    assert "not found in expected point map" in reason


def test_embed_batch_with_provenance_mixed_batch_returns_mixed_tag():
    from providers.embedding_provider import embed_batch_with_provenance

    def _mock_single(text, provider=None, max_retries=5):
        if text == "first":
            return [0.1] * 1024, "amazon.titan-embed-text-v2:0"
        return [0.2] * 1024, "fallback:deterministic"

    with patch("config.settings.ENVIRONMENT", "dev"), patch.dict("os.environ", {"ENVIRONMENT": "dev"}):
        with patch("providers.embedding_provider.embed_text_with_provenance", side_effect=_mock_single):
            vecs, model = embed_batch_with_provenance(["first", "second"], provider="prod")

    assert len(vecs) == 2
    assert model == "mixed:amazon.titan-embed-text-v2:0,fallback:deterministic"


def test_embed_text_with_provenance_executed_model():
    from providers.embedding_provider import embed_text_with_provenance
    import io
    import json

    mock_client = MagicMock()
    mock_body = MagicMock()
    mock_body.read.return_value = json.dumps({
        "inputTextTokenCount": 10,
        "embedding": [0.1] * 1024,
    }).encode("utf-8")
    mock_client.invoke_model.return_value = {"body": mock_body}

    with patch("config.settings.ENVIRONMENT", "dev"), patch.dict("os.environ", {"ENVIRONMENT": "dev"}):
        with patch("aws.infra.get_client", return_value=mock_client):
            vec, model_name = embed_text_with_provenance("sample text", provider="prod")

    assert len(vec) == 1024
    assert model_name == "amazon.titan-embed-text-v2:0"
    mock_client.invoke_model.assert_called_once()
    assert mock_client.invoke_model.call_args[1]["modelId"] == "amazon.titan-embed-text-v2:0"


def test_verify_provider_availability_fails_closed_in_test_env():
    with patch("config.settings.ENVIRONMENT", "test"), patch.dict("os.environ", {"ENVIRONMENT": "test"}):
        ok, msg, _ = verify_provider_availability()
    assert ok is False
    assert "ENVIRONMENT is configured as 'test'" in msg


def test_verify_provider_availability_fails_closed_on_lambda_error():
    mock_lam = MagicMock()
    mock_lam.get_function_configuration.side_effect = RuntimeError("ResourceNotFoundException")

    with patch("config.settings.ENVIRONMENT", "dev"), patch.dict("os.environ", {"ENVIRONMENT": "dev"}):
        with patch("aws.infra.get_client", return_value=mock_lam):
            ok, msg, _ = verify_provider_availability(require_bge_lambda=True, require_bedrock=False)
    assert ok is False
    assert "Unable to verify BGE Lambda" in msg


def test_verify_provider_availability_success():
    mock_lam = MagicMock()
    mock_lam.get_function_configuration.return_value = {"State": "Active"}
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Arn": "arn:aws:iam::123456:user/tester"}

    def _get_client_mock(service):
        if service == "lambda":
            return mock_lam
        if service == "sts":
            return mock_sts
        return MagicMock()

    with patch("config.settings.ENVIRONMENT", "dev"), patch.dict("os.environ", {"ENVIRONMENT": "dev"}):
        with patch("aws.infra.get_client", side_effect=_get_client_mock):
            ok, msg, details = verify_provider_availability(require_bge_lambda=True, require_bedrock=True)
    assert ok is True
    assert details["bge_lambda_state"] == "Active"
    assert "Provider preflight verified" in msg



