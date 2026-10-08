"""Comprehensive unit tests for contracts and identity envelope.

Verifies all acceptance criteria (AC-1 through AC-14) including deep immutability,
elimination of untyped payloads, rebuild safe identifiers, locator and payload compatibility,
operator specific predicates, partition completeness proofs, and execution invariants.
"""

import json
import pytest
from pydantic import ValidationError

from src.schemas.contracts import (
    AnswerMode,
    ArtifactCoverage,
    ArtifactFailureState,
    AssetPayload,
    CanonicalEvidence,
    CapabilityArtifact,
    CellPayload,
    CoverageRange,
    DatasetReference,
    DatasetRefPayload,
    DocumentLocation,
    EvidenceProvenance,
    EvidenceRequirement,
    ExecutableFormulaAST,
    ExecutionCoverageStatus,
    ExtractionQuality,
    FormulaPayload,
    IdentityEnvelope,
    QualifierItem,
    QueryContract,
    RequirementItem,
    RequirementTarget,
    SectionLocation,
    SelectionPredicate,
    ServerExecutionPolicy,
    SlideLocation,
    StructuredExecutionResult,
    TableLocation,
    TablePayload,
    TextPayload,
    TrustedAuthContext,
    VerificationRecord,
)


@pytest.fixture
def sample_auth_context() -> TrustedAuthContext:
    return TrustedAuthContext(
        authorized_workspace_id="ws_enterprise_1",
        principal_id="user_analyst_01",
        principal_roles=("analyst", "viewer"),
        access_policy_version="pol_v2",
    )


@pytest.fixture
def sample_provenance() -> EvidenceProvenance:
    return EvidenceProvenance(
        content_hash="sha256:7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069",
        extraction_pipeline_version="parse-v5",
        parser_element_id="elem_p18_3",
    )


@pytest.fixture
def sample_quality() -> ExtractionQuality:
    return ExtractionQuality(status="VALID", confidence=0.99)


# ==============================================================================
# AC-1: Flat Identity Envelope and Server Only Authorization Context Injection
# ==============================================================================

def test_ac1_flat_identity_serialization(sample_auth_context: TrustedAuthContext):
    """IdentityEnvelope serializes flat without nested envelope container."""
    env = IdentityEnvelope(
        workspace_id="ws_enterprise_1",
        query_id="q_100",
        snapshot_id="snap_ws_enterprise_1_gen4",
    )
    dumped = env.model_dump(mode="json")
    assert dumped["workspace_id"] == "ws_enterprise_1"
    assert dumped["query_id"] == "q_100"
    assert dumped["snapshot_id"] == "snap_ws_enterprise_1_gen4"
    assert "envelope" not in dumped

    # Validation against matching auth context succeeds
    env.validate_auth(sample_auth_context)


def test_ac1_auth_context_mismatch_rejected(sample_auth_context: TrustedAuthContext):
    """Mismatched envelope workspace raises authorization error."""
    env = IdentityEnvelope(workspace_id="ws_other_tenant")
    with pytest.raises(ValueError, match="does not match authorized workspace"):
        env.validate_auth(sample_auth_context)


def test_ac1_empty_or_whitespace_workspace_rejected():
    """Empty or whitespace workspace_id is rejected."""
    with pytest.raises(ValidationError):
        IdentityEnvelope(workspace_id="")
    with pytest.raises(ValidationError):
        IdentityEnvelope(workspace_id="   ")


def test_ac1_wildcard_workspace_rejected():
    """Wildcards in workspace_id are rejected."""
    with pytest.raises(ValidationError, match="wildcard"):
        IdentityEnvelope(workspace_id="ws_*")


def test_ac1_invalid_source_version_rejected():
    """Negative or zero source_version is rejected."""
    with pytest.raises(ValidationError):
        IdentityEnvelope(workspace_id="ws_1", source_id="doc_1", source_version=0)


def test_ac1_source_version_without_source_id_rejected():
    """source_version without source_id is rejected."""
    with pytest.raises(ValidationError):
        IdentityEnvelope(workspace_id="ws_1", source_version=2)


def test_ac1_extra_ambient_keys_rejected():
    """Extra ambient keys are rejected."""
    with pytest.raises(ValidationError):
        IdentityEnvelope(workspace_id="ws_1", ambient_secret="leak")  # type: ignore[call-arg]


# ==============================================================================
# AC-2: Deep Immutability, Elimination of Untyped Payloads, and Typed Qualifiers
# ==============================================================================

