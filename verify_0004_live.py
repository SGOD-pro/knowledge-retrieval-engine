import io
import json
import time
import requests
import sys
from pathlib import Path

BASE_URL = "http://127.0.0.1:8001"
ALICE_HEADERS = {"Authorization": "Bearer test_token_alice"}
BOB_HEADERS = {"Authorization": "Bearer test_token_bob"}

results = []

def record(behavior: str, req_line: str, resp: requests.Response, passed: bool, notes: str = ""):
    entry = {
        "behavior": behavior,
        "request": req_line,
        "status": resp.status_code,
        "passed": passed,
        "body_excerpt": resp.text[:200],
        "notes": notes,
    }
    results.append(entry)
    print(f"[{'PASS' if passed else 'FAIL'}] {behavior} -> {resp.status_code} ({req_line})")

# 1. Create workspace for Alice
r1 = requests.post(f"{BASE_URL}/workspaces", json={"name": "Alice 0004 Live WS"}, headers=ALICE_HEADERS)
record("Create workspace for Alice", "POST /workspaces", r1, r1.status_code == 201)
ws_id = r1.json().get("workspace_id")

# 2. Ingest real CSV through API with DocumentSnapshot provenance
csv_bytes = (
    b"year,amount,industry\n"
    b"2020,150,agriculture\n"
    b"2021,200,manufacturing\n"
    b"2022,350,services\n"
    b"2023,500,tech\n"
    b"2024,650,tech\n"
)
files = {"files": ("industry_growth.csv", io.BytesIO(csv_bytes), "text/csv")}
r2 = requests.post(f"{BASE_URL}/workspaces/{ws_id}/documents", files=files, headers=ALICE_HEADERS)
record("Ingest real CSV via API endpoint", f"POST /workspaces/{ws_id}/documents", r2, r2.status_code == 201)
upload_json = r2.json()
items = upload_json.get("items") or upload_json.get("uploaded_documents", [])
doc_id = items[0]["id"] if items else None
time.sleep(1.0)  # Allow background ingestion to complete

# 3. Query deterministic aggregate over the ingested CSV
r3 = requests.post(
    f"{BASE_URL}/workspaces/{ws_id}/query",
    json={"query": "What is the earliest and latest year in industry_growth.csv?"},
    headers=ALICE_HEADERS,
)
q3_json = r3.json()
passed_q3 = (
    r3.status_code == 200
    and q3_json.get("status") == "SUCCESS"
    and "2020" in q3_json.get("answer", "")
    and "2024" in q3_json.get("answer", "")
    and len(q3_json.get("citations", [])) >= 1
    and q3_json["citations"][0]["location"]["type"] == "table"
)
record("Deterministic aggregate query with cited result", f"POST /workspaces/{ws_id}/query", r3, passed_q3, f"Answer: {q3_json.get('answer')}")

# 4. Ingest large dataset (250 rows) to verify no batch truncation
large_lines = [b"year,amount,division"]
for i in range(250):
    yr = 1800 + i
    amt = 10 + (i % 50)
    large_lines.append(f"{yr},{amt},div_{i % 5}".encode("utf-8"))
large_csv = b"\n".join(large_lines) + b"\n"
files_large = {"files": ("large_series.csv", io.BytesIO(large_csv), "text/csv")}
r4_upload = requests.post(f"{BASE_URL}/workspaces/{ws_id}/documents", files=files_large, headers=ALICE_HEADERS)
record("Ingest large CSV (250 rows) beyond 99-row batch size", f"POST /workspaces/{ws_id}/documents", r4_upload, r4_upload.status_code == 201)
large_doc_id = r4_upload.json()["items"][0]["id"]
for _ in range(20):
    time.sleep(0.5)
    r_chk = requests.get(f"{BASE_URL}/workspaces/{ws_id}/documents", headers=ALICE_HEADERS)
    if r_chk.status_code == 200:
        docs_list = r_chk.json().get("documents", [])
        matched = [d for d in docs_list if d.get("id") == large_doc_id]
        if matched and matched[0].get("status") in ("Ready", "Failed"):
            break

# Query aggregate over 250 rows
r4_query = requests.post(
    f"{BASE_URL}/workspaces/{ws_id}/query",
    json={"query": "What is the earliest and latest year in large_series.csv?"},
    headers=ALICE_HEADERS,
)
q4_json = r4_query.json()
passed_q4 = (
    r4_query.status_code == 200
    and q4_json.get("status") == "SUCCESS"
    and "1800" in q4_json.get("answer", "")
    and "2049" in q4_json.get("answer", "")
)
record("Aggregate across 250 rows proves no truncation", f"POST /workspaces/{ws_id}/query", r4_query, passed_q4, f"Answer: {q4_json.get('answer')}")

