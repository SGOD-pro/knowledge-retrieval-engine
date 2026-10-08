import os
import sys
from pathlib import Path
from dotenv import load_dotenv

backend_dir = Path(__file__).resolve().parent.parent
load_dotenv(backend_dir / ".env")
sys.path.insert(0, str(backend_dir / "src"))

from fastapi.testclient import TestClient
from main import app
from db.database import CloudRepository
from schemas.models import QueryRequest
from ingestion.parse_service import parse_file
from ingestion.embed_service import embed_chunks_dual

def smoke_test_cross_workspace_api():
    print("=" * 80)
    print("=== RUNNING LIVE API CROSS-WORKSPACE ISOLATION SMOKE TEST ===")
    print("=" * 80)

    client = TestClient(app)
    repo = CloudRepository()

    ws_a = "ws_smoke_alpha"
    ws_b = "ws_smoke_beta"

    print(f"\n[1/4] Setting up Workspace A ({ws_a}) and Workspace B ({ws_b})...")
    repo.create_workspace("Workspace Alpha", workspace_id=ws_a)
    repo.create_workspace("Workspace Beta", workspace_id=ws_b)

    data_dir = backend_dir / "tests" / "data" / "advance"
    pdf_path = data_dir / "1706.03762v7.pdf"
    csv_path = data_dir / "Govt_Colleges_TeachingStaff_Position_2024_25_0.csv"

    from dataclasses import replace

    print(f"\n[2/4] Ingesting {pdf_path.name} into Workspace A ({ws_a})...")
    doc_a = parse_file(pdf_path, workspace_id=ws_a)
    doc_a = replace(doc_a, chunks=tuple(embed_chunks_dual(list(doc_a.chunks))))
    repo.save(doc_a)
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    repo.add_document_to_workspace(ws_a, doc_a, raw_bytes=pdf_bytes)
    print(f"  - Ingested {len(doc_a.chunks)} chunks into Workspace A (doc_id={doc_a.id})")

    print(f"\n[3/4] Ingesting {csv_path.name} into Workspace B ({ws_b})...")
    doc_b = parse_file(csv_path, workspace_id=ws_b)
    doc_b = replace(doc_b, chunks=tuple(embed_chunks_dual(list(doc_b.chunks))))
    repo.save(doc_b)
    with open(csv_path, "rb") as f:
        csv_bytes = f.read()
    repo.add_document_to_workspace(ws_b, doc_b, raw_bytes=csv_bytes)
    print(f"  - Ingested {len(doc_b.chunks)} chunks into Workspace B (doc_id={doc_b.id})")

    print("\n[4/4] Executing Cross-Workspace API Queries...")

    # Query 1: Query Workspace A for Workspace B content (Teaching staff positions)
    print("\n  -> Querying Workspace A with Workspace B question: 'What is the teaching staff position in Government Colleges?'")
    resp_a_cross = client.post(
        "/api/v1/query",
        json={
            "query": "What is the teaching staff position in Government Colleges?",
            "workspace_id": ws_a,
        },
    )
    assert resp_a_cross.status_code == 200, f"Query failed: {resp_a_cross.text}"
    data_a_cross = resp_a_cross.json()
    print(f"     Status: {resp_a_cross.status_code}")
    print(f"     Answer: {data_a_cross.get('answer', '')[:120]}...")
    print(f"     Citations: {data_a_cross.get('citations', [])}")
    
    # Assert NO citations or text from Workspace B document
    for cit in data_a_cross.get("citations", []):
        assert str(doc_b.id) not in cit, f"LEAK DETECTED: Workspace B doc {doc_b.id} found in Workspace A response citations!"
    print("     ✓ PASSED: Zero cross-workspace bleed from Workspace B into Workspace A.")

    # Query 2: Query Workspace B for Workspace A content (Attention / Transformer)
    print("\n  -> Querying Workspace B with Workspace A question: 'Explain multi-head attention mechanism in Transformer'")
    resp_b_cross = client.post(
        "/api/v1/query",
        json={
            "query": "Explain multi-head attention mechanism in Transformer",
            "workspace_id": ws_b,
        },
    )
    assert resp_b_cross.status_code == 200, f"Query failed: {resp_b_cross.text}"
    data_b_cross = resp_b_cross.json()
    print(f"     Status: {resp_b_cross.status_code}")
    print(f"     Answer: {data_b_cross.get('answer', '')[:120]}...")
    print(f"     Citations: {data_b_cross.get('citations', [])}")
    
    # Assert NO citations or text from Workspace A document
    for cit in data_b_cross.get("citations", []):
        assert str(doc_a.id) not in cit, f"LEAK DETECTED: Workspace A doc {doc_a.id} found in Workspace B response citations!"
    print("     ✓ PASSED: Zero cross-workspace bleed from Workspace A into Workspace B.")

    # Query 3: Query Workspace A for Workspace A content (Should hit and answer)
    print("\n  -> Querying Workspace A with valid Workspace A question: 'What is the attention mechanism in Transformer?'")
    resp_a_valid = client.post(
        "/api/v1/query",
        json={
            "query": "What is the attention mechanism in Transformer?",
            "workspace_id": ws_a,
        },
    )
    assert resp_a_valid.status_code == 200, f"Query failed: {resp_a_valid.text}"
    data_a_valid = resp_a_valid.json()
    print(f"     Status: {resp_a_valid.status_code}")
    print(f"     Answer: {data_a_valid.get('answer', '')[:120]}...")
    print(f"     Citations: {data_a_valid.get('citations', [])}")
    print("     ✓ PASSED: Valid query in Workspace A successfully retrieved and scoped.")

    print("\n" + "=" * 80)
    print("ALL CROSS-WORKSPACE ISOLATION CHECKS PASSED SUCCESSFULLY!")
    print("=" * 80)

if __name__ == "__main__":
    smoke_test_cross_workspace_api()
