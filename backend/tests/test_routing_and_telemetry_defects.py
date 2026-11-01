"""Offline regression tests for verified routing, outcome classification, and telemetry defects."""

import concurrent.futures
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest

from db.table_store.memory_store import MemoryTableStore
from db.table_store.sqlite_store import SQLiteTableStore
from providers.reranker_provider import (
    get_last_reranker_mode,
    rerank_documents,
    set_last_reranker_mode,
)
from schemas.models import QueryResponse, RerankerMode
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


def _create_large_test_table(
    store,
    workspace_id: str,
    table_id: str,
    doc_id: str,
    num_rows: int,
    target_row_index: int | None = None,
    target_name: str = "SpecialEntity",
    coverage_complete: bool = True,
) -> None:
    cols = (
        ColumnDefinition(col_index=0, name="ID", inferred_dtype=InferredDtype.INTEGER),
        ColumnDefinition(col_index=1, name="Category", inferred_dtype=InferredDtype.STRING),
        ColumnDefinition(col_index=2, name="Amount", inferred_dtype=InferredDtype.DECIMAL),
    )
    schema = TableSchema(
        columns=cols,
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )
    rows = []
    for r_idx in range(num_rows):
        cat_val = target_name if target_row_index is not None and r_idx == target_row_index else "Standard"
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
                        raw_value=str(r_idx),
                        normalized_value=r_idx,
                    ),
                    TableCell(
                        cell_id=f"{table_id}_c_{r_idx}_1",
                        row_index=r_idx,
                        col_index=1,
                        coordinate=f"B{r_idx + 1}",
                        raw_value=cat_val,
                        normalized_value=cat_val,
                    ),
                    TableCell(
                        cell_id=f"{table_id}_c_{r_idx}_2",
                        row_index=r_idx,
                        col_index=2,
                        coordinate=f"C{r_idx + 1}",
                        raw_value="100.0",
                        normalized_value=Decimal("100.0"),
                    ),
                ),
            )
        )
    table = StructuredTable(
        table_id=table_id,
        document_id=doc_id,
        sheet_name=None,
        page_number=None,
        schema=schema,
        rows=tuple(rows),
        row_count=len(rows),
        col_count=3,
    )
    store.store_table(table, workspace_id)
    if hasattr(store, "store_schema"):
        store.store_schema(table_id, workspace_id, schema, doc_id, "v1")
    if hasattr(store, "write_row_batch_with_checkpoint"):
        store.write_row_batch_with_checkpoint(
            table_id=table_id,
            workspace_id=workspace_id,
            document_id=doc_id,
            version="v1",
            rows=list(rows),
            checkpoint_batch=0,
            source_rows=len(rows),
            attempted_rows=len(rows),
            persisted_rows=len(rows),
            rejected_rows=0,
            parser_version="1.0.0",
            schema_version="",
            content_hash="test_content_hash",
        )
    manifest = {
        "coverage_complete": coverage_complete,
        "source_rows": len(rows),
        "persisted_rows": len(rows),
        "rejected_rows": 0,
        "document_version": "v1",
    }
    store.publish_active_version(table_id, workspace_id, doc_id, "v1", manifest)


def test_scan_pagination_boundaries_memory_and_sqlite(tmp_path):
    """Verify scan pagination boundaries at exactly 500 rows and 501 rows for Memory and SQLite stores."""
    sqlite_path = str(tmp_path / "pagination_test.db")
    stores = [
        MemoryTableStore(),
        SQLiteTableStore(sqlite_path),
    ]

    for store in stores:
        ws = f"ws_boundary_{type(store).__name__}"

        # 1. Exactly 500 rows: scan limit 500 should complete without truncation
        _create_large_test_table(store, ws, "tab_500", "doc_500", num_rows=500)
        res_500 = store.query_rows("tab_500", ws, max_scan_items=500)
        assert len(res_500) == 500
        assert res_500.is_truncated is False
        assert res_500.scanned_count == 500

        # 2. 501 rows: scan limit 500 should report truncation
        _create_large_test_table(store, ws, "tab_501", "doc_501", num_rows=501)
        res_501 = store.query_rows("tab_501", ws, max_scan_items=500)
        assert len(res_501) == 500
        assert res_501.is_truncated is True
        assert res_501.scanned_count == 500


