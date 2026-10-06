import os
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from db.table_store import (
    DynamoDBTableStore,
    MemoryTableStore,
    SQLiteTableStore,
    get_shared_table_store,
)


def test_tablestore_explicit_dynamodb_unreachable_raises_actionable_error(monkeypatch):
    """When DynamoDB is configured but unreachable, raise actionable RuntimeError (no silent fallback)."""
    monkeypatch.setenv("KRE_TABLE_STORE_BACKEND", "dynamodb")

    with patch("db.table_store.dynamodb_store.DynamoDBTableStore.client") as mock_client:
        mock_client.describe_table.side_effect = Exception("Connection refused / table missing")
        with pytest.raises(RuntimeError, match="unreachable or misconfigured"):
            get_shared_table_store(reset=True)


def test_tablestore_explicit_sqlite_requires_persistent_path(monkeypatch):
    """When SQLite is selected as durable backend, KRE_TABLE_STORE_SQLITE_PATH must be set to a persistent file."""
    monkeypatch.setenv("KRE_TABLE_STORE_BACKEND", "sqlite")
    monkeypatch.delenv("KRE_TABLE_STORE_SQLITE_PATH", raising=False)

    with pytest.raises(RuntimeError, match="persistent file path"):
        get_shared_table_store(reset=True, require_durable=True)


def test_tablestore_explicit_sqlite_records_absolute_path(monkeypatch, tmp_path):
    """When SQLite has a persistent path, it records its absolute path on the store."""
    db_file = tmp_path / "sub" / "table_store.db"
    monkeypatch.setenv("KRE_TABLE_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("KRE_TABLE_STORE_SQLITE_PATH", str(db_file))

    store = get_shared_table_store(reset=True, require_durable=True)
    assert isinstance(store, SQLiteTableStore)
    assert hasattr(store, "absolute_db_path")
    assert store.absolute_db_path == str(db_file.resolve())
    assert Path(store.absolute_db_path).exists() or db_file.parent.exists()


def test_tablestore_memory_rejected_when_durable_required(monkeypatch):
    """MemoryTableStore is rejected when require_durable=True."""
    monkeypatch.setenv("KRE_TABLE_STORE_BACKEND", "memory")

    with pytest.raises(RuntimeError, match="MemoryTableStore cannot be used when durable backend is required"):
        get_shared_table_store(reset=True, require_durable=True)


def test_tablestore_unconfigured_rejected_when_durable_required(monkeypatch):
    """When no backend is configured, require_durable=True raises an actionable error."""
    monkeypatch.delenv("KRE_TABLE_STORE_BACKEND", raising=False)

    with pytest.raises(RuntimeError, match="explicitly configured durable TableStore backend"):
        get_shared_table_store(reset=True, require_durable=True)