# 5. Ingest malformed CSV and verify incomplete coverage blocks aggregate with 422
malformed_csv = (
    b"year,amount,industry\n"
    b"2021,100,tech\n"
    b"2022,250\n"
    b"2023,400,tech\n"
)
files_malformed = {"files": ("malformed.csv", io.BytesIO(malformed_csv), "text/csv")}
r5_upload = requests.post(f"{BASE_URL}/workspaces/{ws_id}/documents", files=files_malformed, headers=ALICE_HEADERS)
record("Ingest malformed CSV recording exclusions", f"POST /workspaces/{ws_id}/documents", r5_upload, r5_upload.status_code == 201)
time.sleep(1.0)  # Allow malformed parsing and coverage flag persistence

r5_query = requests.post(
    f"{BASE_URL}/workspaces/{ws_id}/query",
    json={"query": "What is the earliest and latest year in malformed.csv?"},
    headers=ALICE_HEADERS,
)
passed_q5 = (
    r5_query.status_code == 422
    and r5_query.json().get("detail", {}).get("type") == "urn:kre:error:insufficient_coverage"
)
record("Incomplete coverage blocks aggregate with HTTP 422", f"POST /workspaces/{ws_id}/query", r5_query, passed_q5)

# 6. Cross-workspace access denial: Bob receives 404
r6_query = requests.post(
    f"{BASE_URL}/workspaces/{ws_id}/query",
    json={"query": "What is the earliest and latest year in industry_growth.csv?"},
    headers=BOB_HEADERS,
)
record("Cross-workspace query denied with HTTP 404", f"POST /workspaces/{ws_id}/query (Bob auth)", r6_query, r6_query.status_code == 404)