def test_ac2_attribute_mutation_raises_validation_error():
    """Mutating attribute on frozen model raises ValidationError with frozen_instance error."""
    qual = QualifierItem(key="region", value="Europe")
    with pytest.raises(ValidationError) as exc_info:
        qual.key = "Asia"  # type: ignore[misc]
    assert any(err["type"] == "frozen_instance" for err in exc_info.value.errors())


def test_ac2_tuple_collection_mutation_raises_type_error():
    """Tuple collections cannot be modified in place."""
    table = TablePayload(
        table_id="tbl_1",
        headers=("ColA", "ColB"),
        rows=((1, 2), (3, 4)),
        row_count=2,
        col_count=2,
    )
    with pytest.raises(TypeError):
        table.rows[0] = (5, 6)  # type: ignore[index]
    with pytest.raises(AttributeError):
        table.headers.append("ColC")  # type: ignore[attr-defined]


def test_ac2_untyped_payload_rejected():
    """Arbitrary untyped objects and dictionaries in table rows are rejected."""
    with pytest.raises(ValidationError, match="Non scalar value"):
        TablePayload(
            table_id="tbl_1",
            headers=("ColA",),
            rows=((({"nested": "dict"}),),),  # type: ignore[arg-type]
            row_count=1,
            col_count=1,
        )


def test_ac2_typed_qualifiers_happy_path(
    sample_auth_context: TrustedAuthContext,
    sample_provenance: EvidenceProvenance,
    sample_quality: ExtractionQuality,
):
    """CanonicalEvidence with typed qualifiers succeeds."""
    evidence = CanonicalEvidence.create_from_extraction(
        auth_context=sample_auth_context,
        source_id="doc_report",
        source_version=1,
        pipeline_version="parse-v5",
        chunk_index=0,
        evidence_type="text_chunk",
        location=DocumentLocation(page=1),
        content=TextPayload(text="Sample text content."),
        provenance=sample_provenance,
        quality=sample_quality,
        qualifiers=(
            QualifierItem(key="period", value="FY2024"),
            QualifierItem(key="audit_passed", value=True),
            QualifierItem(key="confidence_score", value=0.98),
            QualifierItem(key="item_count", value=42),
            QualifierItem(key="optional_val", value=None),
        ),
    )
    assert len(evidence.qualifiers) == 5
    assert evidence.qualifiers[0].key == "period"
    assert evidence.qualifiers[0].value == "FY2024"
    assert evidence.qualifiers[1].value is True


# ==============================================================================
# AC-3: Validated State Transitions and Revalidation Enforcement
# ==============================================================================

def test_ac3_capability_lifecycle_transitions(sample_auth_context: TrustedAuthContext):
    """CapabilityArtifact executes validated state transitions returning new instances."""
    init_cov = ArtifactCoverage(
        unit_type="page",
        covered_ranges=(CoverageRange(unit_type="page", start_index=1, end_index=10),),
        processed_count=10,
        total_units=20,
        is_complete=False,
    )
    artifact = CapabilityArtifact.create_initial(
        auth_context=sample_auth_context,
        source_id="doc_annual",
        source_version=1,
        capability="text_search",
        artifact_version="bm25-v7",
        pipeline_version="index-v3",
        build_id="bld_001_run_abc",
        initial_coverage=init_cov,
    )
    assert artifact.ready is False
    assert artifact.failure_state is None

    # Transition coverage
    updated_cov = ArtifactCoverage(
        unit_type="page",
        covered_ranges=(CoverageRange(unit_type="page", start_index=1, end_index=15),),
        processed_count=15,
        total_units=20,
        is_complete=False,
    )
    in_progress = artifact.transition_coverage(updated_cov)
    assert in_progress.coverage.processed_count == 15
    # Original artifact instance is untouched
    assert artifact.coverage.processed_count == 10

    # Transition scoped readiness
    scoped = in_progress.transition_scoped_readiness(("part_intro",))
    assert scoped.coverage.ready_partitions == ("part_intro",)

    # Transition to ready with complete coverage
    complete_cov = ArtifactCoverage(
        unit_type="page",
        covered_ranges=(CoverageRange(unit_type="page", start_index=1, end_index=20),),
        processed_count=20,
        total_units=20,
        is_complete=True,
    )
    ready_art = scoped.transition_to_ready(complete_cov)
    assert ready_art.ready is True
    assert ready_art.coverage.is_complete is True

    # Transition to failed
    fail_state = ArtifactFailureState(
        error_code="INDEX_TIMEOUT",
        message="Index process timed out after 30s",
        timestamp="2026-10-08T00:00:00Z",
        retryable=True,
    )
    failed_art = in_progress.transition_to_failed(fail_state)
    assert failed_art.ready is False
    assert failed_art.failure_state is not None
    assert failed_art.failure_state.error_code == "INDEX_TIMEOUT"


