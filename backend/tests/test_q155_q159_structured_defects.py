"""In-process pipeline regressions for Q155 formula verification and Q159 absent-row refusal."""

from pathlib import Path
from unittest.mock import patch

import pytest

from db.table_store.memory_store import MemoryTableStore
from db.workspace_boundary_repo import DocumentSnapshot, InMemoryWorkspaceBoundaryRepository
from ingestion.csv_table_ingester import ingest_csv_to_table_store
from services.langgraph_pipeline import pipeline
from services.retrieval.structured_query_service import StructuredQueryService


def _setup_ledger(tmp_path: Path):
    csv_p = tmp_path / "synth_ledger.csv"
    csv_p.write_text(
        "Year,Industry,Variable,Value\n"
        "2025,All industries,Total income,979702\n"
        "2025,All industries,Total expenditure,871375\n"
        "2025,All industries,Surplus before income tax,109848\n"
        "2025,Manufacturing,Total income,250000\n",
        encoding="utf-8",
    )
    ws_id = "ws_q155_q159"
    doc_id = "doc_ledger"
    t_store = MemoryTableStore()
    manifest = ingest_csv_to_table_store(csv_p, document_id=doc_id, workspace_id=ws_id, store=t_store)
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Ledger WS", "usr_alice", workspace_id=ws_id)
    b_repo.create_source(ws_id, csv_p.name, source_id=doc_id)
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source=csv_p.name,
            version=manifest.document_version,
            format="csv",
            persisted_rows=4,
            total_rows=4,
            created_at="2026-11-28T00:00:00Z",
        )
    )
    return ws_id, doc_id, manifest.document_version, t_store, b_repo


def test_q155_formula_verification_binds_operands_and_checks_cell_deterministically(tmp_path: Path):
    ws_id, doc_id, doc_ver, t_store, b_repo = _setup_ledger(tmp_path)
    q = "For 2025 All industries, does total income minus total expenditure equal surplus before income tax in synth_ledger.csv?"

    with (
        patch("db.table_store.get_shared_table_store", return_value=t_store),
        patch("services.retrieval.run_structured_aggregate.get_shared_table_store", return_value=t_store),
        patch("services.retrieval.run_structured_aggregate.get_workspace_boundary_repo", return_value=b_repo),
        patch("services.llm.llm_service.generate_completion") as gen_mock,
    ):
        res = pipeline.run(
            query=q,
            document_ids=[doc_id],
            workspace_id=ws_id,
            benchmark_mode=True,
            boundary_repo=b_repo,
        )

    assert gen_mock.call_count == 0
    assert res.planned_path == "structured_aggregate"
    assert res.executed_path == "structured_aggregate"
    assert "108327" in res.answer or "108,327" in res.answer
    assert "109848" in res.answer or "109,848" in res.answer
    assert "1521" in res.answer or "1,521" in res.answer
    assert len(res.citations) == 1
    assert res.citations[0]["document_id"] == doc_id
    assert res.citations[0]["document_version"] == doc_ver
    assert res.citations[0]["selection_count"] == 3


def test_q155_premise_never_falls_back_to_blind_sum_in_structured_service(tmp_path: Path):
    ws_id, _, _, t_store, _ = _setup_ledger(tmp_path)
    svc = StructuredQueryService(t_store)
    q = "For 2025 All industries, surplus before income tax is just total income minus total expenditure, right? Check against the CSV."
    res = svc.execute(q, workspace_id=ws_id)
    assert res is not None
    assert res.operator != "sum"
    assert "108327" in res.answer_text or "108,327" in res.answer_text
    assert "109848" in res.answer_text or "109,848" in res.answer_text


def test_q159_absent_year_refuses_cleanly_with_zero_citations(tmp_path: Path):
    ws_id, doc_id, _, t_store, b_repo = _setup_ledger(tmp_path)
    q = "What is the 2026 Total income for All industries in synth_ledger.csv?"

    with (
        patch("db.table_store.get_shared_table_store", return_value=t_store),
        patch("services.retrieval.run_structured_aggregate.get_shared_table_store", return_value=t_store),
        patch("services.retrieval.run_structured_aggregate.get_workspace_boundary_repo", return_value=b_repo),
        patch("services.llm.llm_service.generate_completion") as gen_mock,
    ):
        res = pipeline.run(
            query=q,
            document_ids=[doc_id],
            workspace_id=ws_id,
            benchmark_mode=True,
            boundary_repo=b_repo,
        )

    assert gen_mock.call_count == 0
    assert res.planned_path == "structured_aggregate"
    assert res.executed_path == "structured_aggregate"
    assert res.citations == []
    assert "sum is 0" not in res.answer.lower()
    assert "does not contain matching rows" in res.answer.lower() or "no rows matched" in res.answer.lower()
