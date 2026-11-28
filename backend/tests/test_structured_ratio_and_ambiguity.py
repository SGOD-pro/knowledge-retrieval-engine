"""Focused tests for deterministic column ratios, zero-denominator guards, and ambiguous scope clarification."""

from decimal import Decimal
from pathlib import Path

import pytest

from db.table_store.memory_store import MemoryTableStore
from ingestion.csv_table_ingester import ingest_csv_to_table_store
from services.retrieval.planner import planner
from services.retrieval.structured_query_service import (
    AmbiguousBindingError,
    StructuredQueryService,
)

CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "non_pdf_formats" / "MO_Post_2024_25.csv"


def _build_service(csv_path: Path = CSV_PATH) -> tuple[StructuredQueryService, MemoryTableStore]:
    store = MemoryTableStore()
    ingest_csv_to_table_store(csv_path, document_id="doc_mo", workspace_id="ws_test", store=store)
    return StructuredQueryService(store), store


def test_canary2_filtered_percentage_routes_and_computes_deterministically():
    q = "What percentage of sanctioned medical officer posts in UNANI are vacant? Round to 2 decimals."
    plan = planner.route(q)
    assert plan.use_structured_aggregate is True

    svc, _ = _build_service()
    res = svc.execute(q, "ws_test")
    assert res is not None
    assert res.operator == "percentage"
    assert res.result_value == Decimal("43.81")
    assert res.selection_count == 1
    assert len(res.row_ids) == 1
    assert "43.81%" in res.answer_text


def test_canary3_ambiguous_scope_raises_clarification():
    q = "How many medical officer posts are vacant?"
    plan = planner.route(q)
    assert plan.use_structured_aggregate is True

    svc, _ = _build_service()
    with pytest.raises(AmbiguousBindingError, match="SYSTEM"):
        svc.execute(q, "ws_test")


def test_unmatched_explicit_entity_does_not_raise_ambiguous():
    from services.retrieval.structured_query_service import UnresolvedFilterError, UnsupportedQueryError
    svc, _ = _build_service()
    with pytest.raises((UnsupportedQueryError, UnresolvedFilterError)):
        svc.execute("How many medical officer posts are sanctioned in SIDDHA?", "ws_test")


def test_held_out_filtered_ratio_ayurveda():
    q = "What percentage of sanctioned medical officer posts in AYURVEDA are vacant? Round to 2 decimals."
    svc, _ = _build_service()
    res = svc.execute(q, "ws_test")
    assert res is not None
    assert res.result_value == Decimal("23.94")
    assert res.selection_count == 1


def test_held_out_zero_denominator_guard(tmp_path: Path):
    zero_csv = tmp_path / "zero_denom.csv"
    zero_csv.write_text(
        "Sl. No.,SYSTEM,SANCTION,FILLED,VACANT\n1,SIDDHA,0,0,0\n",
        encoding="utf-8",
    )
    svc, _ = _build_service(zero_csv)
    res = svc.execute("What percentage of sanctioned posts in SIDDHA are vacant?", "ws_test")
    assert res is not None
    assert res.result_value is None
    assert "denominator sum is 0" in res.answer_text


def test_held_out_explicit_all_system_total():
    q = "What is the total number of vacant medical officer posts across all systems?"
    plan = planner.route(q)
    assert plan.use_structured_aggregate is True

    svc, _ = _build_service()
    res = svc.execute(q, "ws_test")
    assert res is not None
    assert res.operator == "sum"
    assert res.result_value == Decimal("255")
    assert res.selection_count == 4


def test_held_out_ambiguous_single_system_request():
    q = "How many medical officer posts are sanctioned?"
    plan = planner.route(q)
    assert plan.use_structured_aggregate is True

    svc, _ = _build_service()
    with pytest.raises(AmbiguousBindingError, match="SYSTEM"):
        svc.execute(q, "ws_test")


def test_unmatched_explicit_entity_does_not_raise_ambiguous():
    from services.retrieval.structured_query_service import UnresolvedFilterError, UnsupportedQueryError
    svc, _ = _build_service()
    with pytest.raises((UnsupportedQueryError, UnresolvedFilterError)):
        svc.execute("How many medical officer posts are sanctioned in SIDDHA?", "ws_test")
