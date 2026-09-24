"""End-to-end integration tests proving process boundary and table store integration."""

import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

from aws.infra import _is_floci_available
from config import settings
from db.table_store import get_shared_table_store
from ingestion.csv_table_ingester import ingest_csv_to_table_store
from modules.query.query_service import QueryRequest, QueryService


@pytest.fixture
def survay_path() -> Path:
    p = Path(__file__).parent / "data" / "survay.csv"
    if not p.exists():
        pytest.skip("survay.csv not found")
    return p


def test_fresh_process_query_service_execution(survay_path: Path, tmp_path: Path):
    """Proves persistence across process boundaries using a fresh subprocess with no shared memory."""
    # 1. Ingest via SQLite backend to a persistent file to prove fresh process restart
    sqlite_db = tmp_path / "table_store.db"
    ws = "ws_fresh_process"
    doc_id = "doc_fresh_survay"

    # Pre-ingest in a separate first process
    ingest_script = f"""
import sys
from pathlib import Path
from db.table_store.sqlite_store import SQLiteTableStore
from ingestion.csv_table_ingester import ingest_csv_to_table_store

store = SQLiteTableStore('{sqlite_db}')
res = ingest_csv_to_table_store(
    path=Path('{survay_path}'),
    document_id='{doc_id}',
    workspace_id='{ws}',
    store=store,
    batch_size=5000,
)
print('INGESTED', res.persisted_rows, res.coverage_complete)
"""
    p1 = subprocess.run(
        [sys.executable, "-c", ingest_script],
        cwd=str(Path(__file__).parent.parent),
        env={**os.environ, "PYTHONPATH": "src", "ENVIRONMENT": "test"},
        capture_output=True,
        text=True,
    )
    assert p1.returncode == 0, f"Ingestion process failed: {p1.stderr}"
    assert "INGESTED 60255 True" in p1.stdout

    # 2. Query in a SECOND completely fresh subprocess, with response caches disabled
    query_script = f"""
import sys
from db.table_store.sqlite_store import SQLiteTableStore
from services.retrieval.structured_query_service import StructuredQueryService
from services.evaluation.benchmark_scorer import validate_citations
from services.retrieval.run_structured_aggregate import build_structured_evidence_ref

store = SQLiteTableStore('{sqlite_db}')
svc = StructuredQueryService(store)
res = svc.execute('What is the earliest and latest Year actually present in survay.csv?', workspace_id='{ws}')

assert res is not None
assert res.answer_text == '2013 and 2025.'
assert res.result_value == 2013
assert res.secondary_value == 2025

cit = build_structured_evidence_ref(res, '{ws}')
val = validate_citations([cit])
assert val['valid'] is True, f"Citation validation failed: {{val['errors']}}"
print('QUERY_SUCCESS', res.answer_text, cit['selection_hash'])
"""
    p2 = subprocess.run(
        [sys.executable, "-c", query_script],
        cwd=str(Path(__file__).parent.parent),
        env={**os.environ, "PYTHONPATH": "src", "ENVIRONMENT": "test"},
        capture_output=True,
        text=True,
    )
    assert p2.returncode == 0, f"Query process failed: {p2.stderr}"
    assert "QUERY_SUCCESS 2013 and 2025." in p2.stdout


@pytest.mark.integration
def test_localstack_dynamodb_e2e(survay_path: Path):
    """End-to-end integration test against LocalStack / FLOCI DynamoDB emulator."""
    if not _is_floci_available():
        pytest.skip(
            "LocalStack emulator is not running on localhost:4566. "
            "Labeled emulator result: LocalStack unavailable in current test environment."
        )

    # Note: LocalStack is an emulator, not AWS production DynamoDB
    logger_label = "LocalStack (Emulator)"
    ws = "ws_localstack_e2e"
    doc_id = "doc_localstack_survay"

    from db.table_store.dynamodb_store import DynamoDBTableStore
    from aws.infra import setup_infrastructure

    # Ensure DynamoDB tables exist in LocalStack
    setup_infrastructure()
    store = DynamoDBTableStore()

    # Production ingestion entry point
    res = ingest_csv_to_table_store(
        path=survay_path,
        document_id=doc_id,
        workspace_id=ws,
        store=store,
        batch_size=99,
    )
    assert res.persisted_rows == 60255
    assert res.coverage_complete is True

    # Execute in a fresh QueryService instance with cache disabled
    service = QueryService()
    req = QueryRequest(
        query="What is the earliest and latest Year actually present in survay.csv?",
        workspace_id=ws,
        document_ids=[doc_id],
        use_cache=False,
    )
    response = service.execute(req)

    assert response["answer"] == "2013 and 2025."
    assert response["executed_path"] == "structured_aggregate"
    assert response["structured_aggregate"] is True
    assert response["generation_calls"] == 0
    assert response["structured_aggregate_calls"] == 1
    assert len(response["citations"]) >= 1

    cit = response["citations"][0]
    assert cit["evidence_type"] == "structured_aggregate"
    assert cit["operator"] == "range"
    assert cit["target_column"].lower() == "year"
    assert cit["selection_count"] == 60255
    assert len(cit["selection_hash"]) == 16
