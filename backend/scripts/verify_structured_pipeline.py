"""End-to-End Fresh-Process Ingestion & Structured Query Verification Script.

Proves:
1. Production ingestion orchestration ingests 60,255 rows into persistent SQLiteTableStore with zero rejections and complete coverage.
2. Fresh query-service process reads persisted tables across process boundary.
3. Query 1 (min/max range) deterministically executes with 0 remote LLM/embedding calls, returning 2013 and 2025.
4. Query 2 (metric delta) deterministically executes with 0 remote LLM/embedding calls, returning -4,191 (-0.43%).
5. Citations validate against retained evidence items with full schema/hash binding.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def run_process_1_ingest(db_path: str, workspace_id: str, csv_path: Path) -> dict:
    """Process 1: Ingest survay.csv using production csv_table_ingester into SQLiteTableStore."""
    print("=" * 70)
    print("PROCESS 1: Ingesting survay.csv into persistent SQLiteTableStore")
    print(f"Database: {db_path}")
    print(f"Workspace: {workspace_id}")
    print(f"Source CSV: {csv_path}")
    print("=" * 70)

    # We run ingestion directly or via subprocess to ensure clean environment
    os.environ["KRE_TABLE_STORE_BACKEND"] = "sqlite"
    os.environ["KRE_TABLE_STORE_SQLITE_PATH"] = db_path

    # Set up Python path
    backend_src = str(Path(__file__).resolve().parent.parent / "src")
    if backend_src not in sys.path:
        sys.path.insert(0, backend_src)

    from db.table_store import get_shared_table_store
    from ingestion.csv_table_ingester import ingest_csv_to_table_store

    store = get_shared_table_store(require_durable=True)
    t0 = time.perf_counter()
    result = ingest_csv_to_table_store(
        path=csv_path,
        document_id="doc_survay",
        workspace_id=workspace_id,
        store=store,
    )
    elapsed = time.perf_counter() - t0

    manifest = store.get_ingestion_manifest("doc_survay", workspace_id)
    summary = {
        "document_id": result.document_id,
        "document_version": result.document_version,
        "table_id": result.table_id,
        "source_rows": result.source_rows,
        "persisted_rows": result.persisted_rows,
        "rejected_rows": result.rejected_rows,
        "coverage_complete": result.coverage_complete,
        "content_hash": result.content_hash,
        "elapsed_seconds": round(elapsed, 2),
        "manifest_persisted_rows": manifest.get("persisted_rows") if manifest else None,
        "manifest_coverage_complete": manifest.get("coverage_complete") if manifest else None,
    }

    print(json.dumps(summary, indent=2))
    assert result.source_rows == 60255, f"Expected 60255 source rows, got {result.source_rows}"
    assert result.persisted_rows == 60255, f"Expected 60255 persisted rows, got {result.persisted_rows}"
    assert result.rejected_rows == 0, f"Expected 0 rejected rows, got {result.rejected_rows}"
    assert result.coverage_complete is True, "Expected coverage_complete=True"
    print("\n[PROCESS 1 SUCCESS] All ingestion assertions verified.")
    return summary


def run_process_2_queries(db_path: str, workspace_id: str) -> None:
    """Process 2: Spawned in a completely fresh OS process to execute queries and assert results."""
    print("\n" + "=" * 70)
    print("PROCESS 2: Launching fresh-process query execution via subprocess")
    print("=" * 70)

    runner_code = f"""
import json
import os
import sys
from pathlib import Path

backend_src = '{Path(__file__).resolve().parent.parent / "src"}'
sys.path.insert(0, backend_src)

from db.table_store import get_shared_table_store
from modules.query.query_service import QueryService
from schemas.models import QueryRequest
from services.evaluation.benchmark_scorer import validate_citations
from services.langgraph_pipeline import pipeline

store = get_shared_table_store(require_durable=True)
db_path = getattr(store, "absolute_db_path", str(store))
tables = store.list_tables("{workspace_id}")
print(f"FRESH_PROCESS: Connected to {{db_path}}, found tables: {{tables}}")
assert "survay" in tables, f"Expected 'survay' table in {{tables}}"

qs = QueryService()

# -------------------------------------------------------------
# Query 1: Earliest and latest year
# -------------------------------------------------------------
q1_text = "What is the earliest and latest Year actually present in survay.csv?"
req1 = QueryRequest(query=q1_text, workspace_id="{workspace_id}")
res1 = qs.execute_query(req1)

