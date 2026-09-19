"""Base protocol and definitions for pluggable TableStore backends."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from schemas.structured_table import StructuredTable, TableRow


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
    """Protocol for structured table storage backends (Memory, SQLite, Parquet/S3).

    Every method strictly requires workspace_id to enforce repository-level
    workspace ownership and isolation.
    """

    def store_table(self, table: StructuredTable, workspace_id: str) -> None:
        """Store a structured table and its rows."""
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
    ) -> Decimal | None:
        """Compute an exact deterministic aggregation over a column."""
        ...

    def delete_table(self, table_id: str, workspace_id: str) -> bool:
        """Delete table and all its stored rows."""
        ...