def test_match_after_row_500_truncated_scan_falls_back_safely():
    """Verify entity past row 500 triggers truncated scan reporting and safe fallback without false refusal."""
    store = MemoryTableStore()
    ws = "ws_scan_truncation"

    # Table with 600 rows where RareEntity only exists at row 550
    _create_large_test_table(
        store,
        ws,
        "deep_table",
        "doc_deep",
        num_rows=600,
        target_row_index=550,
        target_name="RareEntity",
    )

    svc = StructuredQueryService(store)
    query_past_limit = "What is the total Amount for RareEntity in deep_table?"

    # Direct service call must raise UnresolvedFilterError with was_truncated=True
    with pytest.raises(UnresolvedFilterError) as exc_info:
        svc.execute(query_past_limit, workspace_id=ws)
    assert exc_info.value.was_truncated is True

    # Pipeline node must catch truncated unresolved filter and safely route to fallback
    state = {
        "query": query_past_limit,
        "workspace_id": ws,
        "document_ids": None,
        "stage_timings": {},
    }
    with patch("services.retrieval.run_structured_aggregate.get_shared_table_store", return_value=store):
        pipeline_res = execute_structured_aggregate(state)

    assert pipeline_res["executed_path"] == "structured_aggregate_fallback"
    assert pipeline_res["reranker_mode"] == RerankerMode.BYPASSED.value
    assert "structured_aggregate_ms" in pipeline_res["stage_timings"]

    # Contrast: when matching entity is within the first 500 rows, probe finds it and query succeeds
    _create_large_test_table(
        store,
        ws,
        "matched_table",
        "doc_matched",
        num_rows=600,
        target_row_index=250,
        target_name="EarlyEntity",
    )
    query_within_limit = "What is the total Amount for EarlyEntity in matched_table?"
    match_result = svc.execute(query_within_limit, workspace_id=ws)
    assert match_result.selection_count == 1
    assert match_result.result_value == Decimal("100.0")


def test_concurrent_reranker_telemetry_isolation():
    """Verify concurrent requests retain isolated reranker telemetry per thread without cross-talk."""
    import threading
    import time

    def mock_is_circuit_open(*args, **kwargs) -> bool:
        tname = threading.current_thread().name
        return "fallback" in tname

    def mock_openrouter(query: str, documents: list[str]) -> list[float]:
        return [0.95] * len(documents)

    def worker(worker_type: str) -> tuple[str, str]:
        threading.current_thread().name = f"thread_{worker_type}_{threading.get_ident()}"
        if worker_type == "remote":
            docs = ["chunk 1", "chunk 2"]
            rerank_documents("sample query", docs)
            time.sleep(0.01)
            return worker_type, get_last_reranker_mode()
        elif worker_type == "fallback":
            docs = ["chunk 1", "chunk 2"]
            rerank_documents("sample query", docs)
            time.sleep(0.01)
            return worker_type, get_last_reranker_mode()
        else:
            rerank_documents("sample query", [])
            time.sleep(0.01)
            return worker_type, get_last_reranker_mode()

    task_types = ["remote", "fallback", "bypassed"] * 10
    with patch("providers.reranker_provider._is_circuit_open", side_effect=mock_is_circuit_open), \
         patch("providers.reranker_provider._openrouter_reranker", side_effect=mock_openrouter), \
         patch("providers.reranker_provider._term_coverage_fallback", return_value=[0.35, 0.25]):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(worker, task_types))

    for expected_type, recorded_mode in outcomes:
        if expected_type == "remote":
            assert recorded_mode == RerankerMode.REMOTE_SUCCESS.value
        elif expected_type == "fallback":
            assert recorded_mode == RerankerMode.CIRCUIT_OPEN_LEXICAL_FALLBACK.value
        else:
            assert recorded_mode == RerankerMode.BYPASSED.value


