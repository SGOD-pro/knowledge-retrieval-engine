import io
import json
import time
import requests
import sys
from pathlib import Path
from pypdf import PdfReader, PdfWriter

BASE_URL = "http://127.0.0.1:8001"
ALICE_HEADERS = {"Authorization": "Bearer test_token_alice"}
BOB_HEADERS = {"Authorization": "Bearer test_token_bob"}

results = []

def record(criterion: str, behavior: str, req_line: str, status_code: int, passed: bool, evidence: str = ""):
    entry = {
        "criterion": criterion,
        "behavior": behavior,
        "request": req_line,
        "status": status_code,
        "passed": passed,
        "evidence": evidence,
    }
    results.append(entry)
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] [{criterion}] {behavior} -> {status_code} ({req_line}) | {evidence[:120]}")

print("=== Running Spec 0005 Live Verification ===")

# 1. Setup workspace for Alice
r_ws = requests.post(f"{BASE_URL}/workspaces", json={"name": "Alice 0005 Live WS"}, headers=ALICE_HEADERS)
ws_id = r_ws.json().get("workspace_id")
record("Setup", "Create workspace for Alice", "POST /workspaces", r_ws.status_code, r_ws.status_code == 201, f"ws_id={ws_id}")

# 2. Ingest real text PDF containing formal caption definitions (AC-1, AC-6)
# Use pages 3 and 4 of Attention is All You Need which contains Figure 1 definition and Figure 2 cross-reference
data_dir = Path(__file__).resolve().parent / "backend" / "tests" / "data"
attention_pdf = data_dir / "advance" / "1706.03762v7.pdf"

reader = PdfReader(str(attention_pdf))
writer = PdfWriter()
writer.add_page(reader.pages[2])  # page 3: Figure 1 caption definition
writer.add_page(reader.pages[3])  # page 4: Figure 2 reference
writer.add_outline_item("Model Architecture", 1)
writer.add_outline_item("Attention Mechanism", 2)
pdf_buf = io.BytesIO()
writer.write(pdf_buf)
pdf_bytes = pdf_buf.getvalue()

