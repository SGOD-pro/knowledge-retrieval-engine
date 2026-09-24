"""Base protocol and definitions for pluggable TableStore backends."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from schemas.structured_table import StructuredTable, TableRow, TableSchema


class PredicateOp(str, Enum):
    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    LT = "lt"
    GTE = "gte"
    LTE = "lte"
    CONTAINS = "contains"
    IN = "in"


class AggregateOp(str, Enum):
    SUM = "sum"
    AVG = "avg"
    COUNT = "count"
    MIN = "min"
    MAX = "max"


@dataclass(frozen=True)
class Predicate:
    column_index: int
    op: PredicateOp
    value: Any


class TableStore(Protocol):
    """Protocol for structured table storage backends (Memory, SQLite, DynamoDB).

    Every method strictly requires workspace_id to enforce repository-level
    workspace ownership and isolation.
    """

    def store_table(self, table: StructuredTable, workspace_id: str) -> None:
        """Store a structured table and its rows (legacy full-table store)."""
        ...

    def get_table(self, table_id: str, workspace_id: str) -> StructuredTable | None:
        """Retrieve table schema and metadata by table ID."""
        ...

    def query_rows(
        self,
        table_id: str,
        workspace_id: str,
        predicates: list[Predicate] | None = None,
        limit: int | None = None,
        version: str | None = None,
    ) -> list[TableRow]:
        """Query rows matching predicates with optional limit."""
        ...

    def aggregate(
        self,
        table_id: str,
        workspace_id: str,
        column_index: int,
        op: AggregateOp,
        predicates: list[Predicate] | None = None,
        version: str | None = None,
    ) -> Decimal | None:
        """Compute an exact deterministic aggregation over a column."""
        ...

    def delete_table(self, table_id: str, workspace_id: str) -> bool:
        """Delete table and all its stored rows."""
        ...

    def store_schema(
        self,
        table_id: str,
        workspace_id: str,
        schema: TableSchema,
        document_id: str,
        document_version: str,
        parser_version: str = "1.0.0",
        schema_version: str = "",
    ) -> None:
        """Store versioned table schema."""
        ...

    def write_row_batch_with_checkpoint(
        self,
        table_id: str,
        workspace_id: str,
        document_id: str,
        version: str,
        rows: list[TableRow],
        checkpoint_batch: int,
        source_rows: int,
        attempted_rows: int,
        persisted_rows: int,
        rejected_rows: int,
        parser_version: str,
        schema_version: str,
        content_hash: str,
    ) -> None:
        """Write a batch of rows and update ingestion checkpoint atomically."""
        ...

    def publish_active_version(
        self,
        table_id: str,
        workspace_id: str,
        document_id: str,
        version: str,
        manifest: dict,
    ) -> None:
        """Publish active version pointer and immutable manifest after validation."""
        ...

    def get_active_version(self, table_id: str, workspace_id: str) -> str | None:
        """Retrieve the currently published active version for a table."""
        ...

    def list_tables(self, workspace_id: str) -> list[str]:
        """List all active tables in workspace via catalogue lookup (no table scans)."""
        ...

    def get_ingestion_manifest(
        self,
        document_id: str,
        workspace_id: str,
        version: str | None = None,
    ) -> dict | None:
        """Retrieve ingestion manifest for document."""
        ...

    def get_checkpoint(
        self,
        document_id: str,
        workspace_id: str,
        version: str,
    ) -> dict | None:
        """Retrieve in-progress checkpoint for document version."""
        ...

    def has_coverage_complete_table(self, table_id: str, workspace_id: str) -> bool:
        """Return True only if the table has active version with coverage_complete."""
        ...

    def execute_single_pass_query(
        self,
        table_id: str,
        workspace_id: str,
        column_index: int | None,
        op: AggregateOp | None,
        predicates: list[Predicate] | None = None,
        limit: int | None = None,
        version: str | None = None,
    ) -> tuple[Decimal | None, int, list[str], list[int], str]:
        """Single-pass streaming execution computing aggregate, count, row_ids, indices, and digest."""
        ...

    def invalidate_document_version(
        self,
        document_id: str,
        workspace_id: str,
        version: str | None = None,
    ) -> int:
        """Invalidate specific version or all versions of a document."""
        ...
