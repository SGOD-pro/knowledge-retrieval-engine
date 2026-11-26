"""Tests for production pipeline routing over structured table storage."""

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import pytest

from config import settings
from db.table_store import get_shared_table_store
from ingestion.csv_table_ingester import ingest_csv_to_table_store
from modules.query.query_service import QueryRequest, QueryService
from schemas.structured_table import ColumnDefinition, HeaderTopology, InferredDtype, StructuredTable, TableSchema
from services.langgraph_pipeline import pipeline
from services.retrieval.planner import planner
from services.telemetry import init_request_telemetry


@pytest.fixture(autouse=True)
def configure_test_environment(monkeypatch):
    """Ensure clean in-memory test substitutes and test mode settings."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "test")
    store = get_shared_table_store(reset=True)
    yield store
    get_shared_table_store(reset=True)


@pytest.fixture
def survay_path() -> Path:
    p = Path(__file__).parent / "data" / "survay.csv"
    if not p.exists():
        pytest.skip("survay.csv not found")
    return p


@pytest.fixture
def populated_workspace(survay_path: Path) -> tuple[str, str]:
    ws = "ws_pipeline_structured"
    doc_id = "doc_pipeline_survay"
    store = get_shared_table_store()
    ingest_csv_to_table_store(
        path=survay_path,
        document_id=doc_id,
        workspace_id=ws,
        store=store,
        batch_size=10000,
    )
    man = store.get_ingestion_manifest(doc_id, ws)
    if man and not man.get("coverage_complete"):
        man["coverage_complete"] = True
        store.publish_active_version("survay", ws, doc_id, man["document_version"], man)
    return ws, doc_id


def test_planner_structured_aggregate_route():
    q152 = "What is the earliest and latest Year actually present in survay.csv?"
    plan152 = planner.route(q152)
    assert plan152.use_structured_aggregate is True
    assert plan152.stages == ["structured_aggregate"]

    q153 = "For All industries (99999), how did total income change from 2024 to 2025? Give the amount and percentage change."
    plan153 = planner.route(q153)
    assert plan153.use_structured_aggregate is True
    assert plan153.stages == ["structured_aggregate"]


def test_pipeline_no_embedding_call_for_structured(populated_workspace):
    ws, doc_id = populated_workspace
    init_request_telemetry()

    with patch("providers.embedding_provider.embed_text") as mock_embed:
        res = pipeline.run(
            query="What is the earliest and latest Year actually present in survay.csv?",
            workspace_id=ws,
            document_ids=[doc_id],
        )

        # Assert remote embedding was never called for structured aggregate
        assert mock_embed.call_count == 0
        assert res.executed_path == "structured_aggregate"
        assert res.structured_aggregate is True
        assert "2013" in res.answer and "2025" in res.answer


def test_pipeline_structured_executes_deterministically(populated_workspace):
    ws, doc_id = populated_workspace
    init_request_telemetry()

    service = QueryService()
    req = QueryRequest(
        query="For All industries (99999), how did total income change from 2024 to 2025? Give the amount and percentage change.",
        workspace_id=ws,
        document_ids=[doc_id],
    )
    res = service.execute(req)

    assert res["executed_path"] == "structured_aggregate"
    assert res["structured_aggregate"] is True
    assert res["generation_calls"] == 0
    assert res["structured_aggregate_calls"] >= 1
    assert "4,191" in res["answer"]
    assert "0.43%" in res["answer"]
    assert len(res["citations"]) >= 1
    assert res["citations"][0]["evidence_type"] == "structured_aggregate"
    assert res["citations"][0]["selection_count"] == 2
    assert len(res["citations"][0]["selection_hash"]) == 16


def test_pipeline_incomplete_data_blocks_generation(populated_workspace):
    ws, doc_id = populated_workspace
    store = get_shared_table_store()

    # Artificially set coverage_complete=False
    man = store.get_ingestion_manifest(doc_id, ws)
    man["coverage_complete"] = False
    store.publish_active_version("survay", ws, doc_id, man["document_version"], man)

    init_request_telemetry()
    service = QueryService()
    req = QueryRequest(
        query="What is the earliest and latest Year actually present in survay.csv?",
        workspace_id=ws,
        document_ids=[doc_id],
    )
    try:
        res = service.execute(req)
        # Incomplete data must explicitly terminate and NOT fall through to generative LLM
        assert res["executed_path"] == "structured_aggregate_incomplete"
        assert res["status"] == "error"
        assert res["error_code"] == "incomplete_data"
        assert res["generation_calls"] == 0
        assert "incomplete" in res["answer"].lower()
    finally:
        man["coverage_complete"] = True
        store.publish_active_version("survay", ws, doc_id, man["document_version"], man)


def test_pipeline_multi_table_workspace_q153_without_document_ids(survay_path: Path):
    ws = "ws_pipeline_multi_table"
    store = get_shared_table_store()

    # Ingest distractor table first
    rs_path = Path(__file__).parent.parent.parent / "data" / "non_pdf_formats" / "rs_status_bill_passed_assent-1952-2016.csv"
    if rs_path.exists():
        if not store.has_coverage_complete_table("rs_status_bill_passed_assent_1952_2016", ws):
            ingest_csv_to_table_store(
                path=rs_path,
                table_name="rs_status_bill_passed_assent_1952_2016",
                document_id="doc_pipeline_rs",
                workspace_id=ws,
                store=store,
                batch_size=10000,
            )
    else:
        if not store.has_coverage_complete_table("rs_status_bill_passed_assent_1952_2016", ws):
            cols = (
                ColumnDefinition(col_index=0, name="Year", inferred_dtype=InferredDtype.INTEGER),
                ColumnDefinition(col_index=1, name="Bill_Title", inferred_dtype=InferredDtype.STRING),
            )
            schema = TableSchema(columns=cols, header_rows=(0,), header_topology=HeaderTopology.SINGLE_ROW, confidence=1.0)
            table = StructuredTable(table_id="rs_status_bill_passed_assent_1952_2016", document_id="doc_pipeline_rs", sheet_name=None, page_number=None, schema=schema, rows=())
            store.store_table(table, ws)
            store.publish_active_version("rs_status_bill_passed_assent_1952_2016", ws, "doc_pipeline_rs", "v1", {"coverage_complete": True, "source_rows": 0, "persisted_rows": 0})

    # Ingest survay second
    if not store.has_coverage_complete_table("survay", ws):
        ingest_csv_to_table_store(
            path=survay_path,
            table_name="survay",
            document_id="doc_pipeline_survay_multi",
            workspace_id=ws,
            store=store,
            batch_size=10000,
        )
        man = store.get_ingestion_manifest("doc_pipeline_survay_multi", ws)
        if man and not man.get("coverage_complete"):
            man["coverage_complete"] = True
            store.publish_active_version("survay", ws, "doc_pipeline_survay_multi", man["document_version"], man)

    init_request_telemetry()
    service = QueryService()
    # Query without document_ids: table binding must resolve to survay
    req = QueryRequest(
        query="For All industries (99999), how did total income change from 2024 to 2025? Give the amount and percentage change.",
        workspace_id=ws,
        document_ids=None,
    )
    res = service.execute(req)

    assert res["executed_path"] == "structured_aggregate"
    assert res["structured_aggregate"] is True
    assert res["generation_calls"] == 0
    assert res["structured_aggregate_calls"] >= 1
    assert "4,191" in res["answer"]
    assert "0.43%" in res["answer"]
    assert len(res["citations"]) >= 1
    assert res["citations"][0]["table_id"] == "survay"


def test_structured_aggregate_citation_provenance_resolves_registered_filename(tmp_path, monkeypatch):
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from schemas.contracts.boundary import DocumentSnapshot
    from services.evaluation.benchmark_scorer import evaluate_grounded_answer
    import services.retrieval.run_structured_aggregate as rsa

    csv_path = tmp_path / "sales_metrics_tbl.csv"
    csv_path.write_text("Year,Region,Revenue\n2024,North,1000\n2025,North,1250\n", encoding="utf-8")

    ws = "ws_synth_prov"
    doc_id = "doc_synth_01"
    registered_filename = "annual_metrics_2026.csv"
    store = get_shared_table_store()
    ingest_csv_to_table_store(
        path=csv_path,
        document_id=doc_id,
        workspace_id=ws,
        store=store,
        batch_size=100,
    )
    man = store.get_ingestion_manifest(doc_id, ws)
    assert man is not None
    doc_version = man["document_version"]
    man["coverage_complete"] = True
    store.publish_active_version("sales_metrics_tbl", ws, doc_id, doc_version, man)

    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws,
            document_id=doc_id,
            source=registered_filename,
            version=doc_version,
            format="csv",
            incomplete_coverage_flag=False,
            persisted_rows=2,
            total_rows=2,
            created_at="2026-11-25T00:00:00Z",
        )
    )
    monkeypatch.setattr(rsa, "get_workspace_boundary_repo", lambda: boundary_repo, raising=False)

    init_request_telemetry()
    res = pipeline.run(
        query="What is the earliest and latest Year in annual_metrics_2026.csv?",
        workspace_id=ws,
        document_ids=[doc_id],
    )

    assert res.executed_path == "structured_aggregate"
    assert res.citations[0]["table_id"] == "sales_metrics_tbl"
    assert res.citations[0]["document_filename"] == registered_filename
    assert res.retained_evidence_items[0].citation_payload["document_filename"] == registered_filename
    assert res.retained_evidence_items[0].provenance["document_filename"] == registered_filename

    score_ok = evaluate_grounded_answer(
        answer=res.answer,
        citations=res.citations,
        is_answer_text_correct=True,
        question_type="structured_aggregate",
        expected_sources=[registered_filename],
        expected_source_version=doc_version,
        retained_evidence_items=res.retained_evidence_items,
    )
    assert score_ok["is_grounded_correct"] is True

    score_bad_ver = evaluate_grounded_answer(
        answer=res.answer,
        citations=res.citations,
        is_answer_text_correct=True,
        question_type="structured_aggregate",
        expected_sources=[registered_filename],
        expected_source_version="wrong_version",
        retained_evidence_items=res.retained_evidence_items,
    )
    assert score_bad_ver["is_grounded_correct"] is False

    score_bad_src = evaluate_grounded_answer(
        answer=res.answer,
        citations=res.citations,
        is_answer_text_correct=True,
        question_type="structured_aggregate",
        expected_sources=["other_file.csv"],
        expected_source_version=doc_version,
        retained_evidence_items=res.retained_evidence_items,
    )
    assert score_bad_src["is_grounded_correct"] is False


def test_structured_aggregate_citation_provenance_missing_and_mismatched_snapshot(tmp_path, monkeypatch):
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from schemas.contracts.boundary import DocumentSnapshot
    from services.evaluation.benchmark_scorer import evaluate_grounded_answer
    import services.retrieval.run_structured_aggregate as rsa

    csv_path = tmp_path / "sales_metrics_tbl.csv"
    csv_path.write_text("Year,Region,Revenue\n2024,North,1000\n2025,North,1250\n", encoding="utf-8")

    ws = "ws_synth_prov_neg"
    doc_id = "doc_synth_neg_01"
    registered_filename = "annual_metrics_2026.csv"
    store = get_shared_table_store()
    ingest_csv_to_table_store(
        path=csv_path,
        document_id=doc_id,
        workspace_id=ws,
        store=store,
        batch_size=100,
    )
    man = store.get_ingestion_manifest(doc_id, ws)
    assert man is not None
    doc_version = man["document_version"]
    man["coverage_complete"] = True
    store.publish_active_version("sales_metrics_tbl", ws, doc_id, doc_version, man)

    # Case 1: Missing workspace snapshot -> filename must remain absent (not inferred from table_id)
    empty_repo = InMemoryWorkspaceBoundaryRepository()
    monkeypatch.setattr(rsa, "get_workspace_boundary_repo", lambda: empty_repo, raising=False)

    init_request_telemetry()
    res_missing = pipeline.run(
        query="What is the earliest and latest Year in sales_metrics_tbl?",
        workspace_id=ws,
        document_ids=[doc_id],
    )
    assert res_missing.executed_path == "structured_aggregate"
    assert "document_filename" not in res_missing.citations[0]
    assert "document_filename" not in res_missing.retained_evidence_items[0].citation_payload
    assert "document_filename" not in res_missing.retained_evidence_items[0].structured_payload
    assert "document_filename" not in res_missing.retained_evidence_items[0].provenance

    score_missing = evaluate_grounded_answer(
        answer=res_missing.answer,
        citations=res_missing.citations,
        is_answer_text_correct=True,
        question_type="structured_aggregate",
        expected_sources=[registered_filename],
        expected_source_version=doc_version,
        retained_evidence_items=res_missing.retained_evidence_items,
    )
    assert score_missing["is_grounded_correct"] is False

    # Case 2: Snapshot version differs from aggregate result -> filename must remain absent
    mismatch_repo = InMemoryWorkspaceBoundaryRepository()
    mismatch_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws,
            document_id=doc_id,
            source=registered_filename,
            version="stale_version_000",
            format="csv",
            incomplete_coverage_flag=False,
            persisted_rows=2,
            total_rows=2,
            created_at="2026-11-25T00:00:00Z",
        )
    )
    monkeypatch.setattr(rsa, "get_workspace_boundary_repo", lambda: mismatch_repo, raising=False)

    init_request_telemetry()
    res_mismatch = pipeline.run(
        query="What is the earliest and latest Year in sales_metrics_tbl?",
        workspace_id=ws,
        document_ids=[doc_id],
    )
    assert res_mismatch.executed_path == "structured_aggregate"
    assert "document_filename" not in res_mismatch.citations[0]
    assert "document_filename" not in res_mismatch.retained_evidence_items[0].citation_payload
    assert "document_filename" not in res_mismatch.retained_evidence_items[0].structured_payload
    assert "document_filename" not in res_mismatch.retained_evidence_items[0].provenance

    score_mismatch = evaluate_grounded_answer(
        answer=res_mismatch.answer,
        citations=res_mismatch.citations,
        is_answer_text_correct=True,
        question_type="structured_aggregate",
        expected_sources=[registered_filename],
        expected_source_version=doc_version,
        retained_evidence_items=res_mismatch.retained_evidence_items,
    )
    assert score_mismatch["is_grounded_correct"] is False


def test_structured_aggregate_stage_timings_recorded(populated_workspace):
    ws, doc_id = populated_workspace
    init_request_telemetry()
    res = pipeline.run(
        query="What is the earliest and latest Year actually present in survay.csv?",
        workspace_id=ws,
        document_ids=[doc_id],
    )
    assert res.executed_path == "structured_aggregate"
    timings = res.stage_timings
    assert "table_lookup_ms" in timings
    assert "row_reads_ms" in timings
    assert "aggregate_ms" in timings
    assert "computation_ms" in timings
    assert "structured_aggregate_ms" in timings
    assert timings["table_lookup_ms"] >= 0.0
    assert timings["row_reads_ms"] >= 0.0
    assert timings["aggregate_ms"] >= 0.0
    assert timings["computation_ms"] >= 0.0
    assert timings["structured_aggregate_ms"] >= 0.0


def test_structured_aggregate_citation_provenance_missing_versions(tmp_path, monkeypatch):
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from schemas.contracts.boundary import DocumentSnapshot
    from services.evaluation.benchmark_scorer import evaluate_grounded_answer
    import services.retrieval.run_structured_aggregate as rsa

    csv_path = tmp_path / "sales_metrics_tbl.csv"
    csv_path.write_text("Year,Region,Revenue\n2024,North,1000\n2025,North,1250\n", encoding="utf-8")

    ws = "ws_synth_prov_ver"
    doc_id = "doc_synth_ver_01"
    registered_filename = "annual_metrics_2026.csv"
    store = get_shared_table_store()
    ingest_csv_to_table_store(
        path=csv_path,
        document_id=doc_id,
        workspace_id=ws,
        store=store,
        batch_size=100,
    )
    man = store.get_ingestion_manifest(doc_id, ws)
    assert man is not None
    doc_version = man["document_version"]
    man["coverage_complete"] = True
    store.publish_active_version("sales_metrics_tbl", ws, doc_id, doc_version, man)

    # Case 1: Snapshot has empty/missing version while aggregate result has doc_version
    no_snap_ver_repo = InMemoryWorkspaceBoundaryRepository()
    no_snap_ver_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws,
            document_id=doc_id,
            source=registered_filename,
            version="",
            format="csv",
            incomplete_coverage_flag=False,
            persisted_rows=2,
            total_rows=2,
            created_at="2026-11-25T00:00:00Z",
        )
    )
    monkeypatch.setattr(rsa, "get_workspace_boundary_repo", lambda: no_snap_ver_repo, raising=False)

    init_request_telemetry()
    res_no_snap_ver = pipeline.run(
        query="What is the earliest and latest Year in sales_metrics_tbl?",
        workspace_id=ws,
        document_ids=[doc_id],
    )
    assert res_no_snap_ver.executed_path == "structured_aggregate"
    assert "document_filename" not in res_no_snap_ver.citations[0]
    assert "document_filename" not in res_no_snap_ver.retained_evidence_items[0].citation_payload
    assert "document_filename" not in res_no_snap_ver.retained_evidence_items[0].structured_payload
    assert "document_filename" not in res_no_snap_ver.retained_evidence_items[0].provenance

    score_no_snap_ver = evaluate_grounded_answer(
        answer=res_no_snap_ver.answer,
        citations=res_no_snap_ver.citations,
        is_answer_text_correct=True,
        question_type="structured_aggregate",
        expected_sources=[registered_filename],
        expected_source_version=doc_version,
        retained_evidence_items=res_no_snap_ver.retained_evidence_items,
    )
    assert score_no_snap_ver["is_grounded_correct"] is False

    # Case 2: Snapshot has doc_version while aggregate result has missing/empty version
    valid_repo = InMemoryWorkspaceBoundaryRepository()
    valid_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws,
            document_id=doc_id,
            source=registered_filename,
            version=doc_version,
            format="csv",
            incomplete_coverage_flag=False,
            persisted_rows=2,
            total_rows=2,
            created_at="2026-11-25T00:00:00Z",
        )
    )
    monkeypatch.setattr(rsa, "get_workspace_boundary_repo", lambda: valid_repo, raising=False)

    orig_execute = rsa.StructuredQueryService.execute

    def _execute_missing_ver(self, *args, **kwargs):
        res = orig_execute(self, *args, **kwargs)
        if res is not None:
            res.document_version = ""
        return res

    monkeypatch.setattr(rsa.StructuredQueryService, "execute", _execute_missing_ver)

    init_request_telemetry()
    res_no_agg_ver = pipeline.run(
        query="What is the earliest and latest Year in sales_metrics_tbl?",
        workspace_id=ws,
        document_ids=[doc_id],
    )
    assert res_no_agg_ver.executed_path == "structured_aggregate"
    assert "document_filename" not in res_no_agg_ver.citations[0]
    assert "document_filename" not in res_no_agg_ver.retained_evidence_items[0].citation_payload
    assert "document_filename" not in res_no_agg_ver.retained_evidence_items[0].structured_payload
    assert "document_filename" not in res_no_agg_ver.retained_evidence_items[0].provenance


def test_premise_comparison_routes_to_evidence_path_while_sum_and_delta_stay_structured(populated_workspace, monkeypatch):
    """Q155 regression: premise comparisons must bypass structured_aggregate, while SUM and delta stay structured."""
    import json
    from db.workspace_boundary_repo import DocumentSnapshot, InMemoryWorkspaceBoundaryRepository
    from schemas.models import Chunk
    from services.retrieval import bm25_retriever
    from services.retrieval.strategy_router import StrategyRouter

    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    ws, doc_id = populated_workspace

    c1 = Chunk(
        id=f"{doc_id}:csv:row1",
        document_id=doc_id,
        source_format="csv",
        text="Year: 2025. Industry: All industries (99999). Variable: Total income. Value: 979702.",
        element_type="table_row",
        page_number=None,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )
    c2 = Chunk(
        id=f"{doc_id}:csv:row2",
        document_id=doc_id,
        source_format="csv",
        text="Year: 2025. Industry: All industries (99999). Variable: Total expenditure. Value: 871375. Surplus before income tax: 109848.",
        element_type="table_row",
        page_number=None,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )

    class _PremiseDocRepo:
        def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
            return {"documents": [{"id": doc_id, "filename": "survay.csv"}]}

        def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
            return [c1, c2]

        def get(self, d_id: str):
            return None

        def search_vector(self, *args, **kwargs):
            return []

        def check_semantic_cache(self, *args, **kwargs):
            return None

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Premise WS", "owner-1", workspace_id=ws)
    b_repo.create_source(ws, "survay.csv", source_id=doc_id)
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws,
            document_id=doc_id,
            source="survay.csv",
            version="v1",
            format="csv",
            created_at="2026-11-26T00:00:00Z",
        )
    )
    doc_repo = _PremiseDocRepo()
    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()

    # 1. Premise comparison through normal QueryService entry point
    premise_q = "For 2025 All industries, surplus before income tax is just total income minus total expenditure, right? Check against the CSV."
    qs = QueryService(repo=doc_repo)
    llm_payload = json.dumps({
        "answer": "No, total income minus total expenditure is 108,327, while reported surplus before income tax is 109,848.",
        "citations": [c1.id, c2.id],
    })
    init_request_telemetry()
    with (
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
        patch("services.langgraph_pipeline.check_fidelity", return_value=True),
        patch("services.llm.llm_service.generate_completion", return_value=(llm_payload, {"input_tokens": 10, "output_tokens": 10})),
    ):
        res_premise = qs.execute_query(
            QueryRequest(
                query=premise_q,
                workspace_id=ws,
                document_ids=[doc_id],
                benchmark_mode=True,
                cache=False,
            )
        )

    assert res_premise["planned_path"] != "structured_aggregate"
    assert res_premise["executed_path"] != "structured_aggregate"
    assert res_premise["structured_aggregate"] is False
    assert "109,848" in res_premise["answer"]

    # 2. Controls: StrategyRouter and QueryService preserve legitimate SUM and numeric delta comparisons
    router = StrategyRouter()
    assert "structured_table" in router.route("What is the total Value for 2025 in survay.csv?").primary
    assert "structured_table" in router.route("For All industries (99999), how did total income change from 2024 to 2025?").primary

    with patch("services.langgraph_pipeline._get_repo", return_value=doc_repo):
        res_delta = qs.execute_query(
            QueryRequest(
                query="For All industries (99999), how did total income change from 2024 to 2025? Give the amount and percentage change.",
                workspace_id=ws,
                document_ids=[doc_id],
                benchmark_mode=True,
                cache=False,
            )
        )
    assert res_delta["executed_path"] == "structured_aggregate"
    assert res_delta["structured_aggregate"] is True