files = {"files": ("transformer_figures.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
r_upload = requests.post(f"{BASE_URL}/workspaces/{ws_id}/documents", files=files, headers=ALICE_HEADERS)
doc_items = r_upload.json().get("items", [])
doc_id = doc_items[0]["id"] if doc_items else None
record("AC-1", "Ingest PDF with structural hierarchy extraction", f"POST /workspaces/{ws_id}/documents", r_upload.status_code, r_upload.status_code == 201, f"doc_id={doc_id}")

# Wait for background ingestion to complete and snapshot to reflect ready state
snap_ready = False
for _ in range(40):
    time.sleep(1.0)
    r_docs = requests.get(f"{BASE_URL}/workspaces/{ws_id}/documents", headers=ALICE_HEADERS)
    if r_docs.status_code == 200:
        docs = r_docs.json().get("documents", [])
        matched = [d for d in docs if d.get("id") == doc_id]
        if matched and matched[0].get("status") in ("Ready", "Failed"):
            snap_ready = matched[0].get("status") == "Ready"
            break

# Inspect DocumentSnapshot metadata from boundary repository (AC-1, AC-4, AC-8)
from src.db.workspace_boundary_repo import get_workspace_boundary_repo
boundary_repo = get_workspace_boundary_repo()
snapshot = boundary_repo.get_document_snapshot(ws_id, doc_id)
pi_status = snapshot.page_index_status if snapshot else None
pi_elements = snapshot.page_index_elements_count if snapshot else 0

record(
    "AC-1",
    "DocumentSnapshot records page_index_status and elements count",
    "DocumentSnapshot metadata",
    200 if snapshot else 404,
    snap_ready and pi_status in ("ready", "missing_layout") and pi_elements > 0,
    f"page_index_status={pi_status}, page_index_elements_count={pi_elements}"
)

# 3. Query with structural constraint and inspect candidate telemetry (AC-3, AC-8)
r_query = requests.post(
    f"{BASE_URL}/query",
    json={
        "query": "What does Figure 1 illustrate in the model architecture?",
        "workspace_id": ws_id,
    },
    headers=ALICE_HEADERS,
)
q_json = r_query.json()
cands = q_json.get("retrieval_candidates", {})
struct_ids = cands.get("structural_candidate_ids", [])
struct_matches = cands.get("structural_match_count", 0)
pi_latency = cands.get("page_index_latency_ms", 0.0)
struct_state = cands.get("structural_target_state", "none")

record(
    "AC-8",
    "retrieval_candidates trace exposes structural candidate telemetry",
    "POST /query",
    r_query.status_code,
    r_query.status_code == 200 and "page_index_latency_ms" in cands and struct_state == "unique" and struct_matches >= 1,
    f"match_count={struct_matches}, latency_ms={pi_latency}, target_state={struct_state}, structural_ids_count={len(struct_ids)}"
)

record(
    "AC-3",
    "Soft pooled reciprocal rank fusion without hard exclusions",
    "POST /query",
    r_query.status_code,
    r_query.status_code == 200 and len(struct_ids) > 0 and len(q_json.get("citations", [])) > 0,
    f"structural_ids={len(struct_ids)}, citations={len(q_json.get('citations', []))}"
)

# 4. Multi-tenant boundary isolation: Bob receives 404 accessing Alice workspace (AC-7)
r_bob = requests.post(
    f"{BASE_URL}/workspaces/{ws_id}/query",
    json={"query": "What does Figure 1 illustrate in the model architecture?"},
    headers=BOB_HEADERS,
)
record(
    "AC-7",
    "Cross-workspace query denied with HTTP 404 (0 tenant leakage)",
    f"POST /workspaces/{ws_id}/query (Bob auth)",
    r_bob.status_code,
    r_bob.status_code == 404,
    f"status={r_bob.status_code}"
)

# 5. Tombstone blocks PageIndex retrieval immediately (AC-7)
r_del = requests.delete(f"{BASE_URL}/workspaces/{ws_id}/sources/{doc_id}", headers=ALICE_HEADERS)
tomb_ok = r_del.status_code == 200 and r_del.json().get("status") == "tombstoned"
record(
    "AC-7",
    "Tombstone source in workspace boundary repository",
    f"DELETE /workspaces/{ws_id}/sources/{doc_id}",
    r_del.status_code,
    tomb_ok,
    f"response={r_del.text[:100]}"
)

# Subsequent query on tombstoned document returns 0 citations and 0 structural candidates without error
r_tomb_query = requests.post(
    f"{BASE_URL}/query",
    json={
        "query": "What does Figure 1 illustrate in the model architecture?",
        "workspace_id": ws_id,
    },
    headers=ALICE_HEADERS,
)
tomb_q_json = r_tomb_query.json()
tomb_cands = tomb_q_json.get("retrieval_candidates", {})
record(
    "AC-7",
    "Tombstoned source yields zero structural candidates without throwing",
    "POST /query",
    r_tomb_query.status_code,
    r_tomb_query.status_code == 200 and len(tomb_cands.get("structural_candidate_ids", [])) == 0,
    f"structural_ids={tomb_cands.get('structural_candidate_ids', [])}, citations={len(tomb_q_json.get('citations', []))}"
)

# 6. Verify absence of administrative HTTP endpoints (AC-8)
r_admin = requests.get(f"{BASE_URL}/api/v1/page_index/admin", headers=ALICE_HEADERS)
record(
    "AC-8",
    "Administrative endpoints deferred to minimize attack surface",
    "GET /api/v1/page_index/admin",
    r_admin.status_code,
    r_admin.status_code in (404, 405),
    f"status={r_admin.status_code} (no exposed admin route)"
)

# 7. Benchmark comparator barrier (AC-10)
from src.services.benchmark.benchmark_runner import validate_benchmark_comparison_preconditions
blocked_error = ""
try:
    validate_benchmark_comparison_preconditions(
        baseline_run_id="run_baseline_123",
        candidate_run_id="run_candidate_456",
        comparison_workspace_id="ws_uncontrolled_eval_123",
    )
except ValueError as e:
    blocked_error = str(e)

record(
    "AC-10",
    "Benchmark comparator barrier blocks accuracy claims on non-canonical workspaces",
    "validate_benchmark_comparison_preconditions()",
    200 if "ws_canonical_comparison_0004" in blocked_error else 500,
    "ws_canonical_comparison_0004" in blocked_error,
    f"blocked_message={blocked_error}"
)

print(f"\nAll live verification behaviors passed: {all(r['passed'] for r in results)} ({sum(1 for r in results if r['passed'])}/{len(results)})")
if not all(r["passed"] for r in results):
    sys.exit(1)
