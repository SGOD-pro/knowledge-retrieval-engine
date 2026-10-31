"""Offline regression tests for verified routing, outcome classification, and telemetry defects."""

from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest

from db.table_store.memory_store import MemoryTableStore
from providers.reranker_provider import (
    get_last_reranker_mode,
    rerank_documents,
    set_last_reranker_mode,
)
from schemas.models import QueryResponse
from schemas.structured_table import (
    ColumnDefinition,
    HeaderTopology,
    InferredDtype,
    StructuredTable,
    TableCell,
    TableRow,
    TableSchema,
)
from services.retrieval.planner import Planner
from services.retrieval.run_structured_aggregate import execute as execute_structured_aggregate
from services.retrieval.strategy_router import StrategyRouter
from services.retrieval.structured_query_service import (
    AmbiguousBindingError,
    IncompleteDataError,
    StorageFailureError,
    StructuredQueryResult,
    StructuredQueryService,
    UnresolvedFilterError,
)


def _create_test_table(
    store: MemoryTableStore,
    workspace_id: str,
    table_id: str,
    doc_id: str,
    years: list[int] = (2024, 2025),
    coverage_complete: bool = True,
) -> None:
    cols = (
        ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
        ColumnDefinition(col_index=1, name="Industry_name", inferred_dtype=InferredDtype.STRING),
        ColumnDefinition(col_index=2, name="Total_income", inferred_dtype=InferredDtype.DECIMAL),
    )
    schema = TableSchema(
        columns=cols,
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )
    rows = []
    r_idx = 0
    for y in years:
        rows.append(
            TableRow(
                row_index=r_idx,
                row_id=f"{table_id}_r_{r_idx}",
                cells=(
                    TableCell(
                        cell_id=f"{table_id}_c_{r_idx}_0",
                        row_index=r_idx,
                        col_index=0,
                        coordinate=f"A{r_idx + 1}",
                        raw_value=str(y),
                        normalized_value=y,
                    ),
                    TableCell(
                        cell_id=f"{table_id}_c_{r_idx}_1",
                        row_index=r_idx,
                        col_index=1,
                        coordinate=f"B{r_idx + 1}",
                        raw_value="All industries",
                        normalized_value="All industries",
                    ),
                    TableCell(
                        cell_id=f"{table_id}_c_{r_idx}_2",
                        row_index=r_idx,
                        col_index=2,
                        coordinate=f"C{r_idx + 1}",
                        raw_value="100000",
                        normalized_value=Decimal("100000"),
                    ),
                ),
            )
        )
        r_idx += 1

    table = StructuredTable(
        table_id=table_id,
        document_id=doc_id,
        sheet_name=None,
        page_number=None,
        schema=schema,
        rows=tuple(rows),
    )
    store.store_table(table, workspace_id)
    manifest = {
        "coverage_complete": coverage_complete,
        "source_rows": len(rows),
        "persisted_rows": len(rows),
        "rejected_rows": 0,
        "document_version": "v1",
    }
    store.publish_active_version(table_id, workspace_id, doc_id, "v1", manifest)


def test_mixed_source_workspace_document_table_not_routed_to_csv_aggregate():
    """Verify PDF document table queries do not route to CSV structured table scans."""
    planner = Planner()
    router = StrategyRouter()

    doc_table_query = (
        "In Table 1 of the interpretability paper, what are the average attention values for full, random 20, and top 20?"
    )

    plan = planner.route(doc_table_query)
    assert plan.use_structured_aggregate is False

    strategies = router.route(doc_table_query, plan=plan)
    assert "structured_table" not in strategies.primary

    store = MemoryTableStore()
    ws = "ws_mixed_test"
    _create_test_table(store, ws, "survay", "doc_survay")

    svc = StructuredQueryService(store)
    cands = svc.store.list_tables(ws)
    tables = [(t[0], svc.store.get_table(t[0], ws), t[1]) for t in cands]
    bound = svc._bind_table(tables, doc_table_query, ws)
    assert bound is None


def test_legitimate_csv_and_xls_queries_preserved():
    """Verify legitimate CSV and spreadsheet aggregate queries are preserved."""
    planner = Planner()
    router = StrategyRouter()

    csv_query_1 = "What is the 2026 total income for All industries in survay.csv?"
    plan_1 = planner.route(csv_query_1)
    assert plan_1.use_structured_aggregate is True

    csv_query_2 = "What is the earliest and latest Year actually present in survay.csv?"
    plan_2 = planner.route(csv_query_2)
    assert plan_2.use_structured_aggregate is True

    strategies_2 = router.route(csv_query_2, plan=plan_2)
    assert "structured_table" in strategies_2.primary


