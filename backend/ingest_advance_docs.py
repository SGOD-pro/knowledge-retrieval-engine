import os
import sys
import time
from pathlib import Path
import requests

API_URL = "http://127.0.0.1:8000/ingest"
ADVANCE_DIR = Path("d:/WORK/knowledge-retrieval-engine/backend/tests/data/advance")

# List of all files to ingest
files_to_ingest = [
    "Govt_Colleges_TeachingStaff_Position_2024_25_0.csv",
    "rs_Bills_Passed_Returned_from_session_217-241.csv",
    "submission.pptx",
    "Workflow Documentation.docx",
    "2204.13154v1.pdf",
    "2507.19595v3.pdf",
    "1706.03762v7.pdf",
    "hdfc-mutual-fund-handbook.pdf",
    "National-Strategy-for-Artificial-Intelligence.pdf",
]

print("=== Ingesting Advance Dataset ===", flush=True)
results = {}

for filename in files_to_ingest:
    filepath = ADVANCE_DIR / filename
    if not filepath.exists():
        print(f"[-] File not found: {filepath}", flush=True)
        continue
    
    print(f"\n[+] Ingesting: {filename} ({filepath.stat().st_size / 1024:.1f} KB)...", flush=True)
    t0 = time.perf_counter()
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(API_URL, files={"file": (filename, f)}, timeout=600)
        
        elapsed = time.perf_counter() - t0
        if resp.status_code == 200:
            data = resp.json()
            print(f"    [SUCCESS in {elapsed:.2f}s] doc_id={data.get('id')} chunks={data.get('chunk_count')} format={data.get('source_format')}", flush=True)
            results[filename] = data
        else:
            print(f"    [FAILED in {elapsed:.2f}s] HTTP {resp.status_code}: {resp.text}", flush=True)
    except Exception as e:
        print(f"    [ERROR after {time.perf_counter() - t0:.2f}s] {e}", flush=True)

print("\n=== Ingestion Complete Summary ===", flush=True)
for fname, res in results.items():
    print(f" - {fname}: {res.get('chunk_count')} chunks (id={res.get('id')})", flush=True)