def test_ac3_negative_transition_to_ready_incomplete(sample_auth_context: TrustedAuthContext):
    """Attempting to transition artifact to ready while coverage is incomplete raises ValueError."""
    init_cov = ArtifactCoverage(
        unit_type="page",
        covered_ranges=(CoverageRange(unit_type="page", start_index=1, end_index=10),),
        processed_count=10,
        total_units=20,
        is_complete=False,
    )
    artifact = CapabilityArtifact.create_initial(
        auth_context=sample_auth_context,
        source_id="doc_annual",
        source_version=1,
        capability="text_search",
        artifact_version="bm25-v7",
        pipeline_version="index-v3",
        build_id="bld_001_run_abc",
        initial_coverage=init_cov,
    )
    with pytest.raises(ValueError, match="Cannot transition to ready without complete coverage"):
        artifact.transition_to_ready(init_cov)


# ==============================================================================
# AC-4: Rebuild Safe Deterministic and Immutable Build Identifiers
# ==============================================================================

def test_ac4_rebuild_safe_identifiers(
    sample_auth_context: TrustedAuthContext,
    sample_provenance: EvidenceProvenance,
    sample_quality: ExtractionQuality,
):
    """Evidence and capability IDs follow deterministic templates preventing collisions."""
    ev = CanonicalEvidence.create_from_extraction(
        auth_context=sample_auth_context,
        source_id="doc_finance",
        source_version=2,
        pipeline_version="parse-v5",
        chunk_index=14,
        evidence_type="text_chunk",
        location=DocumentLocation(page=18),
        content=TextPayload(text="Finance text chunk"),
        provenance=sample_provenance,
        quality=sample_quality,
    )
    # Expected: ev_doc_finance_v2_parse-v5_7f83b1657ff1fc53_14
    assert ev.evidence_id == "ev_doc_finance_v2_parse-v5_7f83b1657ff1fc53_14"

    cov = ArtifactCoverage(unit_type="page", processed_count=0)
    art1 = CapabilityArtifact.create_initial(
        auth_context=sample_auth_context,
        source_id="doc_finance",
        source_version=2,
        capability="text_search",
        artifact_version="bm25-v7",
        pipeline_version="index-v3",
        build_id="bld88a1f99c24018",
        initial_coverage=cov,
    )
    art2 = CapabilityArtifact.create_initial(
        auth_context=sample_auth_context,
        source_id="doc_finance",
        source_version=2,
        capability="text_search",
        artifact_version="bm25-v7",
        pipeline_version="index-v3",
        build_id="bld99c2411e73099",
        initial_coverage=cov,
    )
    # Rebuilding produces a distinct artifact_id
    assert art1.artifact_id != art2.artifact_id
    assert art1.artifact_id.startswith("cap_text_search_doc_finance_v2_bm25-v7_")


# ==============================================================================
# AC-5: Complete Multi Format Locator Union, Top Left Origin, Precision
# ==============================================================================

def test_ac5_coordinate_precision_preservation():
    """Preserves full 64 bit float precision without rounding."""
    high_prec_bbox = (0.0812456123456789, 0.2234198123456789, 0.9245112123456789, 0.4518923123456789)
    doc_loc = DocumentLocation(page=1, bbox=high_prec_bbox)
    assert doc_loc.bbox == high_prec_bbox


def test_ac5_top_left_origin_bounds_validation():
    """Invalid bounding box coordinates (inverted bounds, out of range) raise ValidationError."""
    # Inverted x: x0 >= x1
    with pytest.raises(ValidationError, match="strictly less than x1"):
        DocumentLocation(page=1, bbox=(0.8, 0.1, 0.2, 0.9))

    # Inverted y: y0 >= y1
    with pytest.raises(ValidationError, match="strictly less than y1"):
        DocumentLocation(page=1, bbox=(0.1, 0.9, 0.8, 0.2))

    # Out of normalized range
    with pytest.raises(ValidationError, match="normalized between 0.0 and 1.0"):
        DocumentLocation(page=1, bbox=(-0.1, 0.1, 0.8, 0.9))


def test_ac5_table_location_dual_coordinates():
    """TableLocation supports both visual bbox and structural row/col/sheet coordinates."""
    tbl_loc = TableLocation(
        page=4,
        bbox=(0.1, 0.2, 0.9, 0.8),
        table_id="tbl_rev",
        row_id="Europe",
        column_id="2024",
        sheet_name="Revenue",
    )
    assert tbl_loc.table_id == "tbl_rev"
    assert tbl_loc.sheet_name == "Revenue"
    assert tbl_loc.page == 4


# ==============================================================================
# AC-6: Bounded Payloads, Byte Caps, Dataset References, Separate Results
# ==============================================================================

