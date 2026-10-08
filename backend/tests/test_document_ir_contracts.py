from decimal import Decimal

import pytest

from schemas.document_ir import (
    DocumentIR,
    NodeType,
    Relationship,
    RelationType,
    StructuralNode,
)
from schemas.evidence import (
    EvidenceChunk,
    EvidenceConfidence,
    EvidenceEnvelope,
    EvidenceType,
    FactEvidence,
    MetadataEvidence,
    RelationshipEvidence,
    StructuredRowEvidence,
    TextEvidence,
)
from schemas.manifest import (
    CompletenessExpectation,
    DocumentManifest,
    PopulationState,
    SheetManifest,
    SheetVisibility,
)
from schemas.structured_table import (
    ColumnDefinition,
    HeaderTopology,
    InferredDtype,
    MergedRange,
    StructuredTable,
    StructureOrigin,
    TableCell,
    TableRow,
    TableSchema,
)


def test_structural_node_and_relationships():
    node1 = StructuralNode(
        id="doc1:sec:1",
        document_id="doc1",
        node_type=NodeType.SECTION,
        title="Introduction",
        text="Section introduction text.",
    )
    node2 = StructuralNode(
        id="doc1:p:1",
        document_id="doc1",
        node_type=NodeType.PARAGRAPH,
        text="Paragraph under introduction.",
        parent_id=node1.id,
    )

    rel = Relationship(
        source_id=node1.id,
        target_id=node2.id,
        relation_type=RelationType.CONTAINS,
    )

    assert node1.node_type == NodeType.SECTION
    assert node2.parent_id == "doc1:sec:1"
    assert rel.relation_type == RelationType.CONTAINS
    assert rel.relation_type != RelationType.SPEAKER_NOTES_FOR


def test_evidence_envelope_typed_payloads():
    # 1. TextEvidence
    text_pay = TextEvidence(text="Sample text", element_type="paragraph", page_number=1)
    env1 = EvidenceEnvelope(
        evidence_id="env_1",
        evidence_type=EvidenceType.TEXT,
        document_id="doc1",
        location={"page": 1},
        provider="dense",
        provider_score=0.88,
        payload=text_pay,
    )
    assert isinstance(env1.payload, TextEvidence)
    assert env1.payload.text == "Sample text"

    # 2. StructuredRowEvidence
    row_pay = StructuredRowEvidence(
        table_id="tab1",
        row_id="r10",
        row_index=10,
        headers=("Year", "Revenue"),
        values=(2022, 1000000),
        sheet_name="Summary",
    )
    env2 = EvidenceEnvelope(
        evidence_id="env_2",
        evidence_type=EvidenceType.STRUCTURED_ROW,
        document_id="doc1",
        location={"row": 10},
        provider="schema_table",
        provider_score=0.95,
        payload=row_pay,
    )
    assert isinstance(env2.payload, StructuredRowEvidence)
    assert env2.payload.values[1] == 1000000

    # 3. FactEvidence
    fact_pay = FactEvidence(
        entity="Acme Corp",
        attribute="CEO",
        value="Jane Doe",
        confidence=0.99,
    )
    env3 = EvidenceEnvelope(
        evidence_id="env_3",
        evidence_type=EvidenceType.FACT,
        document_id="doc1",
        location={},
        provider="okf",
        provider_score=0.99,
        payload=fact_pay,
    )
    assert isinstance(env3.payload, FactEvidence)
    assert env3.payload.entity == "Acme Corp"

    # 4. RelationshipEvidence
    rel_pay = RelationshipEvidence(
        source_entity="Acme Corp",
        relation="acquired",
        target_entity="Beta Tech",
    )
    env4 = EvidenceEnvelope(
        evidence_id="env_4",
        evidence_type=EvidenceType.RELATIONSHIP,
        document_id="doc1",
        location={},
        provider="graph",
        provider_score=0.85,
        payload=rel_pay,
    )
    assert isinstance(env4.payload, RelationshipEvidence)
    assert env4.payload.target_entity == "Beta Tech"

    # 5. MetadataEvidence
    meta_pay = MetadataEvidence(
        document_id="doc1",
        attribute_name="hidden_sheets",
        attribute_value=["RawInputs", "Secrets"],
    )
    env5 = EvidenceEnvelope(
        evidence_id="env_5",
        evidence_type=EvidenceType.METADATA,
        document_id="doc1",
        location={},
        provider="metadata",
        provider_score=1.0,
        payload=meta_pay,
    )
    assert isinstance(env5.payload, MetadataEvidence)
    assert "RawInputs" in env5.payload.attribute_value


