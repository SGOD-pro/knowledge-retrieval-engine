"""Query contract, requirement decomposition, selection predicates, and execution policies.

Enforces operator specific predicate validation, requirement uniqueness,
conjunction semantics, server resolved answer modes, and resource ceilings.
"""

from typing import Any, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.schemas.contracts.envelope import TrustedAuthContext
from src.schemas.contracts.location import Location
from src.schemas.contracts.payload import OrderedScalar, ScalarPrimitive

SelectionOperator = Literal[
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "between",
    "contains",
]

RequirementIntent = Literal["lookup", "aggregate", "compare", "summarize", "filter"]

OperationType = Literal[
    "sum",
    "avg",
    "count",
    "min",
    "max",
    "exact_match",
    "ratio",
    "difference",
]

AnswerMode = Literal["deterministic", "narrative", "hybrid"]


class SelectionPredicate(BaseModel):
    """Operator specific selection predicate.

    Multiple predicates in a selection tuple combine strictly via logical AND.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    operator: SelectionOperator
    value: Union[
        ScalarPrimitive,
        tuple[ScalarPrimitive, ...],
        tuple[OrderedScalar, OrderedScalar],
    ]

    @field_validator("field")
    @classmethod
    def validate_field(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Predicate field must be a non empty string")
        return v

    @field_validator("value", mode="before")
    @classmethod
    def coerce_value_tuple(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v

    @model_validator(mode="after")
    def validate_operator_value_shape(self) -> "SelectionPredicate":
        op = self.operator
        val = self.value

        # AC-11 Operator Specific Predicate Validation Matrix
        if op in ("eq", "neq"):
            if not isinstance(val, (str, int, float, bool)):
                raise ValueError(
                    f"Operator '{op}' requires a single scalar primitive value; received {type(val).__name__}"
                )

        elif op in ("gt", "gte", "lt", "lte"):
            if isinstance(val, bool) or not isinstance(val, (int, float, str)):
                raise ValueError(
                    f"Operator '{op}' requires a single ordered scalar (int, float, or str); received {type(val).__name__}"
                )

        elif op in ("in", "not_in"):
            if not isinstance(val, tuple) or len(val) == 0:
                raise ValueError(
                    f"Operator '{op}' requires a non empty tuple of scalar values; received {val}"
                )
            for item in val:
                if not isinstance(item, (str, int, float, bool)):
                    raise ValueError(
                        f"Operator '{op}' items must be scalar primitives; found {type(item).__name__}"
                    )

        elif op == "between":
            if not isinstance(val, tuple) or len(val) != 2:
                raise ValueError(
                    "Operator 'between' requires a 2 element tuple (lower_bound, upper_bound)"
                )
            low, high = val
            if isinstance(low, bool) or isinstance(high, bool):
                raise ValueError("Operator 'between' bounds cannot be boolean")
            if not (isinstance(low, (int, float, str)) and isinstance(high, (int, float, str))):
                raise ValueError("Operator 'between' bounds must be ordered scalars")
            # Bounds order check
            try:
                if low > high:  # type: ignore[operator]
                    raise ValueError(
                        f"Operator 'between' lower bound ({low}) must be <= upper bound ({high})"
                    )
            except TypeError as err:
                raise ValueError(
                    f"Cannot compare lower bound {low} ({type(low).__name__}) with upper bound {high} ({type(high).__name__})"
                ) from err

        elif op == "contains":
            if not isinstance(val, str) or not val.strip():
                raise ValueError(
                    "Operator 'contains' requires a non empty string search pattern"
                )

        return self


class RequirementTarget(BaseModel):
    """Target dataset and metric for a query requirement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset: str | None = None
    metric: str | None = None


class EvidenceRequirement(BaseModel):
    """Specification of required supporting evidence for a query requirement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    locator: Location | None = None
    selection: tuple[SelectionPredicate, ...] = Field(default_factory=tuple)

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


class RequirementItem(BaseModel):
    """Decomposed query requirement item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: str
    intent: RequirementIntent
    operation: OperationType | None = None
    target: RequirementTarget | None = None
    selection: tuple[SelectionPredicate, ...] = Field(default_factory=tuple)
    required_evidence: tuple[EvidenceRequirement, ...] = Field(default_factory=tuple)

    @field_validator("requirement_id")
    @classmethod
    def validate_req_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("requirement_id must be a non empty string")
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

    @field_validator("required_evidence", mode="before")
    @classmethod
    def coerce_required_evidence(cls, v: Any) -> tuple[EvidenceRequirement, ...]:
        if isinstance(v, (list, tuple)):
            coerced: list[EvidenceRequirement] = []
            for item in v:
                if isinstance(item, EvidenceRequirement):
                    coerced.append(item)
                elif isinstance(item, dict):
                    coerced.append(EvidenceRequirement(**item))
                else:
                    raise ValueError(f"Invalid evidence requirement: {item}")
            return tuple(coerced)
        return v

    @model_validator(mode="after")
    def validate_operation_intent_compatibility(self) -> "RequirementItem":
        if self.intent == "aggregate" and self.operation is None:
            raise ValueError("RequirementItem with intent 'aggregate' requires an operation")
        return self