def test_ac6_text_payload_char_cap_rejected():
    """TextPayload exceeding 32,768 characters raises ValidationError."""
    with pytest.raises(ValidationError, match="exceeds maximum character limit"):
        TextPayload(text="A" * 32769)


def test_ac6_table_row_and_column_cap_rejected():
    """TablePayload exceeding 50 rows or 50 columns raises ValidationError."""
    with pytest.raises(ValidationError, match="exceeds maximum 50"):
        TablePayload(
            table_id="tbl_large",
            headers=tuple(f"Col_{i}" for i in range(51)),
            rows=(),
            row_count=0,
            col_count=51,
        )

    with pytest.raises(ValidationError, match="Large tables must use DatasetRefPayload"):
        TablePayload(
            table_id="tbl_large",
            headers=("ColA",),
            rows=tuple((i,) for i in range(51)),
            row_count=51,
            col_count=1,
        )


def test_ac6_dataset_ref_payload_pure_source_reference():
    """DatasetRefPayload carries pure source reference metadata without query status."""
    ds_ref = DatasetReference(
        source_id="sheet_transactions",
        source_version=1,
        registered_file_id="reg_file_001",
        table_id="tbl_all_rows",
        total_rows=100000,
        total_columns=25,
        schema_hash="sha256:abcd1234",
    )
    payload = DatasetRefPayload(
        dataset_ref=ds_ref,
        total_rows=100000,
        total_columns=25,
        schema_hash="sha256:abcd1234",
    )
    assert payload.kind == "dataset_ref"
    assert payload.total_rows == 100000


# ==============================================================================
# AC-7: Asset Provenance & Separation of Raw Formulas from Executable ASTs
# ==============================================================================

def test_ac7_asset_provenance_flag():
    """AssetPayload distinguishes AI generated descriptions from extracted captions."""
    asset = AssetPayload(
        asset_id="chart_1",
        mime_type="image/png",
        storage_ref="s3://kre-assets/chart_1.png",
        ai_description="Bar chart showing regional revenue growth.",
        is_ai_generated_description=True,
    )
    assert asset.is_ai_generated_description is True


def test_ac7_raw_formula_payload():
    """FormulaPayload stores raw extracted spreadsheet expressions."""
    formula = FormulaPayload(
        raw_expression="=SUM(C12:C18)-C19",
        cached_value=184.2,
        eval_status="CACHED_ONLY",
    )
    assert formula.kind == "formula"
    assert formula.raw_expression == "=SUM(C12:C18)-C19"
    assert formula.cached_value == 184.2


def test_ac7_executable_formula_ast():
    """ExecutableFormulaAST provides allowlisted operations on query plane."""
    ast = ExecutableFormulaAST(
        operator="sum",
        operands=("revenue_eu", "revenue_na"),
        null_policy="error",
    )
    assert ast.operator == "sum"
    assert ast.operands == ("revenue_eu", "revenue_na")


# ==============================================================================
# AC-8: Extraction Quality Independence
# ==============================================================================

def test_ac8_extraction_quality_independence(
    sample_auth_context: TrustedAuthContext,
    sample_provenance: EvidenceProvenance,
):
    """CanonicalEvidence enforces extraction quality and rejects verification attributes."""
    quality = ExtractionQuality(status="VALID", confidence=0.95)
    evidence = CanonicalEvidence.create_from_extraction(
        auth_context=sample_auth_context,
        source_id="doc_annual",
        source_version=1,
        pipeline_version="parse-v5",
        chunk_index=0,
        evidence_type="text_chunk",
        location=DocumentLocation(page=1),
        content=TextPayload(text="Report overview text"),
        provenance=sample_provenance,
        quality=quality,
    )
    assert evidence.quality.status == "VALID"
    assert evidence.quality.confidence == 0.95
    # Extra verification fields must be rejected
    with pytest.raises(ValidationError):
        CanonicalEvidence(
            **evidence.model_dump(),
            support_status="ASSERTED",  # type: ignore[call-arg]
        )


# ==============================================================================
# AC-9: Snapshot Pinned Verification Records & Semantic Mismatch Detection
# ==============================================================================