def test_evidence_confidence_blend():
    conf = EvidenceConfidence(
        retrieval_confidence=0.80,
        schema_binding_confidence=0.90,
        structural_confidence=0.70,
    )
    # Expected: (0.80 * 0.3) + (0.90 * 0.4) + (0.70 * 0.3) = 0.24 + 0.36 + 0.21 = 0.81
    assert conf.overall_confidence == 0.81


def test_structured_table_model():
    col1 = ColumnDefinition(col_index=0, name="District", inferred_dtype=InferredDtype.STRING)
    col2 = ColumnDefinition(col_index=1, name="Households", inferred_dtype=InferredDtype.INTEGER, unit="count")

    schema = TableSchema(
        columns=(col1, col2),
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=0.95,
    )

    cell1 = TableCell(
        cell_id="c_0_0",
        row_index=1,
        col_index=0,
        coordinate="A2",
        raw_value="Nicobars",
        normalized_value="Nicobars",
    )
    cell2 = TableCell(
        cell_id="c_0_1",
        row_index=1,
        col_index=1,
        coordinate="B2",
        raw_value="882.0",
        cached_value=882,
        raw_formula=None,
        normalized_value=Decimal("882"),
        inferred_dtype=InferredDtype.INTEGER,
    )

    row = TableRow(row_id="r_1", row_index=1, cells=(cell1, cell2))
    table = StructuredTable(
        table_id="tab_1",
        document_id="doc_1",
        sheet_name="Factsheet",
        page_number=None,
        schema=schema,
        rows=(row,),
        merged_ranges=(MergedRange(range_str="A1:B1", start_row=0, start_col=0, end_row=0, end_col=1, anchor_cell="A1"),),
        row_count=1,
        col_count=2,
        structure_origin=StructureOrigin.NATIVE,
        structure_confidence=1.0,
    )

    assert table.row_count == 1
    assert table.rows[0].cells[1].normalized_value == Decimal("882")
    assert table.structure_origin == StructureOrigin.NATIVE


def test_manifest_completeness_validation():
    sheet1 = SheetManifest(
        sheet_name="Data",
        sheet_index=0,
        visibility=SheetVisibility.VISIBLE,
        state=PopulationState.POPULATED,
        row_count=100,
        col_count=5,
        table_count=1,
    )
    sheet2 = SheetManifest(
        sheet_name="EmptyNotes",
        sheet_index=1,
        visibility=SheetVisibility.HIDDEN,
        state=PopulationState.EMPTY,
        row_count=0,
        col_count=0,
        table_count=0,
    )

    manifest_ok = DocumentManifest(
        document_id="doc_test",
        filename="test.xlsx",
        source_format="xlsx",
        source_size_bytes=1024,
        parser_version="1.0.0",
        detected_sheets=(sheet1, sheet2),
        detected_tables=CompletenessExpectation(expected_count=1, indexed_count=1),
        detected_rows=CompletenessExpectation(expected_count=100, indexed_count=100),
    )
    is_valid, errors = manifest_ok.validate_preflight()
    assert is_valid is True
    assert len(errors) == 0

    manifest_fail = DocumentManifest(
        document_id="doc_test",
        filename="test.xlsx",
        source_format="xlsx",
        source_size_bytes=1024,
        parser_version="1.0.0",
        detected_sheets=(sheet1,),
        detected_tables=CompletenessExpectation(expected_count=3, indexed_count=1, unsupported_skipped_count=0),
        detected_rows=CompletenessExpectation(expected_count=100, indexed_count=50, unsupported_skipped_count=0),
    )
    is_valid_fail, errors_fail = manifest_fail.validate_preflight()
    assert is_valid_fail is False
    assert len(errors_fail) == 2
    assert "Table loss" in errors_fail[0]
    assert "Row loss" in errors_fail[1]
