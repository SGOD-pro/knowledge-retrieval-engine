"""Structured execution result contract representing tabular query outcomes.

Enforces execution proof outside reciprocal rank fusion, attempt counters,
and evaluation invariant validation.
"""

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.schemas.contracts.envelope import TrustedAuthContext
from src.schemas.contracts.payload import DatasetReference, ScalarValue
from src.schemas.contracts.query import SelectionPredicate

ExecutionCoverageStatus = Literal[
    "COMPLETE_FOR_SELECTION",
    "EMPTY_SELECTION",
    "INCOMPLETE_EXECUTION",
]


class StructuredExecutionResult(BaseModel):
    """Query plane execution result for tabular operations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str
    query_id: str
    snapshot_id: str
    requirement_id: str
    attempt_number: int = Field(default=1, ge=1)
    execution_id: str
    evidence_id: str
    source_id: str
    source_version: int = Field(..., ge=1)
    table_id: str
    operation: str
    metric: str | None = None
    selection: tuple[SelectionPredicate, ...] = Field(default_factory=tuple)
    records_examined: int = Field(..., ge=0)
    records_matched: int = Field(..., ge=0)
    selection_digest: str
    lineage_ref: str
    result_value: ScalarValue
    result_unit: str | None = None
    coverage_status: ExecutionCoverageStatus
    executed_at: str

    @field_validator(
        "workspace_id",
        "query_id",
        "snapshot_id",
        "requirement_id",
        "execution_id",
        "evidence_id",
        "source_id",
        "table_id",
        "operation",
        "selection_digest",
        "lineage_ref",
        "executed_at",
    )
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field must be a non empty string")
        return v

    @field_validator("selection", mode="before")
    @classmethod
    def coerce_selection(cls, v: Any) -> tuple[SelectionPredicate, ...]:
        if isinstance(v, (list, tuple)):
            coerced: list[SelectionPredicate] = []
            for item in v:
                if isinstance(item, SelectionPredicate):
                    coerced.append(item)
                elif isinstance(item, dict):
                    coerced.append(SelectionPredicate(**item))
                else:
                    raise ValueError(f"Invalid selection predicate: {item}")
            return tuple(coerced)
        return v

    @model_validator(mode="after")
    def validate_execution_invariants(self) -> "StructuredExecutionResult":
        # AC-14: Invariant 0 <= records_matched <= records_examined
        if self.records_matched > self.records_examined:
            raise ValueError(
                f"records_matched ({self.records_matched}) cannot exceed "
                f"records_examined ({self.records_examined})"
            )

        # Strict coverage_status semantics
        if self.coverage_status == "COMPLETE_FOR_SELECTION":
            if self.records_matched == 0 and self.records_examined > 0:
                raise ValueError(
                    "Execution matched zero records; coverage_status must be 'EMPTY_SELECTION', "
                    "not 'COMPLETE_FOR_SELECTION'"
                )
        elif self.coverage_status == "EMPTY_SELECTION":
            if self.records_matched > 0:
                raise ValueError(
                    f"Execution matched {self.records_matched} records; coverage_status cannot be 'EMPTY_SELECTION'"
                )

        return self

    @classmethod
    def create_result(
        cls,
        auth_context: TrustedAuthContext,
        query_id: str,
        snapshot_id: str,
        requirement_id: str,
        dataset_ref: DatasetReference,
        operation: str,
        records_examined: int,
        records_matched: int,
        selection_digest: str,
        lineage_ref: str,
        result_value: ScalarValue,
        coverage_status: ExecutionCoverageStatus,
        executed_at: str,
        attempt_number: int = 1,
        execution_id: str | None = None,
        metric: str | None = None,
        selection: tuple[SelectionPredicate, ...] = (),
        result_unit: str | None = None,
    ) -> "StructuredExecutionResult":
        """Factory method constructing StructuredExecutionResult with deterministic identifiers."""
        exec_id = execution_id or f"exec_{query_id}_{requirement_id}_att{attempt_number}"
        evidence_id = f"execution:{auth_context.authorized_workspace_id}:{exec_id}"
        return cls(
            workspace_id=auth_context.authorized_workspace_id,
            query_id=query_id,
            snapshot_id=snapshot_id,
            requirement_id=requirement_id,
            attempt_number=attempt_number,
            execution_id=exec_id,
            evidence_id=evidence_id,
            source_id=dataset_ref.source_id,
            source_version=dataset_ref.source_version,
            table_id=dataset_ref.table_id,
            operation=operation,
            metric=metric,
            selection=selection,
            records_examined=records_examined,
            records_matched=records_matched,
            selection_digest=selection_digest,
            lineage_ref=lineage_ref,
            result_value=result_value,
            result_unit=result_unit,
            coverage_status=coverage_status,
            executed_at=executed_at,
        )
