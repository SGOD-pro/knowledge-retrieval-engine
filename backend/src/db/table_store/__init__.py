import logging
import os
from pathlib import Path

from config import settings
from db.table_store.base import AggregateOp, Predicate, PredicateOp, TableStore
from db.table_store.dynamodb_store import DynamoDBTableStore
from db.table_store.memory_store import MemoryTableStore
from db.table_store.sqlite_store import SQLiteTableStore

logger = logging.getLogger(__name__)

_SHARED_TABLE_STORE: TableStore | None = None


def get_shared_table_store(reset: bool = False, require_durable: bool = False) -> TableStore:
    """Return process-scoped shared TableStore.

    Backend selection:
      1. Explicit environment variable: KRE_TABLE_STORE_BACKEND in ('dynamodb', 'sqlite', 'memory').
         - 'dynamodb': Must be reachable; fails with actionable RuntimeError on unreachable/missing table.
                       Does NOT silently fall back to MemoryTableStore or SQLite.
         - 'sqlite': Permitted only with an explicitly configured persistent local file path
                     via KRE_TABLE_STORE_SQLITE_PATH. Absolute path is recorded.
         - 'memory': Ephemeral in-memory store; rejected when require_durable=True or in prod.
      2. If require_durable=True and no durable backend is configured, raises actionable RuntimeError.
      3. ENVIRONMENT == 'prod': DynamoDBTableStore (hard failure if DynamoDB unreachable).
      4. ENVIRONMENT == 'dev': Live DynamoDBTableStore if reachable, else MemoryTableStore with loud warning.
      5. ENVIRONMENT == 'test': MemoryTableStore (ephemeral unit tests).
    """
    global _SHARED_TABLE_STORE
    if reset or _SHARED_TABLE_STORE is None:
        backend_override = (
            os.getenv("KRE_TABLE_STORE_BACKEND")
            or getattr(settings, "TABLE_STORE_BACKEND", None)
            or ""
        ).lower().strip()

        if backend_override:
            if backend_override == "dynamodb":
                store = DynamoDBTableStore()
                last_err = None
                for attempt in range(5):
                    try:
                        store.client.describe_table(TableName=store.table_name)
                        _SHARED_TABLE_STORE = store
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        import time
                        time.sleep(min(5.0, (2 ** attempt) * 0.5))
                if last_err is not None:
                    logger.critical(
                        "Explicitly configured DynamoDB TableStore '%s' is unreachable or misconfigured: %s",
                        getattr(store, "table_name", settings.DYNAMODB_TABLE_NAME),
                        last_err,
                    )
                    raise RuntimeError(
                        f"Explicitly configured DynamoDB TableStore '{getattr(store, 'table_name', settings.DYNAMODB_TABLE_NAME)}' "
                        f"is unreachable or misconfigured: {last_err}. Silent fallback is forbidden."
                    ) from last_err

            elif backend_override == "sqlite":
                sqlite_path = (
                    os.getenv("KRE_TABLE_STORE_SQLITE_PATH")
                    or getattr(settings, "TABLE_STORE_SQLITE_PATH", None)
                    or ""
                ).strip()

                if not sqlite_path or sqlite_path == ":memory:":
                    if require_durable or settings.ENVIRONMENT != "test":
                        raise RuntimeError(
                            "KRE_TABLE_STORE_BACKEND is 'sqlite' but KRE_TABLE_STORE_SQLITE_PATH is not set to a "
                            "persistent file path. SQLite is only permitted as an explicitly configured persistent "
                            "local backend with an absolute path."
                        )
                    _SHARED_TABLE_STORE = SQLiteTableStore(":memory:")
                else:
                    store = SQLiteTableStore(sqlite_path)
                    logger.info("Configured persistent SQLite TableStore at: %s", store.absolute_db_path)
                    _SHARED_TABLE_STORE = store

            elif backend_override == "memory":
                if require_durable or settings.ENVIRONMENT == "prod":
                    raise RuntimeError("MemoryTableStore cannot be used when durable backend is required.")
                _SHARED_TABLE_STORE = MemoryTableStore()

            else:
                raise ValueError(
                    f"Invalid KRE_TABLE_STORE_BACKEND '{backend_override}'. Expected 'dynamodb', 'sqlite', or 'memory'."
                )

        else:
            # No explicit backend override specified
            if require_durable:
                raise RuntimeError(
                    "Benchmark run requires an explicitly configured durable TableStore backend ('dynamodb' or 'sqlite'). "
                    "Missing or unconfigured durable storage is forbidden. Set KRE_TABLE_STORE_BACKEND=dynamodb or "
                    "KRE_TABLE_STORE_BACKEND=sqlite with KRE_TABLE_STORE_SQLITE_PATH."
                )

            if settings.ENVIRONMENT == "prod":
                try:
                    store = DynamoDBTableStore()
                    store.client.describe_table(TableName=store.table_name)
                    _SHARED_TABLE_STORE = store
                except Exception as e:
                    logger.critical("Failed to reach DynamoDB table in production: %s", e)
                    raise RuntimeError(f"DynamoDBTableStore unreachable in production: {e}") from e

            elif settings.ENVIRONMENT == "dev":
                try:
                    store = DynamoDBTableStore()
                    store.client.describe_table(TableName=store.table_name)
                    logger.info("Connected to live DynamoDB table '%s' for dev TableStore", store.table_name)
                    _SHARED_TABLE_STORE = store
                except Exception as e:
                    logger.warning(
                        "Live DynamoDB unreachable in dev (%s); using MemoryTableStore (data will not persist across processes).",
                        e,
                    )
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
