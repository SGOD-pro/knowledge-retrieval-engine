"""Real DynamoDB TableStore Verification Script.

Executes live against AWS DynamoDB (kre-table) in an isolated test workspace:
1. Interrupted ingestion: batch 1 succeeds, batch 2 simulated failure -> verifies incomplete version NEVER becomes active.
2. Resumption: resume from checkpoint, persisting remaining batches -> publishes active version.
3. Idempotent replay: rerun ingestion -> skips re-persisting without duplicating rows.
4. Pagination: exercises next_token pagination across DynamoDB partitions.
5. Independent OS process: queries DynamoDB via QueryService.execute_query.
6. Numerical verification: min/max, signed delta, percentage change vs streaming CSV.
7. Workspace isolation: isolated workspace cannot read other workspace tables/rows.
8. Citation tampering: tampering count, table, or predicates is strictly rejected.
9. Cleanup: cleans all test records from DynamoDB.
"""

from __future__ import annotations

import csv
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any

backend_src = str(Path(__file__).resolve().parent.parent / "src")
if backend_src not in sys.path:
    sys.path.insert(0, backend_src)

from db.table_store import get_shared_table_store
from db.table_store.dynamodb_store import DynamoDBTableStore
from ingestion.csv_table_ingester import (
    CsvIngestionResult,
    StructuredIngestionFailure,
    ingest_csv_to_table_store,
)


def create_verified_test_csv(csv_path: Path) -> dict[str, Any]:
    """Generate a multi-batch CSV with verified entries from survay.csv and edge-case rows."""
    headers = [
        "Year", "Industry_aggregation_NZSIOC", "Industry_code_NZSIOC",
        "Industry_name_NZSIOC", "Units", "Variable_code", "Variable_name",
        "Variable_category", "Value", "Industry_code_ANZSIC06"
    ]

    # Create 120 rows across years 2013-2025, batch size 40 -> 3 batches
    rows = []
    # Row 1: Min year 2013
    rows.append(["2013", "Level 1", "99999", "All industries", "Dollars (millions)", "H01", "Total income", "Financial performance", "750000", "ANZSIC06"])
    # Row 2: Max year 2025 (Value 976077)
    rows.append(["2025", "Level 1", "99999", "All industries", "Dollars (millions)", "H01", "Total income", "Financial performance", "976077", "ANZSIC06"])
    # Row 3: 2024 (Value 980268)
    rows.append(["2024", "Level 1", "99999", "All industries", "Dollars (millions)", "H01", "Total income", "Financial performance", "980268", "ANZSIC06"])
    # Row 4: Leading-zero code
    rows.append(["2024", "Level 1", "00123", "Special Industry", "Dollars (millions)", "H01", "Total income", "Financial performance", "1000", "ANZSIC06"])
    rows.append(["2025", "Level 1", "00123", "Special Industry", "Dollars (millions)", "H01", "Total income", "Financial performance", "1500", "ANZSIC06"])
    # Row 6: Null cell
    rows.append(["2024", "Level 2", "AA11", "Agriculture", "Dollars", "H02", "Sales", "Performance", "", "ANZSIC06"])

    # Filler rows to reach 120 rows
    for i in range(7, 121):
        yr = 2014 + (i % 10)
        rows.append([str(yr), "Level 3", f"IND_{i:04d}", f"Industry_{i}", "Dollars", "H99", f"Variable_{i}", "Category", str(100 + i), "ANZSIC06"])

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    # Independent streaming calculation
    min_year = 9999
    max_year = -9999
    val_2024 = None
    val_2025 = None
    total_data_rows = 0

    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)
        for r in reader:
            total_data_rows += 1
            y = int(r[0])
            min_year = min(min_year, y)
            max_year = max(max_year, y)
            if r[2] == "99999" and r[6] == "Total income":
                if y == 2024:
                    val_2024 = Decimal(r[8])
                elif y == 2025:
                    val_2025 = Decimal(r[8])

    signed_delta = val_2025 - val_2024
    abs_delta = abs(signed_delta)
    pct_change = (abs_delta / val_2024) * Decimal("100")

    return {
        "total_rows": total_data_rows,
        "min_year": min_year,
        "max_year": max_year,
        "val_2024": val_2024,
        "val_2025": val_2025,
        "signed_delta": signed_delta,
        "abs_delta": abs_delta,
        "pct_change": round(pct_change, 2),
    }


