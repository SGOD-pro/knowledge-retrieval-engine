import os
import logging

from config import settings
from db.table_store.base import AggregateOp, Predicate, PredicateOp, TableStore
from db.table_store.dynamodb_store import DynamoDBTableStore
from db.table_store.memory_store import MemoryTableStore
from db.table_store.sqlite_store import SQLiteTableStore

logger = logging.getLogger(__name__)

_SHARED_TABLE_STORE: TableStore | None = None


def get_shared_table_store(reset: bool = False) -> TableStore:
    """Return process-scoped shared TableStore.

    Backend selection:
      1. Explicit environment variable: KRE_TABLE_STORE_BACKEND in ('dynamodb', 'memory', 'sqlite').
      2. ENVIRONMENT == 'prod': DynamoDBTableStore (hard failure if DynamoDB unreachable).
      3. ENVIRONMENT == 'dev': DynamoDBTableStore if local DynamoDB / FLOCI reachable, else MemoryTableStore.
      4. ENVIRONMENT == 'test': MemoryTableStore (unless KRE_TABLE_STORE_BACKEND is set).
    """
    global _SHARED_TABLE_STORE
    if reset or _SHARED_TABLE_STORE is None:
        backend_override = os.getenv("KRE_TABLE_STORE_BACKEND", "").lower().strip()

        if backend_override == "dynamodb":
            _SHARED_TABLE_STORE = DynamoDBTableStore()
        elif backend_override == "sqlite":
            _SHARED_TABLE_STORE = SQLiteTableStore(":memory:")
        elif backend_override == "memory":
            _SHARED_TABLE_STORE = MemoryTableStore()
        elif settings.ENVIRONMENT == "prod":
            try:
                store = DynamoDBTableStore()
                # Verify reachable
                store.client.describe_table(TableName=store.table_name)
                _SHARED_TABLE_STORE = store
            except Exception as e:
                logger.critical("Failed to reach DynamoDB table in production: %s", e)
                raise RuntimeError(f"DynamoDBTableStore unreachable in production: {e}") from e
        elif settings.ENVIRONMENT == "dev":
            try:
                from aws.infra import _is_floci_available
                if _is_floci_available():
                    _SHARED_TABLE_STORE = DynamoDBTableStore()
                else:
                    _SHARED_TABLE_STORE = MemoryTableStore()
            except Exception:
                _SHARED_TABLE_STORE = MemoryTableStore()
        else:  # test / default
            _SHARED_TABLE_STORE = MemoryTableStore()

    return _SHARED_TABLE_STORE


__all__ = [
    "TableStore",
    "Predicate",
    "PredicateOp",
    "AggregateOp",
    "MemoryTableStore",
    "SQLiteTableStore",
    "DynamoDBTableStore",
    "get_shared_table_store",
]
