"""Offline regression suite for TableStore multiyear coverage (>5000 rows).

Verifies:
1. Complete ingestion and reads over datasets exceeding 5,000 rows across multiple years.
2. Complete multiyear range (min/max), sum, count, and temporal metric delta queries.
3. Accurate coverage metadata (source_rows, persisted_rows, rejected_rows, coverage_complete).
4. Boundary isolation across workspaces and document scopes for large tables.
5. Strict refusal of complete-dataset aggregates when table coverage is incomplete.
6. Validation against real survay.csv (60,255 rows across 13 years: 2013-2025).
7. DynamoDB paginated single-pass reads over multiple pages via LastEvaluatedKey.
"""

import csv
from decimal import Decimal
import io
import os
from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch
import pytest

from config import settings
from db.table_store import get_shared_table_store
from db.table_store.base import AggregateOp, Predicate, PredicateOp
from db.table_store.dynamodb_store import DynamoDBTableStore
from db.table_store.memory_store import MemoryTableStore
from db.table_store.sqlite_store import SQLiteTableStore
from ingestion.csv_table_ingester import ingest_csv_to_table_store
from modules.query.query_service import QueryService
from schemas.models import QueryRequest
from schemas.structured_table import (
    ColumnDefinition,
    HeaderTopology,
    InferredDtype,
    TableCell,
    TableRow,
    TableSchema,
)
from services.retrieval.structured_query_service import (
    IncompleteDataError,
    StructuredQueryResult,
    StructuredQueryService,
)
from api.boundary_adapters import ProductionExecutionEngine
from db.workspace_boundary_repo import (
    InMemoryWorkspaceBoundaryRepository,
    get_workspace_boundary_repo,
)