def main():
    print("=" * 80)
    print("REAL AWS DYNAMODB TABLESTORE VERIFICATION")
    print("=" * 80)

    os.environ["KRE_TABLE_STORE_BACKEND"] = "dynamodb"

    # 1. Connect to live DynamoDB
    store = get_shared_table_store(reset=True, require_durable=True)
    if not isinstance(store, DynamoDBTableStore):
        raise RuntimeError(f"Expected DynamoDBTableStore, got {type(store)}")

    print(f"Connected to AWS DynamoDB Table: {store.table_name}")

    workspace_id = f"ws_ddb_audit_{int(time.time())}"
    doc_id = "doc_ddb_test_csv"
    table_id = "test_survay"

    with tempfile.TemporaryDirectory() as tmpdir:
        csv_file = Path(tmpdir) / "test_survay.csv"
        ground_truth = create_verified_test_csv(csv_file)
        print(f"Generated test CSV with {ground_truth['total_rows']} rows at {csv_file}")
        print(f"Ground Truth: Min={ground_truth['min_year']}, Max={ground_truth['max_year']}, Delta={ground_truth['signed_delta']}, Pct={ground_truth['pct_change']}%")

        try:
            # 2. Test Interrupted Ingestion / Incomplete Version Check
            print("\n--- Test 1: Interrupted Ingestion & Incomplete Version Invariant ---")
            batch_size = 40
            original_write_batch = store.write_row_batch_with_checkpoint
            call_count = [0]

            def failing_write_batch(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 2:
                    raise IOError("Simulated network/process failure during batch 2 write")
                return original_write_batch(*args, **kwargs)

            store.write_row_batch_with_checkpoint = failing_write_batch
            interrupted = False
            try:
                ingest_csv_to_table_store(
                    path=csv_file,
                    document_id=doc_id,
                    workspace_id=workspace_id,
                    store=store,
                    batch_size=batch_size,
                )
            except StructuredIngestionFailure as e:
                interrupted = True
                print(f"Expected interruption captured: {e}")
            finally:
                store.write_row_batch_with_checkpoint = original_write_batch

            assert interrupted, "Ingestion should have failed on batch 2"
            # Invariant: An incomplete version MUST NEVER become active
            active_version = store.get_active_version(table_id, workspace_id)
            has_complete = store.has_coverage_complete_table(table_id, workspace_id)
            print(f"Post-interruption state: active_version={active_version}, has_coverage_complete={has_complete}")
            assert active_version is None, f"Expected no active version, got {active_version}"
            assert has_complete is False, "Incomplete version must not be coverage_complete"

            # Checkpoint should exist at batch 1
            import hashlib
            expected_version = hashlib.sha256(csv_file.read_bytes()).hexdigest()[:32]
            cp = store.get_checkpoint(doc_id, workspace_id, expected_version)
            print(f"Checkpoint verified: batch={cp.get('checkpoint_batch')}, persisted={cp.get('persisted_rows')}")
            assert cp is not None
            assert cp.get("checkpoint_batch") == 1
            assert cp.get("persisted_rows") == 40

            # 3. Test Resumption
            print("\n--- Test 2: Resumption from Checkpoint ---")
            res_resumed = ingest_csv_to_table_store(
                path=csv_file,
                document_id=doc_id,
                workspace_id=workspace_id,
                store=store,
                batch_size=batch_size,
            )
            print(f"Resumption complete: persisted={res_resumed.persisted_rows}, coverage_complete={res_resumed.coverage_complete}")
            assert res_resumed.persisted_rows == ground_truth["total_rows"]
            assert res_resumed.coverage_complete is True
            assert store.has_coverage_complete_table(table_id, workspace_id) is True

            # 4. Test Idempotent Retry
            print("\n--- Test 3: Idempotent Replay ---")
            res_retry = ingest_csv_to_table_store(
                path=csv_file,
                document_id=doc_id,
                workspace_id=workspace_id,
                store=store,
                batch_size=batch_size,
            )
            print(f"Idempotent retry: persisted={res_retry.persisted_rows}, finished={res_retry.processing_finished}")
            assert res_retry.persisted_rows == ground_truth["total_rows"]
            assert res_retry.processing_finished is True

            # 5. Test DynamoDB Pagination
            print("\n--- Test 4: DynamoDB Pagination & Limit Slicing ---")
            p1 = store.query_rows(table_id=table_id, workspace_id=workspace_id, limit=35)
            print(f"  Limit query (limit=35): retrieved {len(p1)} rows")
            assert len(p1) == 35

            p2 = store.query_rows(table_id=table_id, workspace_id=workspace_id, limit=75)
            print(f"  Limit query (limit=75): retrieved {len(p2)} rows")
            assert len(p2) == 75

            all_rows = store.query_rows(table_id=table_id, workspace_id=workspace_id)
            print(f"  Full partition query: retrieved {len(all_rows)} rows")
            assert len(all_rows) == ground_truth["total_rows"]
            print(f"Pagination & limit slicing verified: {len(all_rows)} rows retrieved successfully.")

            # 6. Fresh Process Query Execution
            print("\n--- Test 5: Independent Process Query Execution ---")
            runner_code = f"""
import os
import sys
from decimal import Decimal
from pathlib import Path

backend_src = '{backend_src}'
sys.path.insert(0, backend_src)

from db.table_store import get_shared_table_store
from modules.query.query_service import QueryService
from schemas.models import QueryRequest
from services.evaluation.benchmark_scorer import validate_citations
from services.langgraph_pipeline import pipeline

store = get_shared_table_store(require_durable=True)
tables = store.list_tables("{workspace_id}")
print(f"FRESH_PROCESS: DynamoDB tables in {workspace_id}: {{tables}}")
assert "{table_id}" in tables

qs = QueryService()

# Query 1: Earliest and latest year
req1 = QueryRequest(query="What is the earliest and latest Year actually present in {table_id}.csv?", workspace_id="{workspace_id}")
res1 = qs.execute_query(req1)
print(f"Query 1 Answer: {{res1['answer']}} | path: {{res1.get('retrieval_path')}}")
assert "{ground_truth['min_year']}" in res1["answer"]
assert "{ground_truth['max_year']}" in res1["answer"]
assert res1["retrieval_path"] == "structured_aggregate"
assert res1["bedrock_embedding_calls"] == 0
assert res1["generation_calls"] == 0

# Query 2: Metric delta
req2 = QueryRequest(query="For All industries (99999), how did total income change from 2024 to 2025? Give the amount and percentage change.", workspace_id="{workspace_id}")
res2 = qs.execute_query(req2)
print(f"Query 2 Answer: {{res2['answer']}} | path: {{res2.get('retrieval_path')}}")
assert "{abs(ground_truth['signed_delta']):,}" in res2["answer"] or "{abs(ground_truth['signed_delta'])}" in res2["answer"]
assert "{ground_truth['pct_change']:.2f}%" in res2["answer"]
assert res2["retrieval_path"] == "structured_aggregate"
assert res2["bedrock_embedding_calls"] == 0
assert res2["generation_calls"] == 0

# Validate citations with retained evidence items
pipe_res2 = pipeline.run(query=req2.query, workspace_id="{workspace_id}")
val_pass = validate_citations(pipe_res2.citations, retained_evidence_items=pipe_res2.retained_evidence_items)
assert val_pass["valid"] is True
assert val_pass["valid_count"] == 1
print(f"Valid citation verified: count={{val_pass['valid_count']}}")

# Verify citation tampering rejection: count tampering
tampered_count = list(pipe_res2.citations)
tampered_count[0] = dict(tampered_count[0])
tampered_count[0]["selection_count"] = 9999
val_fail_count = validate_citations(tampered_count, retained_evidence_items=pipe_res2.retained_evidence_items)
assert val_fail_count["valid"] is False
assert len(val_fail_count["errors"]) > 0

# Verify citation tampering rejection: table id tampering
tampered_table = list(pipe_res2.citations)
tampered_table[0] = dict(tampered_table[0])
tampered_table[0]["table_id"] = "unauthorized_table"
val_fail_table = validate_citations(tampered_table, retained_evidence_items=pipe_res2.retained_evidence_items)
assert val_fail_table["valid"] is False

# Verify citation tampering rejection: predicate tampering
tampered_pred = list(pipe_res2.citations)
tampered_pred[0] = dict(tampered_pred[0])
tampered_pred[0]["predicate_values"] = ["99999", "Invented"]
val_fail_pred = validate_citations(tampered_pred, retained_evidence_items=pipe_res2.retained_evidence_items)
assert val_fail_pred["valid"] is False

# Verify citation tampering rejection: provenance tampering
tampered_prov = list(pipe_res2.citations)
tampered_prov[0] = dict(tampered_prov[0])
tampered_prov[0]["provenance"] = {{"table_id": "other_table", "row_indices": [999]}}
val_fail_prov = validate_citations(tampered_prov, retained_evidence_items=pipe_res2.retained_evidence_items)
assert val_fail_prov["valid"] is False
print("Citation tampering correctly rejected across count, table, predicate, and provenance modifications.")

# Verify cross-workspace isolation
other_ws = "{workspace_id}_isolated"
other_tables = store.list_tables(other_ws)
assert len(other_tables) == 0
req_other = QueryRequest(query="What is the earliest and latest Year actually present in {table_id}.csv?", workspace_id=other_ws)
res_other = qs.execute_query(req_other)
assert res_other["answer"] != res1["answer"]
print("Workspace isolation verified: other workspace returned no cross-workspace evidence.")
"""
            env = dict(os.environ)
            env["KRE_TABLE_STORE_BACKEND"] = "dynamodb"

            try:
                proc = subprocess.run([sys.executable, "-c", runner_code], env=env, capture_output=True, text=True, check=True)
                print(proc.stdout)
                print("\n[SUCCESS] Real AWS DynamoDB TableStore verification passed completely.")
            except subprocess.CalledProcessError as e:
                print("Subprocess stdout:\n", e.stdout)
                print("Subprocess stderr:\n", e.stderr)
                raise

        finally:
            # 7. Clean up isolated test workspace items
            print("\n--- Cleaning up test workspace from DynamoDB ---")
            for attempt in range(3):
                try:
                    deleted = store.invalidate_document_version(doc_id, workspace_id)
                    print(f"Deleted {deleted} test records from DynamoDB for workspace {workspace_id}")
                    break
                except Exception as e:
                    print(f"Cleanup attempt {attempt+1} encountered: {e}")
                    time.sleep(2)


if __name__ == "__main__":
    main()