class ServerExecutionPolicy(BaseModel):
    """Server controlled execution policy separating streaming batches from total ceilings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str = "query-default-v3"
    max_query_llm_calls: int = Field(default=2, ge=1)
    max_query_rerank_calls: int = Field(default=2, ge=1)
    max_verifier_attempts: int = Field(default=3, ge=1)
    execution_batch_size: int = Field(default=5000, ge=1)
    max_execution_scan_rows: int = Field(default=50000, ge=1)
    max_execution_memory_mb: int = Field(default=256, ge=1)
    execution_timeout_ms: int = Field(default=5000, ge=1)
    null_policy: Literal["error", "zero", "null"] = "error"
    formula_evaluation_mode: Literal["validated_cached_values", "ast_evaluator"] = "ast_evaluator"


class QueryContract(BaseModel):
    """Decomposed query contract linking requirements, snapshot pinning, and execution policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str
    workspace_id: str
    snapshot_id: str
    requirements: tuple[RequirementItem, ...]
    requested_answer_mode: AnswerMode | None = None
    resolved_answer_mode: AnswerMode
    policy_resolution_rationale: str
    budget_policy_id: str = "query-default-v3"

    @field_validator("query_id", "workspace_id", "snapshot_id")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field must be a non empty string")
        return v

    @field_validator("requirements", mode="before")
    @classmethod
    def coerce_requirements(cls, v: Any) -> tuple[RequirementItem, ...]:
        if isinstance(v, (list, tuple)):
            coerced: list[RequirementItem] = []
            for item in v:
                if isinstance(item, RequirementItem):
                    coerced.append(item)
                elif isinstance(item, dict):
                    coerced.append(RequirementItem(**item))
                else:
                    raise ValueError(f"Invalid requirement item: {item}")
            return tuple(coerced)
        return v

    @model_validator(mode="after")
    def validate_contract_invariants(self) -> "QueryContract":
        if len(self.requirements) == 0:
            raise ValueError("QueryContract must contain at least one RequirementItem")

        # AC-11 Requirement Uniqueness
        req_ids = [r.requirement_id for r in self.requirements]
        if len(req_ids) != len(set(req_ids)):
            duplicates = [rid for rid in req_ids if req_ids.count(rid) > 1]
            raise ValueError(
                f"Requirement IDs must be unique within a QueryContract; duplicate found: {set(duplicates)}"
            )

        # AC-11 Answer Mode Resolution Rules
        has_computational = any(
            r.intent in ("lookup", "aggregate", "compare") for r in self.requirements
        )
        has_narrative = any(r.intent == "summarize" for r in self.requirements)

        if has_computational and has_narrative:
            if self.resolved_answer_mode != "hybrid":
                raise ValueError(
                    f"Query mixing computational and narrative requirements must have "
                    f"resolved_answer_mode='hybrid'; received '{self.resolved_answer_mode}'"
                )
        elif has_computational and not has_narrative:
            if self.resolved_answer_mode != "deterministic":
                raise ValueError(
                    f"Pure computational or lookup requirements enforce resolved_answer_mode='deterministic'; "
                    f"received '{self.resolved_answer_mode}'"
                )
        elif has_narrative and not has_computational:
            if self.resolved_answer_mode != "narrative":
                raise ValueError(
                    f"Pure summarization requirements enforce resolved_answer_mode='narrative'; "
                    f"received '{self.resolved_answer_mode}'"
                )

        return self

    @classmethod
    def create_planned(
        cls,
        auth_context: TrustedAuthContext,
        query_id: str,
        snapshot_id: str,
        requirements: tuple[RequirementItem, ...],
        requested_answer_mode: AnswerMode | None = None,
        server_policy: ServerExecutionPolicy | None = None,
    ) -> "QueryContract":
        """Factory method resolving answer mode and applying server execution policy."""
        policy = server_policy or ServerExecutionPolicy()

        has_computational = any(
            r.intent in ("lookup", "aggregate", "compare") for r in requirements
        )
        has_narrative = any(r.intent == "summarize" for r in requirements)

        if has_computational and has_narrative:
            resolved_mode: AnswerMode = "hybrid"
            rationale = (
                "Query contains mixed requirements (exact calculation and narrative synthesis); "
                "enforcing hybrid mode to guarantee exact calculation without narrative override."
            )
        elif has_computational:
            resolved_mode = "deterministic"
            rationale = (
                "Exact metric lookup or calculation requires deterministic execution "
                "without narrative synthesis."
            )
        else:
            resolved_mode = "narrative"
            rationale = "Pure narrative summarization without exact numeric computation requirements."

        return cls(
            query_id=query_id,
            workspace_id=auth_context.authorized_workspace_id,
            snapshot_id=snapshot_id,
            requirements=requirements,
            requested_answer_mode=requested_answer_mode,
            resolved_answer_mode=resolved_mode,
            policy_resolution_rationale=rationale,
            budget_policy_id=policy.policy_id,
        )