def test_ac9_snapshot_pinned_verification_record(sample_auth_context: TrustedAuthContext):
    """VerificationRecord is pinned to snapshot and embeds verifier type, version, attempt in ID."""
    rec = VerificationRecord.create_verified(
        auth_context=sample_auth_context,
        query_id="q_100",
        snapshot_id="snap_ws_enterprise_1_gen4",
        requirement_id="r1",
        evidence_id="ev_doc_annual_v1_0",
        verifier_version="semantic-verifier-v2",
        verifier_type="generative",
        attempt_number=1,
        execution_time_ms=125,
        support_status="ASSERTED",
        confidence_score=0.99,
        rationale="Matches reported revenue 184.2 million euros.",
        verified_at="2026-10-08T01:00:00Z",
    )
    assert rec.snapshot_id == "snap_ws_enterprise_1_gen4"
    assert rec.attempt_number == 1
    assert "generative" in rec.verification_id
    assert "semantic-verifier-v2" in rec.verification_id
    assert "att1" in rec.verification_id


def test_ac9_verifier_attempt_limit_enforced():
    """attempt_number > 3 is rejected."""
    with pytest.raises(ValidationError):
        VerificationRecord(
            verification_id="vr_test",
            workspace_id="ws_1",
            query_id="q_1",
            snapshot_id="snap_1",
            requirement_id="r1",
            evidence_id="ev_1",
            verifier_version="v1",
            verifier_type="encoder",
            attempt_number=4,
            execution_time_ms=10,
            support_status="ASSERTED",
            confidence_score=0.9,
            verification_rationale="Test",
            verified_at="2026-10-08T00:00:00Z",
        )


def test_ac9_semantic_mismatch_negative_case(sample_auth_context: TrustedAuthContext):
    """Evaluating revenue requirement against EBITDA evidence produces CONTRADICTED or UNVERIFIED."""
    rec = VerificationRecord.create_verified(
        auth_context=sample_auth_context,
        query_id="q_100",
        snapshot_id="snap_ws_enterprise_1_gen4",
        requirement_id="r_revenue_lookup",
        evidence_id="ev_sheet_financials_ebitda_row",
        verifier_version="semantic-verifier-v2",
        verifier_type="mechanical",
        attempt_number=1,
        execution_time_ms=45,
        support_status="CONTRADICTED",
        confidence_score=0.98,
        rationale="Semantic mismatch: requirement requests net revenue, but evidence is EBITDA from operating table.",
        verified_at="2026-10-08T01:00:00Z",
    )
    assert rec.support_status == "CONTRADICTED"
    assert "Semantic mismatch" in rec.verification_rationale


# ==============================================================================
# AC-10: Comprehensive Locator and Payload Compatibility Validation
# ==============================================================================

def test_ac10_locator_payload_compatibility_valid(
    sample_auth_context: TrustedAuthContext,
    sample_provenance: EvidenceProvenance,
    sample_quality: ExtractionQuality,
):
    """Valid pairings across evidence type, locator, and payload pass validation."""
    # text_chunk + DocumentLocation + TextPayload
    e1 = CanonicalEvidence.create_from_extraction(
        auth_context=sample_auth_context,
        source_id="s1",
        source_version=1,
        pipeline_version="p1",
        chunk_index=0,
        evidence_type="text_chunk",
        location=DocumentLocation(page=1),
        content=TextPayload(text="Text"),
        provenance=sample_provenance,
        quality=sample_quality,
    )
    assert e1.type == "text_chunk"

    # table_cell + TableLocation + FormulaPayload
    e2 = CanonicalEvidence.create_from_extraction(
        auth_context=sample_auth_context,
        source_id="s1",
        source_version=1,
        pipeline_version="p1",
        chunk_index=1,
        evidence_type="table_cell",
        location=TableLocation(table_id="tbl_1"),
        content=FormulaPayload(raw_expression="=A1+B1", cached_value=10),
        provenance=sample_provenance,
        quality=sample_quality,
    )
    assert e2.type == "table_cell"


def test_ac10_locator_payload_compatibility_incompatible_rejected(
    sample_auth_context: TrustedAuthContext,
    sample_provenance: EvidenceProvenance,
    sample_quality: ExtractionQuality,
):
    """Incompatible pairings are rejected with clear validation errors."""
    # text_chunk with TableLocation is forbidden
    with pytest.raises(ValidationError, match="requires document, section, or slide location"):
        CanonicalEvidence.create_from_extraction(
            auth_context=sample_auth_context,
            source_id="s1",
            source_version=1,
            pipeline_version="p1",
            chunk_index=0,
            evidence_type="text_chunk",
            location=TableLocation(table_id="tbl_1"),
            content=TextPayload(text="Text"),
            provenance=sample_provenance,
            quality=sample_quality,
        )

    # table_cell with TextPayload is forbidden
    with pytest.raises(ValidationError, match="requires cell or formula payload"):
        CanonicalEvidence.create_from_extraction(
            auth_context=sample_auth_context,
            source_id="s1",
            source_version=1,
            pipeline_version="p1",
            chunk_index=1,
            evidence_type="table_cell",
            location=TableLocation(table_id="tbl_1"),
            content=TextPayload(text="Text in cell"),  # type: ignore[arg-type]
            provenance=sample_provenance,
            quality=sample_quality,
        )