r6_upload = requests.post(
    f"{BASE_URL}/workspaces/{ws_id}/documents",
    files={"files": ("hacked.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
    headers=BOB_HEADERS,
)
record("Cross-workspace upload denied with HTTP 404", f"POST /workspaces/{ws_id}/documents (Bob auth)", r6_upload, r6_upload.status_code == 404)

# 7. Insufficient evidence returns 200 refusal in an empty workspace (distinct from 503)
r_empty = requests.post(f"{BASE_URL}/workspaces", json={"name": "Alice Empty WS"}, headers=ALICE_HEADERS)
empty_ws_id = r_empty.json().get("workspace_id")
r7_no_data = requests.post(
    f"{BASE_URL}/workspaces/{empty_ws_id}/query",
    json={"query": "What is the earliest and latest year in missing_table.csv?"},
    headers=ALICE_HEADERS,
)
q7_json = r7_no_data.json()
passed_r7 = (
    r7_no_data.status_code == 200
    and q7_json.get("status") == "SUCCESS"
    and len(q7_json.get("citations", [])) == 0
    and "couldn't find" in q7_json.get("answer", "").lower()
)
record("Insufficient evidence returns HTTP 200 refusal with 0 citations", f"POST /workspaces/{empty_ws_id}/query", r7_no_data, passed_r7)

# 8. Deleted source denial: Alice tombstones source, blocking subsequent aggregate with HTTP 422
r8_del = requests.delete(f"{BASE_URL}/workspaces/{ws_id}/sources/{doc_id}", headers=ALICE_HEADERS)
passed_del = (r8_del.status_code == 200 and r8_del.json().get("status") == "tombstoned")
record("Tombstone source in workspace", f"DELETE /workspaces/{ws_id}/sources/{doc_id}", r8_del, passed_del)

r8_query = requests.post(
    f"{BASE_URL}/workspaces/{ws_id}/query",
    json={"query": "What is the earliest and latest year in industry_growth.csv?"},
    headers=ALICE_HEADERS,
)
passed_tomb_query = (
    r8_query.status_code == 422
    and "tombstoned" in r8_query.json().get("detail", {}).get("detail", "").lower()
)
record("Tombstoned source blocks aggregate query with HTTP 422", f"POST /workspaces/{ws_id}/query", r8_query, passed_tomb_query)

# 9. Ingest real text PDF through API
from pypdf import PdfReader, PdfWriter
data_dir = Path(__file__).resolve().parent / "backend" / "tests" / "data"
reader = PdfReader(str(data_dir / "hdfc.pdf"))
writer = PdfWriter()
writer.add_page(reader.pages[0])
writer.add_page(reader.pages[1])
pdf_buf = io.BytesIO()
writer.write(pdf_buf)
pdf_bytes = pdf_buf.getvalue()

r_ws_pdf = requests.post(f"{BASE_URL}/workspaces", json={"name": "Alice PDF Live WS"}, headers=ALICE_HEADERS)
ws_pdf_id = r_ws_pdf.json().get("workspace_id")
files_pdf = {"files": ("fund_handbook.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
r9_upload = requests.post(f"{BASE_URL}/workspaces/{ws_pdf_id}/documents", files=files_pdf, headers=ALICE_HEADERS)
record("Ingest real text PDF via API endpoint", f"POST /workspaces/{ws_pdf_id}/documents", r9_upload, r9_upload.status_code == 201)
pdf_doc_id = r9_upload.json()["items"][0]["id"]
for _ in range(30):
    time.sleep(1.0)
    r_chk = requests.get(f"{BASE_URL}/workspaces/{ws_pdf_id}/documents", headers=ALICE_HEADERS)
    if r_chk.status_code == 200:
        docs_list = r_chk.json().get("documents", [])
        matched = [d for d in docs_list if d.get("id") == pdf_doc_id]
        if matched and matched[0].get("status") in ("Ready", "Failed"):
            break

# 10. Query text retrieval from ingested PDF with DocumentLocation citations
r10_query = requests.post(
    f"{BASE_URL}/workspaces/{ws_pdf_id}/query",
    json={"query": "What is the mission in the mutual fund handbook?"},
    headers=ALICE_HEADERS,
)
q10_json = r10_query.json()
passed_q10 = (
    r10_query.status_code == 200
    and q10_json.get("status") == "SUCCESS"
    and len(q10_json.get("answer", "")) > 0
    and "wealth creator" in q10_json.get("answer", "").lower()
    and len(q10_json.get("citations", [])) >= 1
    and q10_json["citations"][0]["location"]["type"] == "document"
    and q10_json["citations"][0]["location"]["page"] == 1
    and "wealth creator" in q10_json["citations"][0].get("snippet", "").lower()
)
record("Text PDF query with DocumentLocation citation (mocked generation in test mode)", f"POST /workspaces/{ws_pdf_id}/query", r10_query, passed_q10, f"Answer: {q10_json.get('answer')[:100]}...")

# 11. Insufficient evidence on PDF returns 200 refusal with 0 citations
r11_query = requests.post(
    f"{BASE_URL}/workspaces/{ws_pdf_id}/query",
    json={"query": "What is the secret recipe for sourdough bread?"},
    headers=ALICE_HEADERS,
)
q11_json = r11_query.json()
passed_q11 = (
    r11_query.status_code == 200
    and q11_json.get("status") == "SUCCESS"
    and len(q11_json.get("citations", [])) == 0
    and "couldn't find" in q11_json.get("answer", "").lower()
)
record("PDF insufficient evidence returns HTTP 200 refusal with 0 citations", f"POST /workspaces/{ws_pdf_id}/query", r11_query, passed_q11)

# 12. Tombstoning PDF source blocks subsequent retrieval
r12_del = requests.delete(f"{BASE_URL}/workspaces/{ws_pdf_id}/sources/{pdf_doc_id}", headers=ALICE_HEADERS)
passed_pdf_del = (r12_del.status_code == 200 and r12_del.json().get("status") == "tombstoned")
record("Tombstone PDF source in workspace", f"DELETE /workspaces/{ws_pdf_id}/sources/{pdf_doc_id}", r12_del, passed_pdf_del)

r12_query = requests.post(
    f"{BASE_URL}/workspaces/{ws_pdf_id}/query",
    json={"query": "What is the mission in the mutual fund handbook?"},
    headers=ALICE_HEADERS,
)
q12_json = r12_query.json()
passed_pdf_tomb = (
    r12_query.status_code == 200
    and len(q12_json.get("citations", [])) == 0
    and "couldn't find" in q12_json.get("answer", "").lower()
)
record("Tombstoned PDF blocks citations in subsequent queries", f"POST /workspaces/{ws_pdf_id}/query", r12_query, passed_pdf_tomb)

print(f"\nAll behaviors passed: {all(r['passed'] for r in results)} ({sum(1 for r in results if r['passed'])}/{len(results)})")
if not all(r["passed"] for r in results):
    sys.exit(1)