print("\\n--- Query 1 Result ---")
print(f"Answer: {{res1.get('answer')}}")
print(f"Retrieval path: {{res1.get('retrieval_path')}}")
print(f"Bedrock calls: {{res1.get('bedrock_embedding_calls')}}")
print(f"Generation calls: {{res1.get('generation_calls')}}")

assert "2013" in res1["answer"], f"Expected 2013 in answer: {{res1['answer']}}"
assert "2025" in res1["answer"], f"Expected 2025 in answer: {{res1['answer']}}"
assert res1["retrieval_path"] == "structured_aggregate"
assert res1["bedrock_embedding_calls"] == 0
assert res1["generation_calls"] == 0

# Validate Q1 citations
pipe_res1 = pipeline.run(query=q1_text, workspace_id="{workspace_id}")
val1 = validate_citations(pipe_res1.citations, retained_evidence_items=pipe_res1.retained_evidence_items)
print(f"Citations valid: {{val1['valid']}}, count: {{val1['valid_count']}}, errors: {{val1['errors']}}")
assert val1["valid"] is True
assert val1["valid_count"] == 1
assert len(val1["errors"]) == 0

# -------------------------------------------------------------
# Query 2: Metric delta (Total income change 2024 to 2025 for 99999)
# -------------------------------------------------------------
q2_text = "For All industries (99999), how did total income change from 2024 to 2025? Give the amount and percentage change."
req2 = QueryRequest(query=q2_text, workspace_id="{workspace_id}")
res2 = qs.execute_query(req2)

print("\\n--- Query 2 Result ---")
print(f"Answer: {{res2.get('answer')}}")
print(f"Retrieval path: {{res2.get('retrieval_path')}}")
print(f"Bedrock calls: {{res2.get('bedrock_embedding_calls')}}")
print(f"Generation calls: {{res2.get('generation_calls')}}")

assert "4,191" in res2["answer"] or "4191" in res2["answer"], f"Expected 4,191 in answer: {{res2['answer']}}"
assert "980,268" in res2["answer"] or "980268" in res2["answer"], f"Expected 980,268 in answer: {{res2['answer']}}"
assert "976,077" in res2["answer"] or "976077" in res2["answer"], f"Expected 976,077 in answer: {{res2['answer']}}"
assert "0.43%" in res2["answer"], f"Expected 0.43% in answer: {{res2['answer']}}"
assert res2["retrieval_path"] == "structured_aggregate"
assert res2["bedrock_embedding_calls"] == 0
assert res2["generation_calls"] == 0

# Validate Q2 citations
pipe_res2 = pipeline.run(query=q2_text, workspace_id="{workspace_id}")
val2 = validate_citations(pipe_res2.citations, retained_evidence_items=pipe_res2.retained_evidence_items)
print(f"Citations valid: {{val2['valid']}}, count: {{val2['valid_count']}}, errors: {{val2['errors']}}")
assert val2["valid"] is True
assert val2["valid_count"] == 1
assert len(val2["errors"]) == 0

print("\\n[PROCESS 2 SUCCESS] All fresh-process queries and citation verifications passed!")
"""

    env = dict(os.environ)
    env["KRE_TABLE_STORE_BACKEND"] = "sqlite"
    env["KRE_TABLE_STORE_SQLITE_PATH"] = db_path

    cmd = [sys.executable, "-c", runner_code]
    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    print(proc.stdout)


def main():
    repo_root = Path(__file__).resolve().parent.parent.parent
    csv_path = repo_root / "data" / "non_pdf_formats" / "survay.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Source file not found at {csv_path}")

    workspace_id = f"ws_structured_audit_{int(time.time())}"

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "table_store.db")
        print(f"Running pipeline verification with durable SQLite store at: {db_path}")

        # Step 1: Ingest
        ingest_summary = run_process_1_ingest(db_path, workspace_id, csv_path)

        # Step 2: Fresh Process Queries
        run_process_2_queries(db_path, workspace_id)

    print("\n" + "=" * 70)
    print("ALL STRUCTURED PIPELINE VERIFICATIONS PASSED SUCCESSFULLY.")
    print("=" * 70)


if __name__ == "__main__":
    main()