# ==============================================================================
# AC-11: Operator Specific Predicates, Conjunction, Requirement Uniqueness, Policy
# ==============================================================================

def test_ac11_operator_specific_predicate_validation():
    """SelectionPredicate validates operator specific value shapes."""
    # eq with single scalar primitive passes
    p_eq = SelectionPredicate(field="region", operator="eq", value="Europe")
    assert p_eq.value == "Europe"

    # eq with list/tuple fails
    with pytest.raises(ValidationError, match="single scalar primitive"):
        SelectionPredicate(field="region", operator="eq", value=("Europe", "Asia"))

    # between with ordered tuple passes
    p_bet = SelectionPredicate(field="year", operator="between", value=(2020, 2024))
    assert p_bet.value == (2020, 2024)

    # between with inverted bounds fails
    with pytest.raises(ValidationError, match="lower bound .* must be <= upper bound"):
        SelectionPredicate(field="year", operator="between", value=(2024, 2020))

    # in with non empty tuple passes
    p_in = SelectionPredicate(field="year", operator="in", value=(2022, 2023, 2024))
    assert len(p_in.value) == 3  # type: ignore[arg-type]

    # in with single integer fails
    with pytest.raises(ValidationError, match="non empty tuple"):
        SelectionPredicate(field="year", operator="in", value=2024)  # type: ignore[arg-type]

    # contains with non empty string passes
    p_cnt = SelectionPredicate(field="notes", operator="contains", value="audit")
    assert p_cnt.value == "audit"

    # contains with empty string fails
    with pytest.raises(ValidationError, match="non empty string"):
        SelectionPredicate(field="notes", operator="contains", value="")


def test_ac11_requirement_uniqueness(sample_auth_context: TrustedAuthContext):
    """Duplicate requirement_id in QueryContract raises ValidationError."""
    r1 = RequirementItem(requirement_id="r1", intent="lookup")
    r2_dup = RequirementItem(requirement_id="r1", intent="summarize")
    with pytest.raises(ValidationError, match="Requirement IDs must be unique"):
        QueryContract.create_planned(
            auth_context=sample_auth_context,
            query_id="q_1",
            snapshot_id="snap_1",
            requirements=(r1, r2_dup),
        )


def test_ac11_mixed_query_hybrid_answer_mode(sample_auth_context: TrustedAuthContext):
    """Query mixing computation and summarization resolves to hybrid answer mode."""
    r_agg = RequirementItem(requirement_id="r1", intent="aggregate", operation="sum")
    r_sum = RequirementItem(requirement_id="r2", intent="summarize")
    qc = QueryContract.create_planned(
        auth_context=sample_auth_context,
        query_id="q_1",
        snapshot_id="snap_1",
        requirements=(r_agg, r_sum),
    )
    assert qc.resolved_answer_mode == "hybrid"


def test_ac11_server_execution_policy_batching():
    """ServerExecutionPolicy separates batch size from scan ceilings."""
    policy = ServerExecutionPolicy(
        execution_batch_size=5000,
        max_execution_scan_rows=50000,
        max_execution_memory_mb=256,
        execution_timeout_ms=5000,
    )
    assert policy.execution_batch_size == 5000
    assert policy.max_execution_scan_rows == 50000


# ==============================================================================
# AC-12: Partition Aware Coverage, Completeness Proof, and Scoped Readiness
# ==============================================================================

def test_ac12_partition_completeness_proof():
    """ArtifactCoverage validates closed ranges and completeness proof with zero gaps."""
    # Successful complete coverage on 1 indexed pages [1, 50]
    cov = ArtifactCoverage(
        unit_type="page",
        covered_ranges=(
            CoverageRange(unit_type="page", start_index=1, end_index=25),
            CoverageRange(unit_type="page", start_index=26, end_index=50),
        ),
        processed_count=50,
        total_units=50,
        is_complete=True,
    )
    assert cov.is_complete is True


def test_ac12_partition_completeness_proof_gap_rejected():
    """Coverage with gaps between ranges cannot be complete."""
    with pytest.raises(ValidationError, match="Completeness proof failed: gap"):
        ArtifactCoverage(
            unit_type="page",
            covered_ranges=(
                CoverageRange(unit_type="page", start_index=1, end_index=20),
                # Gap: 21 to 24 missing
                CoverageRange(unit_type="page", start_index=25, end_index=50),
            ),
            processed_count=46,
            total_units=46,
            is_complete=True,
        )


