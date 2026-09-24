"""Tests for production pipeline routing over structured table storage."""

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import pytest

from db.table_store import get_shared_table_store
from ingestion.csv_table_ingester import ingest_csv_to_table_store
from modules.query.query_service import QueryRequest, QueryService
from services.langgraph_pipeline import pipeline
from services.retrieval.planner import planner
from services.telemetry import init_request_telemetry


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
    res = service.execute(req)

    # Incomplete data must explicitly terminate and NOT fall through to generative LLM
    assert res["executed_path"] == "structured_aggregate_incomplete"
    assert res["status"] == "error"
    assert res["error_code"] == "incomplete_data"
    assert res["generation_calls"] == 0
    assert "incomplete" in res["answer"].lower()
