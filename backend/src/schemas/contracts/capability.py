"""Capability artifact, machine readable coverage ranges, and lifecycle state transitions.

Enforces rebuild safe identifiers, partition completeness proofs, unknown total rules,
scoped readiness, and validated state transitions.
"""

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.schemas.contracts.envelope import TrustedAuthContext

CapabilityType = Literal[
    "text_search",
    "vector_search",
    "table_execution",
    "page_index",
    "knowledge_graph",
]

CoverageUnit = Literal["page", "chunk", "sheet", "row", "slide", "section"]


class CoverageRange(BaseModel):
    """Closed inclusive range [start_index, end_index] covering units in a partition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_type: CoverageUnit
    start_index: int = Field(..., ge=0)
    end_index: int = Field(..., ge=0)
    partition_id: str | None = None

    @model_validator(mode="after")
    def validate_range_bounds(self) -> "CoverageRange":
        if self.start_index > self.end_index:
            raise ValueError(
                f"start_index ({self.start_index}) must be <= end_index ({self.end_index})"
            )

        # 1-indexed for page, slide, sheet
        if self.unit_type in ("page", "slide", "sheet") and self.start_index < 1:
            raise ValueError(
                f"unit_type '{self.unit_type}' requires 1-indexed start_index (>= 1); received {self.start_index}"
            )

        return self

    @property
    def unit_count(self) -> int:
        """Count of closed units covered by this range."""
        return self.end_index - self.start_index + 1


class ArtifactCoverage(BaseModel):
    """Machine readable capability index coverage across partitions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_type: CoverageUnit
    covered_ranges: tuple[CoverageRange, ...] = Field(default_factory=tuple)
    exclusion_ranges: tuple[CoverageRange, ...] = Field(default_factory=tuple)
    processed_count: int = Field(default=0, ge=0)
    total_units: int | None = Field(default=None, ge=0)
    is_complete: bool = False
    ready_partitions: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("covered_ranges", "exclusion_ranges", mode="before")
    @classmethod
    def coerce_ranges(cls, v: Any) -> tuple[CoverageRange, ...]:
        if isinstance(v, (list, tuple)):
            coerced: list[CoverageRange] = []
            for item in v:
                if isinstance(item, CoverageRange):
                    coerced.append(item)
                elif isinstance(item, dict):
                    coerced.append(CoverageRange(**item))
                else:
                    raise ValueError(f"Invalid coverage range item: {item}")
            return tuple(coerced)
        return v

    @field_validator("ready_partitions", mode="before")
    @classmethod
    def coerce_partitions(cls, v: Any) -> tuple[str, ...]:
        if isinstance(v, (list, tuple)):
            return tuple(str(x) for x in v)
        return v

    @model_validator(mode="after")
    def validate_coverage_consistency(self) -> "ArtifactCoverage":
        # Group covered ranges by partition
        by_partition: dict[str | None, list[CoverageRange]] = {}
        for r in self.covered_ranges:
            if r.unit_type != self.unit_type:
                raise ValueError(
                    f"Range unit_type '{r.unit_type}' does not match coverage unit_type '{self.unit_type}'"
                )
            by_partition.setdefault(r.partition_id, []).append(r)

        # Validate sorted non overlapping per partition
        for part, ranges in by_partition.items():
            for i in range(len(ranges) - 1):
                cur = ranges[i]
                nxt = ranges[i + 1]
                if cur.end_index >= nxt.start_index:
                    raise ValueError(
                        f"Overlapping or unsorted ranges in partition '{part}': "
                        f"[{cur.start_index}, {cur.end_index}] and [{nxt.start_index}, {nxt.end_index}]"
                    )

        # Validate range arithmetic: sum of covered units equals processed_count
        computed_units = sum(r.unit_count for r in self.covered_ranges)
        if computed_units != self.processed_count:
            raise ValueError(
                f"Range sum count ({computed_units}) does not match processed_count ({self.processed_count})"
            )

        if self.total_units is not None and self.processed_count > self.total_units:
            raise ValueError(
                f"processed_count ({self.processed_count}) exceeds total_units ({self.total_units})"
            )

        # Completeness proof rules
        if self.is_complete:
            if self.total_units is None:
                raise ValueError("is_complete cannot be True when total_units is unknown (None)")

            if self.processed_count != self.total_units:
                raise ValueError(
                    f"is_complete is True but processed_count ({self.processed_count}) "
                    f"!= total_units ({self.total_units})"
                )

            # Check for gaps across partition coverage
            for part, ranges in by_partition.items():
                if not ranges:
                    continue
                expected_start = 1 if self.unit_type in ("page", "slide", "sheet") else 0
                if ranges[0].start_index != expected_start:
                    raise ValueError(
                        f"Completeness proof failed for partition '{part}': "
                        f"first range starts at {ranges[0].start_index}, expected {expected_start}"
                    )
                for i in range(len(ranges) - 1):
                    if ranges[i].end_index + 1 != ranges[i + 1].start_index:
                        raise ValueError(
                            f"Completeness proof failed: gap between {ranges[i].end_index} "
                            f"and {ranges[i+1].start_index} in partition '{part}'"
                        )
                # If single partition, end must reach total_units
                if len(by_partition) == 1:
                    expected_end = (
                        self.total_units if expected_start == 1 else self.total_units - 1
                    )
                    if ranges[-1].end_index != expected_end:
                        raise ValueError(
                            f"Completeness proof failed: last range ends at {ranges[-1].end_index}, "
                            f"expected {expected_end}"
                        )

        return self


