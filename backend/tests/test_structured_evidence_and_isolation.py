import pytest
from services.evaluation.benchmark_scorer import validate_citations
from services.retrieval.evidence_contract import EvidenceItem
from services.retrieval.strategy_router import StrategyRouter


def _make_retained_evidence_item():
    return EvidenceItem(
        evidence_id="table_survay_abc123def4567890",
        workspace_id="ws_test",
        document_id="doc_survay",
        document_version="c029d234eefd3fee",
        source_type="table_row_set",
        locator={
            "table_id": "survay",
            "selection_hash": "abc123def4567890",
            "selection_count": 2,
            "total_rows_in_table": 60255,
            "location_reference": "2 row(s) from survay",
        },
        text="Range 2013 to 2025",
        structured_payload={
            "table_id": "survay",
            "document_version": "c029d234eefd3fee",
            "operator": "range",
            "target_column": "Year",
            "selection_hash": "abc123def4567890",
            "selection_count": 2,
            "result_value": "2013",
        },
        score=1.0,
        strategy="structured_table",
        provenance={"table_id": "survay"},
        citation_payload={
            "table_id": "survay",
            "document_version": "c029d234eefd3fee",
            "operator": "range",
            "target_column": "Year",
            "selection_hash": "abc123def4567890",
            "selection_count": 2,
            "result_value": "2013",
            "workspace_id": "ws_test",
        },
    )


def test_citation_tampered_count_rejected_even_with_matching_hash():
    item = _make_retained_evidence_item()
    citation = {
        "evidence_type": "structured_aggregate",
        "workspace_id": "ws_test",
        "document_id": "doc_survay",
        "document_version": "c029d234eefd3fee",
        "table_id": "survay",
        "operator": "range",
        "target_column": "Year",
        "selection_hash": "abc123def4567890",
        "selection_count": 999,  # TAMPERED
        "result_value": "2013",
    }
    res = validate_citations([citation], retained_evidence_items=[item])
    assert not res["valid"]
    assert any("selection_count" in err for err in res["errors"])


def test_citation_tampered_table_id_rejected_even_with_matching_hash():
    item = _make_retained_evidence_item()
    citation = {
        "evidence_type": "structured_aggregate",
        "workspace_id": "ws_test",
        "document_id": "doc_survay",
        "document_version": "c029d234eefd3fee",
        "table_id": "fake_table",  # TAMPERED
        "operator": "range",
        "target_column": "Year",
        "selection_hash": "abc123def4567890",
        "selection_count": 2,
        "result_value": "2013",
    }
    res = validate_citations([citation], retained_evidence_items=[item])
    assert not res["valid"]
    assert any("table_id" in err for err in res["errors"])


def test_citation_tampered_version_rejected_even_with_matching_hash():
    item = _make_retained_evidence_item()
    citation = {
        "evidence_type": "structured_aggregate",
        "workspace_id": "ws_test",
        "document_id": "doc_survay",
        "document_version": "fake_version_9999",  # TAMPERED
        "table_id": "survay",
        "operator": "range",
        "target_column": "Year",
        "selection_hash": "abc123def4567890",
        "selection_count": 2,
        "result_value": "2013",
    }
    res = validate_citations([citation], retained_evidence_items=[item])
    assert not res["valid"]
    assert any("document_version" in err for err in res["errors"])


def test_citation_tampered_operator_rejected_even_with_matching_hash():
    item = _make_retained_evidence_item()
    citation = {
        "evidence_type": "structured_aggregate",
        "workspace_id": "ws_test",
        "document_id": "doc_survay",
        "document_version": "c029d234eefd3fee",
        "table_id": "survay",
        "operator": "fake_operator",  # TAMPERED
        "target_column": "Year",
        "selection_hash": "abc123def4567890",
        "selection_count": 2,
        "result_value": "2013",
    }
    res = validate_citations([citation], retained_evidence_items=[item])
    assert not res["valid"]
    assert any("operator" in err for err in res["errors"])


def test_citation_valid_matching_retained_passes():
    item = _make_retained_evidence_item()
    citation = {
        "evidence_type": "structured_aggregate",
        "workspace_id": "ws_test",
        "document_id": "doc_survay",
        "document_version": "c029d234eefd3fee",
        "table_id": "survay",
        "operator": "range",
        "target_column": "Year",
        "selection_hash": "abc123def4567890",
        "selection_count": 2,
        "result_value": "2013",
    }
    res = validate_citations([citation], retained_evidence_items=[item])
    assert res["valid"]
    assert res["valid_count"] == 1
    assert len(res["errors"]) == 0


def test_graph_selection_prevented_when_isolation_missing():
    router = StrategyRouter()
    decision = router.route("What is the relationship between Company A and Company B?")
    # Must NOT select knowledge_graph because graph lacks workspace isolation
    assert "knowledge_graph" not in decision.primary
    assert "knowledge_graph" not in decision.fallback