@pytest.fixture(autouse=True)
def configure_test_environment(monkeypatch):
    """Ensure clean in-memory test substitutes and test mode settings."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "test")
    repo = get_workspace_boundary_repo(reset=True)
    store = get_shared_table_store(reset=True)
    yield repo, store
    get_workspace_boundary_repo(reset=True)
    get_shared_table_store(reset=True)
    try:
        import providers.reranker_provider as rp
        rp._circuit_breaker_tripped = False
    except Exception:
        pass


def generate_multiyear_csv(
    num_rows: int = 6000,
    years: list[int] | None = None,
    malformed_interval: int | None = None,
) -> Path:
    """Generate a temporary CSV with specified row count across multiple years."""
    if years is None:
        years = [2020, 2021, 2022, 2023, 2024, 2025]
    num_years = len(years)

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    writer = csv.writer(tmp)
    writer.writerow(["Year", "Industry_code_NZSIOC", "Variable_name", "Value", "Unit"])

    for i in range(num_rows):
        if malformed_interval and (i + 1) % malformed_interval == 0:
            # Malformed row with missing columns
            writer.writerow([2025, "BAD_ROW"])
            continue

        yr = years[i % num_years]
        ind = "99999" if i in (4, 5) else f"IND_{(i % 20) + 1:02d}"
        var = "Total income" if i in (4, 5) else "Standard metric"
        val = 1000 + (i * 2)
        unit = "Dollars (millions)"
        writer.writerow([yr, ind, var, val, unit])

    tmp.close()
    return Path(tmp.name)


@pytest.mark.parametrize("store_factory", [
    lambda: MemoryTableStore(),
    lambda: SQLiteTableStore(":memory:"),
])
def test_multiyear_dataset_above_5000_rows_complete_ingestion_and_reads(store_factory):
    """Verify ingestion and complete reads over 6,000 rows across 6 years (2020-2025)."""
    csv_path = generate_multiyear_csv(num_rows=6000, years=[2020, 2021, 2022, 2023, 2024, 2025])
    try:
        store = store_factory()
        ws = "ws_multiyear_complete"
        doc_id = "doc_multiyear_6000"

        res = ingest_csv_to_table_store(
            path=csv_path,
            document_id=doc_id,
            workspace_id=ws,
            store=store,
            batch_size=99,
            table_name="enterprise_survey",
        )

        assert res.source_rows == 6000
        assert res.persisted_rows == 6000
        assert res.rejected_rows == 0
        assert res.coverage_complete is True
        assert store.has_coverage_complete_table("enterprise_survey", ws) is True

        svc = StructuredQueryService(store)

        # 1. Range query across all 6,000 rows: earliest and latest year
        q_range = "What is the earliest and latest Year actually present in enterprise_survey.csv?"
        res_range = svc.execute(q_range, workspace_id=ws)
        assert res_range is not None
        assert isinstance(res_range, StructuredQueryResult)
        assert res_range.result_value == 2020
        assert res_range.secondary_value == 2025
        assert res_range.selection_count == 6000
        assert res_range.total_rows_in_table == 6000
        assert res_range.answer_text == "2020 and 2025."
        assert len(res_range.row_ids) == 6000

        # 2. Complete aggregate sum over all 6,000 rows
        # Sum of 1000 + 2*i for i in 0..5999 = 6000*1000 + 2*(5999*6000/2) = 6,000,000 + 35,994,000 = 41,994,000
        q_sum = "What is the total Value in enterprise_survey.csv?"
        res_sum = svc.execute(q_sum, workspace_id=ws)
        assert res_sum is not None
        assert res_sum.selection_count == 6000
        assert res_sum.result_value == Decimal("41994000")

        # 3. Temporal metric delta between 2024 and 2025 for 99999 Total income
        # In our generator:
        # i=4: year=2024, ind=99999, var="Total income", val = 1000 + 2*4 = 1008
        # i=5: year=2025, ind=99999, var="Total income", val = 1000 + 2*5 = 1010
        # i=10: year=2024, but ind=IND_11
        # Exactly one row for 2024 and one for 2025 for 99999 Total income
        q_delta = "For All industries (99999), how did Total income change from 2024 to 2025 in enterprise_survey.csv?"
        res_delta = svc.execute(q_delta, workspace_id=ws)
        assert res_delta is not None
        assert res_delta.operator == "compare"
        assert res_delta.selection_count == 2
        assert res_delta.result_value == Decimal("2")
        assert "increased by 2" in res_delta.answer_text.lower()
        assert "from 1,008 to 1,010" in res_delta.answer_text

    finally:
        csv_path.unlink(missing_ok=True)


@pytest.mark.parametrize("store_factory", [
    lambda: MemoryTableStore(),
    lambda: SQLiteTableStore(":memory:"),
])
def test_coverage_metadata_accuracy_with_rejected_rows_above_5000_rows(store_factory):
    """Verify rejection tracking and incomplete coverage flag on 5,200 rows with 200 malformed rows."""
    # Generate 5,200 rows with every 26th row malformed -> exactly 200 malformed rows
    csv_path = generate_multiyear_csv(num_rows=5200, malformed_interval=26)
    try:
        store = store_factory()
        ws = "ws_coverage_metadata"
        doc_id = "doc_malformed_5200"

        res = ingest_csv_to_table_store(
            path=csv_path,
            document_id=doc_id,
            workspace_id=ws,
            store=store,
            batch_size=99,
            table_name="imperfect_table",
        )

        assert res.source_rows == 5200
        assert res.persisted_rows == 5000
        assert res.rejected_rows == 200
        assert res.coverage_complete is False
        assert len(res.rejection_reasons) == 100  # Capped at 100 error messages
        assert store.has_coverage_complete_table("imperfect_table", ws) is False

        manifest = store.get_ingestion_manifest(doc_id, ws)
        assert manifest is not None
        assert manifest["coverage_complete"] is False
        assert int(manifest["persisted_rows"]) == 5000
        assert int(manifest["source_rows"]) == 5200
        assert int(manifest["rejected_rows"]) == 200

    finally:
        csv_path.unlink(missing_ok=True)


def test_refusal_of_complete_dataset_aggregates_when_coverage_incomplete():
    """Verify structured aggregates fail closed when table coverage is incomplete."""
    store = get_shared_table_store(reset=True)
    ws = "ws_refusal_incomplete"
    doc_id = "doc_incomplete"

    schema = TableSchema(
        columns=(
            ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=1, name="Value", inferred_dtype=InferredDtype.DECIMAL),
        ),
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )
    store.store_schema("survey_incomplete", ws, schema, doc_id, "v1")
    store.publish_active_version(
        "survey_incomplete",
        ws,
        doc_id,
        "v1",
        manifest={"coverage_complete": False, "source_rows": 6000, "persisted_rows": 5000, "rejected_rows": 1000},
    )

    # 1. Direct service execution raises IncompleteDataError
    svc = StructuredQueryService(store)
    with pytest.raises(IncompleteDataError) as exc_info:
        svc.execute("What is the earliest and latest Year in survey_incomplete.csv?", ws)
    assert "incomplete" in str(exc_info.value).lower()

    # 2. Pipeline execution through QueryService returns structured refusal
    qs = QueryService()
    req = QueryRequest(query="What is the earliest and latest Year in survey_incomplete.csv?", workspace_id=ws)
    res = qs.execute_query(req)

    assert "incomplete" in res["answer"].lower()
    assert res["status"] == "error"
    assert res["error_code"] == "incomplete_data"
    assert res["executed_path"] == "structured_aggregate_incomplete"
    assert res["citations"] == []
    assert res["confidence"] == 0.0

    # 3. ProductionExecutionEngine returns INSUFFICIENT_COVERAGE outcome
    engine = ProductionExecutionEngine(query_service=qs)
    outcome = engine.execute_query(workspace_id=ws, query_text="What is the total Value in survey_incomplete.csv?")
    assert outcome.status == "INSUFFICIENT_COVERAGE"
    assert outcome.citations == ()


def test_workspace_boundary_isolation_above_5000_rows():
    """Verify complete isolation between workspaces storing distinct tables above 5,000 rows."""
    csv_alpha = generate_multiyear_csv(num_rows=5500, years=[2015, 2016, 2017, 2018, 2019])
    csv_beta = generate_multiyear_csv(num_rows=5500, years=[2021, 2022, 2023, 2024, 2025])

    try:
        store = MemoryTableStore()
        ws_a = "ws_isolated_alpha"
        ws_b = "ws_isolated_beta"

        ingest_csv_to_table_store(csv_alpha, "doc_alpha", ws_a, store, batch_size=99, table_name="finances")
        ingest_csv_to_table_store(csv_beta, "doc_beta", ws_b, store, batch_size=99, table_name="finances")

        svc = StructuredQueryService(store)

        res_a = svc.execute("What is the earliest and latest Year in finances?", workspace_id=ws_a)
        res_b = svc.execute("What is the earliest and latest Year in finances?", workspace_id=ws_b)

        assert res_a is not None and res_b is not None
        # Workspace Alpha reflects only 2015-2019
        assert res_a.result_value == 2015
        assert res_a.secondary_value == 2019
        assert res_a.selection_count == 5500
        assert res_a.total_rows_in_table == 5500
        assert res_a.workspace_id == ws_a

        # Workspace Beta reflects only 2021-2025
        assert res_b.result_value == 2021
        assert res_b.secondary_value == 2025
        assert res_b.selection_count == 5500
        assert res_b.total_rows_in_table == 5500
        assert res_b.workspace_id == ws_b

        # Assert no cross-workspace leakage:
        # 1. Total row count across each workspace is exactly 5500, not 11000
        assert res_a.total_rows_in_table == 5500
        assert res_b.total_rows_in_table == 5500
        # 2. Querying workspace with no tables returns None
        assert svc.execute("What is the earliest and latest Year in finances?", workspace_id="ws_empty") is None
        # 3. Document scoping isolates tables within workspace
        assert svc.execute("What is the earliest and latest Year in finances?", workspace_id=ws_a, document_ids=["doc_beta"]) is None
        # 4. TableStore workspace isolation
        ingest_csv_to_table_store(csv_alpha, "doc_alpha_private", ws_a, store, batch_size=99, table_name="alpha_private")
        assert store.get_active_version("alpha_private", ws_b) is None
        assert "alpha_private" not in store.list_tables(ws_b)

    finally:
        csv_alpha.unlink(missing_ok=True)
        csv_beta.unlink(missing_ok=True)


def test_document_scoping_isolation():
    """Verify document_ids filter scopes table discovery and excludes non-matching documents."""
    csv_path = generate_multiyear_csv(num_rows=200, years=[2022, 2023])
    try:
        store = MemoryTableStore()
        ws = "ws_scoping"

        ingest_csv_to_table_store(csv_path, "doc_target", ws, store, table_name="target_table")
        ingest_csv_to_table_store(csv_path, "doc_other", ws, store, table_name="other_table")

        svc = StructuredQueryService(store)

        # Scoping to matching document_id succeeds
        res_match = svc.execute("What is the earliest and latest Year in target_table?", ws, document_ids=["doc_target"])
        assert res_match is not None
        assert res_match.document_id == "doc_target"

        # Scoping to non-matching document_id returns None
        res_none = svc.execute("What is the earliest and latest Year in target_table?", ws, document_ids=["doc_unrelated"])
        assert res_none is None

    finally:
        csv_path.unlink(missing_ok=True)


def test_dynamodb_paginated_reads_above_5000_rows_across_pages():
    """Verify DynamoDBTableStore.execute_single_pass_query consumes all pages via LastEvaluatedKey."""
    mock_resource = MagicMock()
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_resource.Table.return_value = mock_table

    store = DynamoDBTableStore(
        table_name="test-kre-table",
        resource=mock_resource,
        client=mock_client,
    )

    # Mock get_active_version
    mock_table.get_item.return_value = {
        "Item": {
            "active_version": "v_paginated",
            "coverage_complete": True,
        }
    }

    # Simulate 6 paginated query responses of 1,000 rows each (total 6,000 rows)
    total_simulated_rows = 6000
    page_size = 1000

    def make_items_page(start_idx: int, count: int) -> list[dict[str, Any]]:
        items = []
        for i in range(start_idx, start_idx + count):
            r_idx = i + 1
            yr = 2020 + (i % 6)
            cells = [
                TableCell(
                    cell_id=f"tab:{r_idx}:0",
                    row_index=r_idx,
                    col_index=0,
                    coordinate=f"R{r_idx}C1",
                    raw_value=str(yr),
                    normalized_value=Decimal(str(yr)),
                    inferred_dtype=InferredDtype.INTEGER,
                ),
                TableCell(
                    cell_id=f"tab:{r_idx}:1",
                    row_index=r_idx,
                    col_index=1,
                    coordinate=f"R{r_idx}C2",
                    raw_value=str(100),
                    normalized_value=Decimal("100"),
                    inferred_dtype=InferredDtype.DECIMAL,
                ),
            ]
            import json
            from db.table_store.dynamodb_store import _cells_to_json
            items.append({
                "PK": "TABLE#ws_page#tab#V#v_paginated",
                "SK": f"ROW#{r_idx:010d}",
                "row_id": f"row_{r_idx}",
                "row_index": Decimal(str(r_idx)),
                "cells_json": _cells_to_json(cells),
                "is_header": False,
            })
        return items

    query_responses = []
    for p in range(0, total_simulated_rows, page_size):
        has_next = (p + page_size < total_simulated_rows)
        resp: dict[str, Any] = {
            "Items": make_items_page(p, page_size),
        }
        if has_next:
            resp["LastEvaluatedKey"] = {"PK": "TABLE#ws_page#tab#V#v_paginated", "SK": f"ROW#{(p + page_size):010d}"}
        query_responses.append(resp)

    mock_table.query.side_effect = query_responses

    min_val, count, matched_ids, matched_idx, digest = store.execute_single_pass_query(
        table_id="tab",
        workspace_id="ws_page",
        column_index=0,
        op=AggregateOp.MIN,
        predicates=[],
    )

    assert count == 6000
    assert len(matched_ids) == 6000
    assert len(matched_idx) == 6000
    assert min_val == Decimal("2020")
    assert mock_table.query.call_count == 6


def test_real_survay_csv_complete_dataset_validation():
    """Verify that TableStore ingests all 60,255 rows and all 13 years (2013-2025) from actual survay.csv."""
    survay_path = Path(__file__).parent / "data" / "survay.csv"
    if not survay_path.exists():
        pytest.skip("survay.csv not available for offline test")

    store = MemoryTableStore()
    ws = "ws_real_survay_validation"
    doc_id = "doc_survay_actual"

    res = ingest_csv_to_table_store(
        path=survay_path,
        document_id=doc_id,
        workspace_id=ws,
        store=store,
        batch_size=10000,
    )

    assert res.source_rows == 60255
    assert res.persisted_rows == 60255
    assert res.rejected_rows == 0
    assert res.coverage_complete is True

    svc = StructuredQueryService(store)

    # Range across all 13 years: confirms presence of 2013 through 2025
    q_range = "What is the earliest and latest Year actually present in survay.csv?"
    res_range = svc.execute(q_range, workspace_id=ws)
    assert res_range is not None
    assert res_range.result_value == 2013
    assert res_range.secondary_value == 2025
    assert res_range.selection_count == 60255
    assert res_range.total_rows_in_table == 60255
    assert res_range.answer_text == "2013 and 2025."

    # Delta for 99999 Total income between 2024 and 2025
    q_delta = "For All industries (99999), how did Total income change from 2024 to 2025 in survay.csv?"
    res_delta = svc.execute(q_delta, workspace_id=ws)
    assert res_delta is not None
    assert res_delta.result_value == Decimal("4191")
    assert abs(res_delta.secondary_value - Decimal("0.43")) <= Decimal("0.02")
    assert "4,191" in res_delta.answer_text
    assert "0.43%" in res_delta.answer_text
    assert ("fell" in res_delta.answer_text.lower() or "decline" in res_delta.answer_text.lower())
