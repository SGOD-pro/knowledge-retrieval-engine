import pytest

from schemas.structured_table import ColumnDefinition, HeaderTopology, InferredDtype, TableSchema
from services.retrieval.schema_resolver import (
    resolve_schema_concept,
)


def _build_test_schema() -> TableSchema:
    cols = (
        ColumnDefinition(col_index=0, name="District Names", inferred_dtype=InferredDtype.STRING),
        ColumnDefinition(col_index=1, name="Total Turnover (₹ Cr)", inferred_dtype=InferredDtype.DECIMAL, unit="INR Cr"),
        ColumnDefinition(col_index=2, name="Total Workforce", inferred_dtype=InferredDtype.INTEGER, unit="count"),
        ColumnDefinition(col_index=3, name="Number of Households surveyed", inferred_dtype=InferredDtype.INTEGER),
    )
    return TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)


def test_exact_match():
    schema = _build_test_schema()
    res = resolve_schema_concept("District Names", schema)
    assert not res.is_ambiguous
    assert len(res.bindings) == 1
    assert res.bindings[0].column_name == "District Names"
    assert res.bindings[0].confidence == 1.0


def test_synonym_resolution_sales_to_turnover():
    schema = _build_test_schema()
    # "sales" should match "Total Turnover (₹ Cr)" via general ontology
    res = resolve_schema_concept("sales", schema)
    assert not res.is_ambiguous
    assert len(res.bindings) == 1
    assert res.bindings[0].column_name == "Total Turnover (₹ Cr)"
    assert res.bindings[0].confidence >= 0.80


def test_synonym_resolution_employees_to_workforce():
    schema = _build_test_schema()
    # "employees" should match "Total Workforce"
    res = resolve_schema_concept("employees", schema)
    assert not res.is_ambiguous
    assert len(res.bindings) == 1
    assert res.bindings[0].column_name == "Total Workforce"
    assert res.bindings[0].confidence >= 0.80


def test_ambiguity_margin_detection():
    # Table with two near-identical synonyms
    cols = (
        ColumnDefinition(col_index=0, name="Total Revenue", inferred_dtype=InferredDtype.DECIMAL),
        ColumnDefinition(col_index=1, name="Total Turnover", inferred_dtype=InferredDtype.DECIMAL),
    )
    schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)

    # Query concept "sales" matches both revenue and turnover closely
    res = resolve_schema_concept("sales", schema, ambiguity_margin=0.15)
    assert res.is_ambiguous
    assert len(res.ambiguity_candidates) == 2


def test_unrelated_concept_rejection():
    schema = _build_test_schema()
    res = resolve_schema_concept("astrophysics telescope", schema, min_confidence=0.70)
    assert len(res.bindings) == 0
    assert res.top_confidence < 0.70