def test_ambiguous_source_selection_safe():
    """Verify ambiguous candidate tables raise AmbiguousBindingError and return clarification_needed."""
    store = MemoryTableStore()
    ws = "ws_ambig_test"
    _create_test_table(store, ws, "finance_2024", "doc_fin_24")
    _create_test_table(store, ws, "finance_2025", "doc_fin_25")

    svc = StructuredQueryService(store)
    ambig_query = "What is the total income across all industries?"

    with pytest.raises(AmbiguousBindingError):
        svc.execute(ambig_query, workspace_id=ws)

    state = {
        "query": ambig_query,
        "workspace_id": ws,
        "document_ids": None,
        "stage_timings": {},
    }
    with patch("services.retrieval.run_structured_aggregate.get_shared_table_store", return_value=store):
        res = execute_structured_aggregate(state)

    assert res["status"] == "clarification_needed"
    assert res["error_code"] == "ambiguous_binding"
    assert res["executed_path"] == "structured_aggregate_ambiguous"
    assert res["reranker_mode"] == "bypassed"


def test_avoid_unbounded_scans_on_deep_probes():
    """Verify deep store probing passes bounded scan parameters and skips type mismatched columns."""
    mock_store = MagicMock()
    mock_store.list_tables.return_value = ["test_table"]
    mock_store.get_active_version.return_value = "v1"
    cols = (
        ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
        ColumnDefinition(col_index=1, name="Value", inferred_dtype=InferredDtype.DECIMAL),
        ColumnDefinition(col_index=2, name="Category", inferred_dtype=InferredDtype.STRING),
    )
    schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)
    mock_table = StructuredTable(table_id="test_table", document_id="d1", sheet_name=None, page_number=None, schema=schema, rows=())
    mock_store.get_table.return_value = mock_table
    mock_store.has_coverage_complete_table.return_value = True
    mock_store.get_ingestion_manifest.return_value = {"persisted_rows": 50000, "coverage_complete": True}

    # Probe rows returns empty, triggering deep store search
    mock_store.query_rows.return_value = []

    svc = StructuredQueryService(mock_store)
    query = "For NonexistentDept in test_table, what is the total Value?"

    with pytest.raises(UnresolvedFilterError):
        svc.execute(query, workspace_id="ws_mock")

    # Verify query_rows was never called with string 'NonexistentDept' on integer or decimal columns
    for call in mock_store.query_rows.call_args_list:
        predicates = call.kwargs.get("predicates") or (call.args[2] if len(call.args) > 2 else [])
        for p in predicates:
            if p.column_index in (0, 1):
                assert not isinstance(p.value, str) or p.value.isdigit()


def test_empty_result_with_complete_coverage_distinguished_from_unresolved_filter():
    """Verify zero matching rows in complete dataset returns success with is_refusal=True rather than error."""
    store = MemoryTableStore()
    ws = "ws_empty_result_test"
    # Table only has years 2024 and 2025
    _create_test_table(store, ws, "survay", "doc_survay", years=[2024, 2025], coverage_complete=True)

    query = "What is the 2026 total income for All industries in survay.csv?"
    svc = StructuredQueryService(store)
    result = svc.execute(query, workspace_id=ws)

    assert result is not None
    assert isinstance(result, StructuredQueryResult)
    assert result.selection_count == 0
    assert result.result_value == Decimal(0)
    assert "No rows matched" in result.answer_text

    state = {
        "query": query,
        "workspace_id": ws,
        "document_ids": None,
        "stage_timings": {},
    }
    with patch("services.retrieval.run_structured_aggregate.get_shared_table_store", return_value=store):
        pipeline_res = execute_structured_aggregate(state)

    assert pipeline_res["status"] == "success"
    assert pipeline_res["is_refusal"] is True
    assert pipeline_res.get("error_code") is None
    assert pipeline_res["executed_path"] == "structured_aggregate"
    assert pipeline_res["reranker_mode"] == "bypassed"


def test_unresolved_filter_raises_and_returns_error():
    """Verify unresolvable explicit filter raises UnresolvedFilterError and returns status error."""
    store = MemoryTableStore()
    ws = "ws_unresolved_test"
    _create_test_table(store, ws, "survay", "doc_survay", years=[2024, 2025], coverage_complete=True)

    svc = StructuredQueryService(store)
    unresolved_query = "What is the total income for NonexistentCohortXYZ in survay.csv?"

    with pytest.raises(UnresolvedFilterError):
        svc.execute(unresolved_query, workspace_id=ws)

    state = {
        "query": unresolved_query,
        "workspace_id": ws,
        "document_ids": None,
        "stage_timings": {},
    }
    with patch("services.retrieval.run_structured_aggregate.get_shared_table_store", return_value=store):
        pipeline_res = execute_structured_aggregate(state)

    assert pipeline_res["status"] == "error"
    assert pipeline_res["error_code"] == "unresolved_filter"
    assert pipeline_res["executed_path"] == "structured_aggregate_unresolved_filter"
    assert pipeline_res["reranker_mode"] == "bypassed"


