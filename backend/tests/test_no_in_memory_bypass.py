from unittest.mock import MagicMock
import pytest
import db.database as db_mod
from db.database import CloudRepository
from schemas.models import Chunk, Document


def test_no_in_memory_bypass_outside_test_env(monkeypatch):
    # Clean in-memory dicts
    db_mod._IN_MEMORY_CHUNKS.clear()
    db_mod._IN_MEMORY_DOCS.clear()

    monkeypatch.setattr("config.settings.ENVIRONMENT", "dev")
    monkeypatch.setenv("ENVIRONMENT", "dev")

    repo = CloudRepository()
    mock_table = MagicMock()
    mock_batch = MagicMock()
    mock_table.batch_writer.return_value.__enter__.return_value = mock_batch
    mock_table.query.return_value = {"Items": []}
    repo.table = mock_table

    mock_qclient = MagicMock()
    repo.qclient = mock_qclient

    doc = Document(
        id="doc_dev_1",
        filename="dev_doc.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="chunk_dev_1",
                document_id="doc_dev_1",
                source_format="pdf",
                text="Dev document content here.",
                element_type="paragraph",
                page_number=1,
                embedding_fast=[0.1] * 384,
                embedding_full=[0.1] * 1024,
                workspace_id="ws_dev",
            ),
        ),
        workspace_id="ws_dev",
    )

    repo.save(doc)

    assert len(db_mod._IN_MEMORY_CHUNKS) == 0, f"Expected _IN_MEMORY_CHUNKS to be empty outside test env, found {len(db_mod._IN_MEMORY_CHUNKS)}"
    assert len(db_mod._IN_MEMORY_DOCS) == 0, f"Expected _IN_MEMORY_DOCS to be empty outside test env, found {len(db_mod._IN_MEMORY_DOCS)}"

    chunks = repo.get_all_chunks(workspace_id="ws_dev")
    assert chunks == []
    mock_table.query.assert_called_once()
