"""Verification record contract pinned to query snapshots.

Enforces storage identity embedding verifier type, version, and attempt number,
while keeping source evidence pure and unmodified.
"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.schemas.contracts.envelope import TrustedAuthContext

SupportStatus = Literal[
    "ASSERTED",
    "HYPOTHESIZED",
    "CONTRADICTED",
    "UNVERIFIED",
]

VerifierType = Literal["mechanical", "encoder", "generative"]


class VerificationRecord(BaseModel):
    """Snapshot pinned semantic verification record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verification_id: str
    workspace_id: str
    query_id: str
    snapshot_id: str
    requirement_id: str
    evidence_id: str
    verifier_version: str
    verifier_type: VerifierType
    attempt_number: int = Field(..., ge=1, le=3, description="1 indexed attempt counter, capped at 3")
    execution_time_ms: int = Field(..., ge=0)
    support_status: SupportStatus
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    verification_rationale: str = Field(..., max_length=4096)
    verified_at: str

    @field_validator(
        "verification_id",
        "workspace_id",
        "query_id",
        "snapshot_id",
        "requirement_id",
        "evidence_id",
        "verifier_version",
        "verification_rationale",
        "verified_at",
    )
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field must be a non empty string")
        return v

    @classmethod
    def create_verified(
        cls,
        auth_context: TrustedAuthContext,
        query_id: str,
        snapshot_id: str,
        requirement_id: str,
        evidence_id: str,
        verifier_version: str,
        verifier_type: VerifierType,
        attempt_number: int,
        execution_time_ms: int,
        support_status: SupportStatus,
        confidence_score: float,
        rationale: str,
        verified_at: str,
    ) -> "VerificationRecord":
        """Factory method constructing VerificationRecord with deterministic storage identity."""
        clean_evidence_id = evidence_id.replace(":", "_")
        verification_id = (
            f"vr_{query_id}_{snapshot_id}_{requirement_id}_{clean_evidence_id}_"
            f"{verifier_type}_{verifier_version}_att{attempt_number}"
        )
        return cls(
            verification_id=verification_id,
            workspace_id=auth_context.authorized_workspace_id,
            query_id=query_id,
            snapshot_id=snapshot_id,
            requirement_id=requirement_id,
            evidence_id=evidence_id,
            verifier_version=verifier_version,
            verifier_type=verifier_type,
            attempt_number=attempt_number,
            execution_time_ms=execution_time_ms,
            support_status=support_status,
            confidence_score=confidence_score,
            verification_rationale=rationale,
            verified_at=verified_at,
        )