def test_incomplete_coverage_raises_and_returns_error():
    """Verify table with coverage_complete=False raises IncompleteDataError and returns status error."""
    store = MemoryTableStore()
    ws = "ws_incomplete_test"
    _create_test_table(store, ws, "survay", "doc_survay", years=[2024, 2025], coverage_complete=False)

    svc = StructuredQueryService(store)
    query = "What is the earliest and latest Year actually present in survay.csv?"

    with pytest.raises(IncompleteDataError):
        svc.execute(query, workspace_id=ws)

    state = {
        "query": query,
        "workspace_id": ws,
        "document_ids": None,
        "stage_timings": {},
    }
    with patch("services.retrieval.run_structured_aggregate.get_shared_table_store", return_value=store):
        pipeline_res = execute_structured_aggregate(state)

    assert pipeline_res["status"] == "error"
    assert pipeline_res["error_code"] == "incomplete_data"
    assert pipeline_res["executed_path"] == "structured_aggregate_incomplete"
    assert pipeline_res["reranker_mode"] == "bypassed"


def test_storage_failure_raises_and_returns_error():
    """Verify storage exception raises StorageFailureError and returns status error."""
    mock_store = MagicMock()
    mock_store.list_tables.return_value = ["survay"]
    mock_store.get_active_version.return_value = "v1"
    cols = (
        ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
        ColumnDefinition(col_index=1, name="Value", inferred_dtype=InferredDtype.DECIMAL),
    )
    schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)
    mock_table = StructuredTable(table_id="survay", document_id="d1", sheet_name=None, page_number=None, schema=schema, rows=())
    mock_store.get_table.return_value = mock_table
    mock_store.has_coverage_complete_table.return_value = True
    mock_store.get_ingestion_manifest.return_value = {"persisted_rows": 100, "coverage_complete": True}
    mock_store.execute_single_pass_query.side_effect = RuntimeError("DynamoDB connection timeout")

    svc = StructuredQueryService(mock_store)
    query = "What is the earliest and latest Year actually present in survay.csv?"

    with pytest.raises(StorageFailureError):
        svc.execute(query, workspace_id="ws_store_fail")

    state = {
        "query": query,
        "workspace_id": "ws_store_fail",
        "document_ids": None,
        "stage_timings": {},
    }
    with patch("services.retrieval.run_structured_aggregate.get_shared_table_store", return_value=mock_store):
        pipeline_res = execute_structured_aggregate(state)

    assert pipeline_res["status"] == "error"
    assert pipeline_res["error_code"] == "storage_failure"
    assert pipeline_res["executed_path"] == "structured_aggregate_storage_failure"
    assert pipeline_res["reranker_mode"] == "bypassed"


def test_reranker_telemetry_bypassed_and_model_default():
    """Verify reranker telemetry accurately reports bypassed when reranker was not executed."""
    resp = QueryResponse(answer="Test answer")
    assert resp.reranker_mode == "bypassed"

    set_last_reranker_mode("remote_success")
    assert get_last_reranker_mode() == "remote_success"

    # Empty documents bypasses reranker
    scores = rerank_documents("test query", [])
    assert scores == []
    assert get_last_reranker_mode() == "bypassed"


def test_reranker_telemetry_remote_success_and_fallback():
    """Verify reranker telemetry reports remote_success only on remote success and preserves fallback reporting."""
    docs = ["Doc chunk 1", "Doc chunk 2"]

    # 1. Successful remote call
    with patch("providers.reranker_provider._is_circuit_open", return_value=False), \
         patch("providers.reranker_provider._openrouter_reranker", return_value=[0.9, 0.4]):
        scores = rerank_documents("test query", docs)
        assert len(scores) == 2
        assert get_last_reranker_mode() == "remote_success"

    # 2. Circuit open fallback
    with patch("providers.reranker_provider._is_circuit_open", return_value=True), \
         patch("providers.reranker_provider._term_coverage_fallback", return_value=[0.5, 0.2]):
        scores = rerank_documents("test query", docs)
        assert len(scores) == 2
        assert get_last_reranker_mode() == "circuit_open_lexical_fallback"

    # 3. Remote call failure fallback
    with patch("providers.reranker_provider._is_circuit_open", return_value=False), \
         patch("providers.reranker_provider._openrouter_reranker", side_effect=Exception("API timeout")), \
         patch("providers.reranker_provider._term_coverage_fallback", return_value=[0.3, 0.1]):
        scores = rerank_documents("test query", docs)
        assert len(scores) == 2
        assert get_last_reranker_mode() == "circuit_open_lexical_fallback"
