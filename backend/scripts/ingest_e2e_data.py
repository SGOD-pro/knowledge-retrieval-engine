import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

backend_dir = Path(__file__).resolve().parent.parent
load_dotenv(backend_dir / ".env")
sys.path.insert(0, str(backend_dir / "src"))

from db.database import CloudRepository
from ingestion.embed_service import embed_chunks_dual
from ingestion.format_router import route as route_adapter
from ingestion.okf_builder import build_okf
from ingestion.parse_service import generate_deterministic_doc_id
from schemas.models import Document

WORKSPACE_ID = "ws_38c1ac31"

DOC_PATHS = [
    Path("backend/tests/data/advance/Govt_Colleges_TeachingStaff_Position_2024_25_0.csv"),
    Path("data/non_pdf_formats/sample.xlsx"),
    Path("data/non_pdf_formats/submission.pptx"),
]

def main():
    print(f"=== INGESTING MULTI-FORMAT DOCUMENTS INTO {WORKSPACE_ID} ===")
    repo = CloudRepository()
    
    for doc_path in DOC_PATHS:
        full_path = backend_dir.parent / doc_path if not doc_path.exists() else doc_path
        if not full_path.exists():
            print(f"File not found: {full_path}")
            continue
        
        filename = full_path.name
        print(f"\n[+] Processing {filename} ({full_path.stat().st_size / 1024:.1f} KB)...")
        t0 = time.perf_counter()
        
        # 1. Parse
        source_format, adapter = route_adapter(full_path)
        doc_id = generate_deterministic_doc_id(full_path, workspace_id=WORKSPACE_ID)
        raw_chunks = adapter(full_path, doc_id, workspace_id=WORKSPACE_ID)
        if len(raw_chunks) > 40:
            raw_chunks = raw_chunks[:40]
        print(f"    1. Parsed {len(raw_chunks)} raw chunks in {time.perf_counter() - t0:.2f}s")
        
        # 2. Dual-Embed
        t_emb = time.perf_counter()
        embedded_chunks = embed_chunks_dual(raw_chunks, provider="aws")
        print(f"    2. Embedded {len(embedded_chunks)} chunks in {time.perf_counter() - t_emb:.2f}s")
        
        # 3. Save Document + Chunks
        doc = Document(
            id=doc_id,
            filename=filename,
            source_format=source_format,
            chunks=tuple(embedded_chunks),
            workspace_id=WORKSPACE_ID,
        )
        repo.save(doc)
        print(f"    3. Saved to DynamoDB & Qdrant for workspace {WORKSPACE_ID}")
        
        # 4. OKF extraction (cap chunks for speed)
        try:
            t_okf = time.perf_counter()
            build_okf(doc)
            print(f"    4. OKF Graph built in {time.perf_counter() - t_okf:.2f}s")
        except Exception as e:
            print(f"    4. OKF extraction warning: {e}")
            
        print(f"    -> Done in {time.perf_counter() - t0:.2f}s total.")

if __name__ == "__main__":
    main()