def test_empty_aggregate_semantics_min_max_avg_count_sum():
    """Verify empty aggregate results: count/sum are 0, avg/min/max are None (undefined), with empty citations."""
    store = MemoryTableStore()
    ws = "ws_empty_aggregates"
    _create_test_table(store, ws, "survay", "doc_survay", years=[2024], coverage_complete=True)

    svc = StructuredQueryService(store)

    # 1. COUNT for nonexistent year returns Decimal(0)
    res_count = svc.execute("What is the count of records for 2026 in survay.csv?", workspace_id=ws)
    assert res_count.selection_count == 0
    assert res_count.result_value == Decimal(0)

    # 2. SUM for nonexistent year returns Decimal(0)
    res_sum = svc.execute("What is the total income for 2026 in survay.csv?", workspace_id=ws)
    assert res_sum.selection_count == 0
    assert res_sum.result_value == Decimal(0)

    # 3. AVG for nonexistent year returns None
    res_avg = svc.execute("What is the average income for 2026 in survay.csv?", workspace_id=ws)
    assert res_avg.selection_count == 0
    assert res_avg.result_value is None
    assert "undefined" in res_avg.answer_text.lower()

    # 4. MIN for nonexistent year returns None
    res_min = svc.execute("What is the minimum income for 2026 in survay.csv?", workspace_id=ws)
    assert res_min.selection_count == 0
    assert res_min.result_value is None
    assert "undefined" in res_min.answer_text.lower()

    # 5. MAX for nonexistent year returns None
    res_max = svc.execute("What is the maximum income for 2026 in survay.csv?", workspace_id=ws)
    assert res_max.selection_count == 0
    assert res_max.result_value is None
    assert "undefined" in res_max.answer_text.lower()

    # 6. Pipeline execution returns refusal with empty citations and retained evidence
    state = {
        "query": "What is the average income for 2026 in survay.csv?",
        "workspace_id": ws,
        "document_ids": None,
        "stage_timings": {},
    }
    with patch("services.retrieval.run_structured_aggregate.get_shared_table_store", return_value=store):
        pipeline_res = execute_structured_aggregate(state)

    assert pipeline_res["status"] == "success"
    assert pipeline_res["is_refusal"] is True
    assert pipeline_res["execution_result"].result_value is None
    assert pipeline_res["citations"] == []
    assert pipeline_res["retained_evidence_items"] == []
    assert pipeline_res["reranker_mode"] == RerankerMode.BYPASSED.value


def test_mixed_source_and_document_content_routing():
    """Verify document table references bypass structured table scans while legitimate report aggregates route properly."""
    planner = Planner()

    # Academic/paper table and figure references should bypass structured table aggregate
    q1 = "In Table 1 of the interpretability paper, what are the average attention values?"
    assert planner.route(q1).use_structured_aggregate is False

    q2 = "According to Table 2 of the vision transformer paper, what is the accuracy?"
    assert planner.route(q2).use_structured_aggregate is False

    q3 = "In Figure 3 of the paper, what is the loss trend?"
    assert planner.route(q3).use_structured_aggregate is False

    # Legitimate aggregate queries mentioning reports or workbooks route to structured table
    q4 = "What is the total expenditure in the annual financial report table?"
    assert planner.route(q4).use_structured_aggregate is True

    q5 = "What is the average salary in the salary survey workbook?"
    assert planner.route(q5).use_structured_aggregate is True

    q6 = "What is the maximum revenue in survay.csv?"
    assert planner.route(q6).use_structured_aggregate is True


def test_workspace_isolation_table_store():
    """Verify tables in workspace A are completely inaccessible from workspace B."""
    store = MemoryTableStore()
    _create_test_table(store, "ws_alpha", "financial_records", "doc_fin_alpha")

    # In ws_alpha, table exists
    assert store.get_table("financial_records", "ws_alpha") is not None
    assert "financial_records" in store.list_tables("ws_alpha")

    # In ws_beta, table does not exist
    assert store.get_table("financial_records", "ws_beta") is None
    assert store.list_tables("ws_beta") == []
    assert len(store.query_rows("financial_records", "ws_beta")) == 0

    # Querying from ws_beta cannot access ws_alpha data
    svc = StructuredQueryService(store)
    assert svc.execute("What is the total income in financial_records?", workspace_id="ws_beta") is None

