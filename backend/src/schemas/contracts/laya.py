"""Frozen Pydantic contracts for bounded LAYA escalation and targeted query expansion.

Enforces:
- Strict extra field forbidding and frozen immutability across all payloads.
- Discriminator agreement between route and route parameter types.
- Opaque string document versions (max 64 chars) and document IDs (max 128 chars).
- Mandatory routing confidence without certifying evidence sufficiency.
- Bounded payload sizes for requests, parameters, and baseline evidence summaries.
"""

from typing import Annotated, Literal, Union, Any
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MissingRequirementDescription(BaseModel):
    """Structured characterization of missing evidence causing escalation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: str = Field(..., min_length=1, max_length=64)
    missing_dimension: Literal[
        "location",
        "exact_term",
        "semantic_concept",
        "relationship",
        "tabular_operand",
    ]
    description: str = Field(..., min_length=1, max_length=300)
    failed_stage: Literal[
        "run_fidelity_coverage",
        "run_fidelity_empty_context",
        "run_deterministic_math_operands",
    ]
    detected_gap: str = Field(..., min_length=1, max_length=300)


class BaselineEvidenceSummary(BaseModel):
    """Authoritative summary of evidence retrieved during baseline discovery.

    Document identifiers are strings up to 128 characters.
    Document versions are opaque strings up to 64 characters, never integers.
    Coordinates enforce 1 indexed pages and normalized bounding boxes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(..., min_length=1, max_length=64)
    document_id: str = Field(..., min_length=1, max_length=128)
    document_version: str = Field(..., min_length=1, max_length=64)
    workspace_id: str = Field(..., min_length=1, max_length=64, pattern=r"^ws_[a-zA-Z0-9_-]+$")
    snippet: str = Field(..., min_length=1, max_length=300)
    score: float = Field(..., ge=0.0, le=100.0)
    source_kind: Literal["text", "pdf_page", "table_cell", "graph_node"]
    page_number: int | None = Field(default=None, ge=1, le=10000)
    bbox: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def validate_normalized_bbox(self) -> "BaselineEvidenceSummary":
        if self.bbox is not None:
            if len(self.bbox) != 4:
                raise ValueError("bbox must contain exactly 4 coordinates")
            x0, y0, x1, y1 = self.bbox
            for coord in (x0, y0, x1, y1):
                if coord < 0.0 or coord > 1.0:
                    raise ValueError("bbox coordinates must be between 0.0 and 1.0")
            if x0 >= x1 or y0 >= y1:
                raise ValueError("bbox coordinates must satisfy x0 < x1 and y0 < y1")
        return self


class LexicalExpansionParams(BaseModel):
    """Parameters for targeted BM25 keyword expansion."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["lexical_expansion"] = "lexical_expansion"

    query_terms: tuple[str, ...] = Field(..., min_length=1, max_length=5)
    document_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=5)

    @model_validator(mode="after")
    def validate_item_bounds(self) -> "LexicalExpansionParams":
        for term in self.query_terms:
            if not (1 <= len(term) <= 64):
                raise ValueError("query_terms items must be between 1 and 64 characters")
        for doc_id in self.document_ids:
            if not (1 <= len(doc_id) <= 128):
                raise ValueError("document_ids items must be between 1 and 128 characters")
        return self


class DenseExpansionParams(BaseModel):
    """Parameters for targeted vector similarity expansion."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["dense_expansion"] = "dense_expansion"

    concept_query: str = Field(..., min_length=3, max_length=200)
    document_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=5)

    @model_validator(mode="after")
    def validate_item_bounds(self) -> "DenseExpansionParams":
        for doc_id in self.document_ids:
            if not (1 <= len(doc_id) <= 128):
                raise ValueError("document_ids items must be between 1 and 128 characters")
        return self


