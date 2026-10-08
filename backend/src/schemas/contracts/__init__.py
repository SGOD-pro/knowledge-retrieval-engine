"""Shared contracts and identity envelopes for the Knowledge Retrieval Engine.

Export all deeply immutable, strictly typed Pydantic V2 models across
identity envelopes, locators, payloads, evidence, capability artifacts,
query contracts, structured execution outcomes, and verification records.
"""

from src.schemas.contracts.capability import (
    ArtifactCoverage,
    ArtifactFailureState,
    CapabilityArtifact,
    CapabilityType,
    CoverageRange,
    CoverageUnit,
)
from src.schemas.contracts.envelope import (
    IdentityEnvelope,
    TrustedAuthContext,
)
from src.schemas.contracts.boundary import (
    CASConflictError,
    CacheKeyDescriptor,
    CachedQueryResult,
    CitationItem,
    ErrorEnvelope,
    JobRecord,
    PrincipalRecord,
    PublicationManifestRecord,
    QdrantFilterContract,
    QueryExecutionOutcome,
    SnapshotRecord,
    SourceItem,
    TombstoneRecord,
    WorkerExecutionContext,
    WorkspaceAccessDeniedError,
    WorkspaceRecord,
)
from src.schemas.contracts.evidence import (
    CanonicalEvidence,
    EvidenceProvenance,
    EvidenceType,
    ExtractionQuality,
    QualityStatus,
)
from src.schemas.contracts.execution import (
    ExecutionCoverageStatus,
    StructuredExecutionResult,
)
from src.schemas.contracts.location import (
    DocumentLocation,
    Location,
    SectionLocation,
    SlideLocation,
    TableLocation,
)
from src.schemas.contracts.payload import (
    AssetPayload,
    CellPayload,
    ContentPayload,
    DatasetReference,
    DatasetRefPayload,
    ExecutableFormulaAST,
    FormulaEvalStatus,
    FormulaPayload,
    OrderedScalar,
    QualifierItem,
    ScalarPrimitive,
    ScalarValue,
    TablePayload,
    TextPayload,
)
from src.schemas.contracts.query import (
    AnswerMode,
    EvidenceRequirement,
    OperationType,
    QueryContract,
    RequirementIntent,
    RequirementItem,
    RequirementTarget,
    SelectionOperator,
    SelectionPredicate,
    ServerExecutionPolicy,
)
from src.schemas.contracts.verification import (
    SupportStatus,
    VerificationRecord,
    VerifierType,
)

__all__ = [
    # Envelope
    "IdentityEnvelope",
    "TrustedAuthContext",
    # Location
    "Location",
    "DocumentLocation",
    "TableLocation",
    "SlideLocation",
    "SectionLocation",
    # Payload
    "ScalarPrimitive",
    "OrderedScalar",
    "ScalarValue",
    "FormulaEvalStatus",
    "QualifierItem",
    "DatasetReference",
    "TextPayload",
    "CellPayload",
    "TablePayload",
    "DatasetRefPayload",
    "AssetPayload",
    "FormulaPayload",
    "ContentPayload",
    "ExecutableFormulaAST",
    # Evidence
    "EvidenceType",
    "QualityStatus",
    "ExtractionQuality",
    "EvidenceProvenance",
    "CanonicalEvidence",
    # Execution
    "ExecutionCoverageStatus",
    "StructuredExecutionResult",
    # Verification
    "SupportStatus",
    "VerifierType",
    "VerificationRecord",
    # Capability
    "CapabilityType",
    "CoverageUnit",
    "CoverageRange",
    "ArtifactCoverage",
    "ArtifactFailureState",
    "CapabilityArtifact",
    # Query
    "SelectionOperator",
    "SelectionPredicate",
    "RequirementIntent",
    "OperationType",
    "AnswerMode",
    "RequirementTarget",
    "EvidenceRequirement",
    "RequirementItem",
    "ServerExecutionPolicy",
    "QueryContract",
    # Boundary and Workspace Isolation
    "WorkspaceAccessDeniedError",
    "CASConflictError",
    "WorkspaceRecord",
    "PrincipalRecord",
    "SnapshotRecord",
    "JobRecord",
    "WorkerExecutionContext",
    "TombstoneRecord",
    "SourceItem",
    "CitationItem",
    "CachedQueryResult",
    "CacheKeyDescriptor",
    "QdrantFilterContract",
    "PublicationManifestRecord",
    "QueryExecutionOutcome",
    "ErrorEnvelope",
]