class ArtifactFailureState(BaseModel):
    """Failure details for a capability build or index task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    error_code: str
    message: str = Field(..., max_length=2048)
    timestamp: str
    retryable: bool
    details: tuple[tuple[str, str], ...] = Field(default_factory=tuple)

    @field_validator("details", mode="before")
    @classmethod
    def coerce_details(cls, v: Any) -> tuple[tuple[str, str], ...]:
        if isinstance(v, dict):
            return tuple((str(k), str(val)) for k, val in v.items())
        if isinstance(v, (list, tuple)):
            return tuple((str(x[0]), str(x[1])) for x in v)
        return v


class CapabilityArtifact(BaseModel):
    """Capability index manifest artifact with scoped readiness and validated transitions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str
    source_id: str
    source_version: int = Field(..., ge=1)
    artifact_id: str
    capability: CapabilityType
    ready: bool
    coverage: ArtifactCoverage
    artifact_version: str
    pipeline_version: str
    build_id: str
    failure_state: ArtifactFailureState | None = None

    @field_validator(
        "workspace_id",
        "source_id",
        "artifact_id",
        "artifact_version",
        "pipeline_version",
        "build_id",
    )
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Identifier must be a non empty string")
        return v

    @model_validator(mode="after")
    def validate_state_invariants(self) -> "CapabilityArtifact":
        if self.ready:
            if self.failure_state is not None:
                raise ValueError("CapabilityArtifact cannot be ready while retaining a failure_state")
            if not self.coverage.is_complete:
                raise ValueError("CapabilityArtifact cannot be ready when coverage.is_complete is False")
        if self.failure_state is not None and self.ready:
            raise ValueError("CapabilityArtifact cannot have failure_state and ready=True")
        return self

    @classmethod
    def create_initial(
        cls,
        auth_context: TrustedAuthContext,
        source_id: str,
        source_version: int,
        capability: CapabilityType,
        artifact_version: str,
        pipeline_version: str,
        build_id: str,
        initial_coverage: ArtifactCoverage,
    ) -> "CapabilityArtifact":
        """Factory method constructing initial unready artifact with rebuild safe identifier."""
        build_prefix = build_id[:8]
        artifact_id = (
            f"cap_{capability}_{source_id}_v{source_version}_{artifact_version}_{build_prefix}"
        )
        return cls(
            workspace_id=auth_context.authorized_workspace_id,
            source_id=source_id,
            source_version=source_version,
            artifact_id=artifact_id,
            capability=capability,
            ready=False,
            coverage=initial_coverage,
            artifact_version=artifact_version,
            pipeline_version=pipeline_version,
            build_id=build_id,
            failure_state=None,
        )

    def transition_coverage(self, updated_coverage: ArtifactCoverage) -> "CapabilityArtifact":
        """Transition progress coverage, revalidating all invariants."""
        return CapabilityArtifact(
            workspace_id=self.workspace_id,
            source_id=self.source_id,
            source_version=self.source_version,
            artifact_id=self.artifact_id,
            capability=self.capability,
            ready=False,
            coverage=updated_coverage,
            artifact_version=self.artifact_version,
            pipeline_version=self.pipeline_version,
            build_id=self.build_id,
            failure_state=self.failure_state,
        )

    def transition_scoped_readiness(
        self, ready_partitions: tuple[str, ...]
    ) -> "CapabilityArtifact":
        """Transition scoped readiness partitions for multi partition capability routing."""
        new_cov = ArtifactCoverage(
            unit_type=self.coverage.unit_type,
            covered_ranges=self.coverage.covered_ranges,
            exclusion_ranges=self.coverage.exclusion_ranges,
            processed_count=self.coverage.processed_count,
            total_units=self.coverage.total_units,
            is_complete=self.coverage.is_complete,
            ready_partitions=ready_partitions,
        )
        return CapabilityArtifact(
            workspace_id=self.workspace_id,
            source_id=self.source_id,
            source_version=self.source_version,
            artifact_id=self.artifact_id,
            capability=self.capability,
            ready=self.ready,
            coverage=new_cov,
            artifact_version=self.artifact_version,
            pipeline_version=self.pipeline_version,
            build_id=self.build_id,
            failure_state=self.failure_state,
        )

    def transition_to_ready(self, complete_coverage: ArtifactCoverage) -> "CapabilityArtifact":
        """Transition artifact to ready state, enforcing completeness proof."""
        if not complete_coverage.is_complete:
            raise ValueError("Cannot transition to ready without complete coverage (is_complete=True)")
        return CapabilityArtifact(
            workspace_id=self.workspace_id,
            source_id=self.source_id,
            source_version=self.source_version,
            artifact_id=self.artifact_id,
            capability=self.capability,
            ready=True,
            coverage=complete_coverage,
            artifact_version=self.artifact_version,
            pipeline_version=self.pipeline_version,
            build_id=self.build_id,
            failure_state=None,
        )

    def transition_to_failed(
        self, failure_state: ArtifactFailureState
    ) -> "CapabilityArtifact":
        """Transition artifact to failed state, clearing ready and recording error."""
        return CapabilityArtifact(
            workspace_id=self.workspace_id,
            source_id=self.source_id,
            source_version=self.source_version,
            artifact_id=self.artifact_id,
            capability=self.capability,
            ready=False,
            coverage=self.coverage,
            artifact_version=self.artifact_version,
            pipeline_version=self.pipeline_version,
            build_id=self.build_id,
            failure_state=failure_state,
        )