class StructuralExpansionParams(BaseModel):
    """Parameters for targeted PageIndex section or page navigation."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["structural_expansion"] = "structural_expansion"

    document_id: str = Field(..., min_length=1, max_length=128)
    section_heading: str | None = Field(default=None, max_length=128)
    target_pages: tuple[int, ...] = Field(default_factory=tuple, max_length=5)

    @model_validator(mode="after")
    def validate_page_bounds(self) -> "StructuralExpansionParams":
        for page in self.target_pages:
            if not (1 <= page <= 10000):
                raise ValueError("target_pages items must be between 1 and 10000")
        return self


class GraphExpansionParams(BaseModel):
    """Parameters for bounded knowledge graph multi hop traversal."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["graph_expansion"] = "graph_expansion"

    seed_entity_id: str = Field(..., min_length=1, max_length=64)
    relation_types: tuple[str, ...] = Field(default_factory=tuple, max_length=5)
    max_hops: int = Field(default=1, ge=1, le=2)

    @model_validator(mode="after")
    def validate_item_bounds(self) -> "GraphExpansionParams":
        for rel in self.relation_types:
            if not (1 <= len(rel) <= 64):
                raise ValueError("relation_types items must be between 1 and 64 characters")
        return self


class StructuredExpansionParams(BaseModel):
    """Parameters for TableStore column or row operand lookups."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["structured_expansion"] = "structured_expansion"

    table_name: str = Field(..., min_length=1, max_length=128)
    required_columns: tuple[str, ...] = Field(..., min_length=1, max_length=10)
    filter_predicates: tuple[str, ...] = Field(default_factory=tuple, max_length=5)

    @model_validator(mode="after")
    def validate_item_bounds(self) -> "StructuredExpansionParams":
        for col in self.required_columns:
            if not (1 <= len(col) <= 64):
                raise ValueError("required_columns items must be between 1 and 64 characters")
        for pred in self.filter_predicates:
            if not (1 <= len(pred) <= 128):
                raise ValueError("filter_predicates items must be between 1 and 128 characters")
        return self


class NoneExpansionParams(BaseModel):
    """Parameters when LAYA decides no expansion should occur."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["none"] = "none"

    reason: str = Field(default="", max_length=200)


LayaRouteParameters = Annotated[
    Union[
        LexicalExpansionParams,
        DenseExpansionParams,
        StructuralExpansionParams,
        GraphExpansionParams,
        StructuredExpansionParams,
        NoneExpansionParams,
    ],
    Field(discriminator="route_type"),
]


class LayaEscalationRequest(BaseModel):
    """Input payload provided to the LAYA decision model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str = Field(..., min_length=1, max_length=64)
    workspace_id: str = Field(..., min_length=1, max_length=64, pattern=r"^ws_[a-zA-Z0-9_-]+$")
    query: str = Field(..., min_length=1, max_length=1000)
    missing_requirement: MissingRequirementDescription
    available_routes: tuple[str, ...] = Field(default_factory=tuple, max_length=6)
    retrieved_summary: tuple[BaselineEvidenceSummary, ...] = Field(default_factory=tuple, max_length=10)
    caller_deadline_ms: float = Field(default=2500.0, ge=0.0)


class LayaEscalationDecision(BaseModel):
    """Typed decision output produced by LAYA and validated by code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(..., min_length=1, max_length=64)
    route: Literal[
        "lexical_expansion",
        "dense_expansion",
        "structural_expansion",
        "graph_expansion",
        "structured_expansion",
        "none",
    ]
    parameters: LayaRouteParameters
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=200)

    @field_validator("confidence", mode="before")
    @classmethod
    def validate_confidence_not_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("confidence must not be a boolean")
        return v
    execution_status: Literal[
        "proposed",
        "approved",
        "rejected_by_code",
        "budget_exhausted",
        "timeout",
        "storage_failed",
    ] = "proposed"

    @model_validator(mode="after")
    def validate_route_parameter_agreement(self) -> "LayaEscalationDecision":
        """Enforce strict agreement between route discriminator and parameters.route_type."""
        if self.route != self.parameters.route_type:
            raise ValueError(
                f"Route discriminator mismatch: route '{self.route}' does not match "
                f"parameters.route_type '{self.parameters.route_type}'"
            )
        return self