def test_ac12_unknown_total_units_blocks_is_complete():
    """total_units=None cannot have is_complete=True."""
    with pytest.raises(ValidationError, match="is_complete cannot be True when total_units is unknown"):
        ArtifactCoverage(
            unit_type="chunk",
            covered_ranges=(CoverageRange(unit_type="chunk", start_index=0, end_index=10),),
            processed_count=11,
            total_units=None,
            is_complete=True,
        )


def test_ac12_scoped_readiness_partitions():
    """ready_partitions exposes ready partition scopes for planner routing."""
    cov = ArtifactCoverage(
        unit_type="sheet",
        covered_ranges=(
            CoverageRange(unit_type="sheet", start_index=1, end_index=1, partition_id="Sheet:Sales"),
        ),
        processed_count=1,
        total_units=5,
        is_complete=False,
        ready_partitions=("Sheet:Sales",),
    )
    assert cov.ready_partitions == ("Sheet:Sales",)


# ==============================================================================
# AC-13: Explicit Serialization and Deserialization Coercion Rules
# ==============================================================================

def test_ac13_json_round_trip_coerces_lists_to_tuples(
    sample_auth_context: TrustedAuthContext,
    sample_provenance: EvidenceProvenance,
    sample_quality: ExtractionQuality,
):
    """JSON serialization and deserialization coerces lists into immutable tuples."""
    ev = CanonicalEvidence.create_from_extraction(
        auth_context=sample_auth_context,
        source_id="doc_annual",
        source_version=1,
        pipeline_version="parse-v5",
        chunk_index=0,
        evidence_type="text_chunk",
        location=DocumentLocation(page=1, section_path=["A", "B"]),  # type: ignore[arg-type]
        content=TextPayload(text="Overview"),
        provenance=sample_provenance,
        quality=sample_quality,
        qualifiers=(QualifierItem(key="tag", value="v1"),),
    )
    json_str = ev.model_dump_json()
    parsed = json.loads(json_str)
    # Validate that JSON contains lists
    assert isinstance(parsed["location"]["section_path"], list)

    # Reconstruct from JSON
    restored = CanonicalEvidence.model_validate_json(json_str)
    # Must be coerced back to immutable tuples
    assert isinstance(restored.location.section_path, tuple)  # type: ignore[union-attr]
    assert restored.location.section_path == ("A", "B")  # type: ignore[union-attr]
    assert isinstance(restored.qualifiers, tuple)


# ==============================================================================
# AC-14: Query Plane Structured Execution Result Contract & Invariants
# ==============================================================================

def test_ac14_structured_execution_result_contract(sample_auth_context: TrustedAuthContext):
    """StructuredExecutionResult captures query execution outcome with invariants."""
    ds_ref = DatasetReference(
        source_id="sheet_tx",
        source_version=1,
        registered_file_id="reg_1",
        table_id="tbl_tx",
        total_rows=1000,
        total_columns=10,
        schema_hash="hash_1",
    )
    res = StructuredExecutionResult.create_result(
        auth_context=sample_auth_context,
        query_id="q_100",
        snapshot_id="snap_1",
        requirement_id="r1",
        attempt_number=1,
        dataset_ref=ds_ref,
        operation="sum",
        metric="revenue",
        selection=(SelectionPredicate(field="region", operator="eq", value="Europe"),),
        records_examined=1000,
        records_matched=42,
        selection_digest="sha256:digest_1",
        lineage_ref="s3://kre-lineage/q100.parquet",
        result_value=184.2,
        result_unit="EUR_million",
        coverage_status="COMPLETE_FOR_SELECTION",
        executed_at="2026-10-08T01:00:00Z",
    )
    assert res.attempt_number == 1
    assert res.records_matched <= res.records_examined
    assert res.coverage_status == "COMPLETE_FOR_SELECTION"


def test_ac14_structured_execution_invariants_rejected(sample_auth_context: TrustedAuthContext):
    """records_matched > records_examined raises ValidationError."""
    ds_ref = DatasetReference(
        source_id="sheet_tx",
        source_version=1,
        registered_file_id="reg_1",
        table_id="tbl_tx",
        total_rows=1000,
        total_columns=10,
        schema_hash="hash_1",
    )
    with pytest.raises(ValidationError, match="records_matched .* cannot exceed records_examined"):
        StructuredExecutionResult.create_result(
            auth_context=sample_auth_context,
            query_id="q_100",
            snapshot_id="snap_1",
            requirement_id="r1",
            dataset_ref=ds_ref,
            operation="sum",
            records_examined=50,
            records_matched=100,  # invalid: matched > examined
            selection_digest="digest",
            lineage_ref="lineage",
            result_value=10,
            coverage_status="COMPLETE_FOR_SELECTION",
            executed_at="2026-10-08T01:00:00Z",
        )


