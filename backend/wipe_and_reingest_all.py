import os
import sys
import time
import uuid
from pathlib import Path
from qdrant_client.http import models as qmodels

if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from config import settings
from db.database import CloudRepository
from ingestion.format_router import route as route_adapter
from ingestion.embed_service import embed_chunks_dual
from schemas.models import Document

print("=== Starting Full Wipe & Re-Ingestion (AF2) ===")

repo = CloudRepository()

# 1. Wipe & Recreate Qdrant Collection
print("\n[1/4] Recreating Qdrant collection 'kre_chunks'...")
try:
    if repo.qclient.collection_exists(repo.collection_name):
        repo.qclient.delete_collection(repo.collection_name)
        print("  - Deleted existing Qdrant collection.")
    
    repo.qclient.create_collection(
        collection_name=repo.collection_name,
        vectors_config={
            "embedding_fast": qmodels.VectorParams(size=384, distance=qmodels.Distance.COSINE),
            "embedding_full": qmodels.VectorParams(size=1024, distance=qmodels.Distance.COSINE)
        }
    )
    print("  - Created fresh Qdrant collection 'kre_chunks' with dual vectors (384, 1024).")
except Exception as e:
    print(f"  - Error recreating Qdrant collection: {e}")
    sys.exit(1)

# 2. Clear DynamoDB table items
print("\n[2/4] Clearing DynamoDB table items...")
try:
    # Scan and delete existing items in batches
    scan_resp = repo.table.scan(ProjectionExpression="PK, SK")
    items_to_delete = scan_resp.get("Items", [])
    while "LastEvaluatedKey" in scan_resp:
        scan_resp = repo.table.scan(ProjectionExpression="PK, SK", ExclusiveStartKey=scan_resp["LastEvaluatedKey"])
        items_to_delete.extend(scan_resp.get("Items", []))
        
    print(f"  - Found {len(items_to_delete)} existing items in DynamoDB. Deleting...")
    with repo.table.batch_writer() as batch:
        for item in items_to_delete:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    print("  - DynamoDB table cleared.")
except Exception as e:
    print(f"  - Error clearing DynamoDB: {e}")

# 3. Ingest All Documents (Advance + Baseline)
print("\n[3/4] Ingesting documents...")

ADVANCE_DIR = Path("tests/data/advance")
BASELINE_DIR = Path("tests/data")

# Full list of documents across both corpora
docs_to_ingest = [
    # Advance Corpus
    ADVANCE_DIR / "1706.03762v7.pdf",
    ADVANCE_DIR / "2204.13154v1.pdf",
    ADVANCE_DIR / "2507.19595v3.pdf",
    ADVANCE_DIR / "Govt_Colleges_TeachingStaff_Position_2024_25_0.csv",
    ADVANCE_DIR / "National-Strategy-for-Artificial-Intelligence.pdf",
    ADVANCE_DIR / "Workflow Documentation.docx",
    ADVANCE_DIR / "hdfc-mutual-fund-handbook.pdf",
    ADVANCE_DIR / "rs_Bills_Passed_Returned_from_session_217-241.csv",
    ADVANCE_DIR / "submission.pptx",
    # Baseline Extra Files
    BASELINE_DIR / "machine-readable-business-employment-data-mar-2026-quarter.csv",
    BASELINE_DIR / "survay.csv",
    BASELINE_DIR / "sample.xlsx",
]

ingested_summary = {}

for filepath in docs_to_ingest:
    if not filepath.exists():
        print(f"[-] Skipping missing file: {filepath}")
        continue
        
    filename = filepath.name
    print(f"\n[+] Ingesting: {filename} ({filepath.stat().st_size / 1024:.1f} KB)...")
    t0 = time.perf_counter()
    
    try:
        # Parse
        source_format, adapter = route_adapter(filepath)
        doc_id = str(uuid.uuid4())
        raw_chunks = adapter(filepath, doc_id)
        parse_elapsed = time.perf_counter() - t0
        print(f"    - Parsed {len(raw_chunks)} raw chunks in {parse_elapsed:.2f}s")
        
        if not raw_chunks:
            print("    - No chunks produced, skipping.")
            continue
            
        # Embed dual (384 ONNX + 1024 Titan Bedrock)
        t_embed = time.perf_counter()
        embedded_chunks = embed_chunks_dual(raw_chunks, provider="prod")
        embed_elapsed = time.perf_counter() - t_embed
        print(f"    - Embedded {len(embedded_chunks)} chunks (BGE + Titan) in {embed_elapsed:.2f}s")
        
        # Save document
        source_format = filepath.suffix.lstrip(".").lower()
        doc = Document(doc_id, filename, source_format, tuple(embedded_chunks))
        
        t_save = time.perf_counter()
        repo.save(doc)
        save_elapsed = time.perf_counter() - t_save
        print(f"    - Saved to DynamoDB & Qdrant in {save_elapsed:.2f}s")
        
        total_elapsed = time.perf_counter() - t0
        print(f"    [SUCCESS] {filename} -> {len(embedded_chunks)} chunks in {total_elapsed:.2f}s (doc_id={doc_id})")
        ingested_summary[filename] = {
            "doc_id": doc_id,
            "chunks": len(embedded_chunks),
            "format": source_format
        }
    except Exception as e:
        print(f"    [FAILED] {filename}: {e}")
        import traceback
        traceback.print_exc()

# 4. Direct Qdrant Payload Verification
print("\n[4/4] Direct Qdrant Payload Verification (100% Audit)...")

total_verified = 0
null_fast_count = 0
null_full_count = 0
zero_padded_count = 0
dim_mismatch_count = 0

offset = None
while True:
    res, offset = repo.qclient.scroll(
        collection_name=repo.collection_name,
        limit=500,
        with_vectors=True,
        offset=offset
    )
    for pt in res:
        total_verified += 1
        
        # Fast vector check (384-dim)
        v_fast = pt.vector.get("embedding_fast")
        if v_fast is None:
            null_fast_count += 1
        elif len(v_fast) != 384:
            dim_mismatch_count += 1
            
        # Full vector check (1024-dim)
        v_full = pt.vector.get("embedding_full")
        if v_full is None:
            null_full_count += 1
        elif len(v_full) != 1024:
            dim_mismatch_count += 1
        else:
            # Check specifically if dimensions 384:1024 are all zero
            if all(x == 0.0 for x in v_full[384:]):
                zero_padded_count += 1
                
    if offset is None:
        break

print("\n" + "=" * 60)
print("=== QDRANT VERIFICATION AUDIT REPORT ===")
print("=" * 60)
print(f"Total Qdrant points audited:        {total_verified}")
print(f"NULL embedding_fast count:          {null_fast_count}")
print(f"NULL embedding_full count:          {null_full_count}")
print(f"Zero-padded embedding_full count:   {zero_padded_count}")
print(f"Dimension mismatches:               {dim_mismatch_count}")
print("=" * 60)

if null_fast_count == 0 and null_full_count == 0 and zero_padded_count == 0 and dim_mismatch_count == 0 and total_verified > 0:
    print(">>> 100% DATA INTEGRITY CONFIRMED: ZERO CORRUPTION DETECTED. <<<")
else:
    print(">>> FAILED DATA INTEGRITY AUDIT! <<<")
    sys.exit(1)