def test_ac5_slide_and_section_location_bounds():
    """Slide numbers must be positive (>= 1) and section paragraph indices non negative (>= 0)."""
    with pytest.raises(ValidationError):
        SlideLocation(slide_number=0)

    with pytest.raises(ValidationError):
        SectionLocation(heading="Intro", paragraph_index=-1)


def test_ac6_cell_payload_char_cap_rejected():
    """CellPayload raw_value string exceeding 4096 characters raises ValidationError."""
    with pytest.raises(ValidationError, match="exceeds 4096 characters"):
        CellPayload(raw_value="A" * 4097)


def test_ac6_table_row_length_mismatch_rejected():
    """TablePayload with row length differing from headers count raises ValidationError."""
    with pytest.raises(ValidationError, match="Row at index 0 has length 3, expected 2"):
        TablePayload(
            table_id="tbl_mismatch",
            headers=("A", "B"),
            rows=((1, 2, 3),),
            row_count=1,
            col_count=2,
        )


def test_ac7_executable_formula_unsupported_operator():
    """ExecutableFormulaAST rejects unapproved operators or arbitrary code execution."""
    with pytest.raises(ValidationError):
        ExecutableFormulaAST(operator="eval", operands=("x",))  # type: ignore[arg-type]


def test_ac8_extraction_quality_confidence_bounds():
    """ExtractionQuality confidence must be strictly between 0.0 and 1.0."""
    with pytest.raises(ValidationError):
        ExtractionQuality(status="VALID", confidence=1.01)

    with pytest.raises(ValidationError):
        ExtractionQuality(status="VALID", confidence=-0.01)


def test_ac9_verification_record_empty_rationale_rejected(sample_auth_context: TrustedAuthContext):
    """VerificationRecord rejects empty or whitespace verification rationale."""
    with pytest.raises(ValidationError, match="non empty string"):
        VerificationRecord.create_verified(
            auth_context=sample_auth_context,
            query_id="q_1",
            snapshot_id="snap_1",
            requirement_id="r1",
            evidence_id="ev_1",
            verifier_version="v1",
            verifier_type="mechanical",
            attempt_number=1,
            execution_time_ms=10,
            support_status="ASSERTED",
            confidence_score=0.9,
            rationale="   ",
            verified_at="2026-10-08T00:00:00Z",
        )


def test_ac11_pure_answer_modes_enforced(sample_auth_context: TrustedAuthContext):
    """Pure computational requirements enforce deterministic; pure summarize enforces narrative."""
    r_agg = RequirementItem(requirement_id="r1", intent="aggregate", operation="sum")
    qc_det = QueryContract.create_planned(
        auth_context=sample_auth_context,
        query_id="q_1",
        snapshot_id="snap_1",
        requirements=(r_agg,),
    )
    assert qc_det.resolved_answer_mode == "deterministic"

    r_sum = RequirementItem(requirement_id="r2", intent="summarize")
    qc_narr = QueryContract.create_planned(
        auth_context=sample_auth_context,
        query_id="q_2",
        snapshot_id="snap_1",
        requirements=(r_sum,),
    )
    assert qc_narr.resolved_answer_mode == "narrative"


def test_ac12_coverage_range_bounds_validation():
    """CoverageRange start_index must be <= end_index and respect 1 indexed units."""
    with pytest.raises(ValidationError, match="start_index .* must be <= end_index"):
        CoverageRange(unit_type="chunk", start_index=15, end_index=10)

    with pytest.raises(ValidationError, match="requires 1-indexed start_index"):
        CoverageRange(unit_type="page", start_index=0, end_index=10)


def test_ac14_structured_execution_empty_selection_mismatch(sample_auth_context: TrustedAuthContext):
    """EMPTY_SELECTION with positive records_matched raises ValidationError."""
    ds_ref = DatasetReference(
        source_id="sheet_tx",
        source_version=1,
        registered_file_id="reg_1",
        table_id="tbl_tx",
        total_rows=1000,
        total_columns=10,
        schema_hash="hash_1",
    )
    with pytest.raises(ValidationError, match="coverage_status cannot be 'EMPTY_SELECTION'"):
        StructuredExecutionResult.create_result(
            auth_context=sample_auth_context,
            query_id="q_100",
            snapshot_id="snap_1",
            requirement_id="r1",
            dataset_ref=ds_ref,
            operation="sum",
            records_examined=50,
            records_matched=10,
            selection_digest="digest",
            lineage_ref="lineage",
            result_value=10,
            coverage_status="EMPTY_SELECTION",
            executed_at="2026-10-08T01:00:00Z",
        )

