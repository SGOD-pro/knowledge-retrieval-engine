import logging
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

backend_dir = Path(__file__).resolve().parent.parent
load_dotenv(backend_dir / ".env")
sys.path.insert(0, str(backend_dir / "src"))

from config import settings
from db.database import CloudRepository
from schemas.models import Document, Workspace
from ingestion.parse_service import parse_file, generate_deterministic_doc_id
from ingestion.embed_service import embed_chunks_dual

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingest_benchmark")

WORKSPACE_ID = "ws_fresh_benchmark"
WORKSPACE_NAME = "Fresh Benchmark Corpus"

FILES = [
    "data/academic_research/set_aside/2212.14776v3.pdf",
    "data/academic_research/set_aside/2412.20875v1.pdf",
    "data/academic_research/set_aside/2501.05730v1.pdf",
    "data/academic_research/set_aside/2501.09166v1.pdf",
    "data/financial_tables/SEC-Form-10Q.pdf",
    "data/policy_regulatory/National-Strategy-for-Artificial-Intelligence.pdf",
    "data/non_pdf_formats/NFHS_5_India_Districts_Factsheet_Data.xls",
    "data/non_pdf_formats/rs_status_bill_passed_assent-1952-2016.csv",
    "data/non_pdf_formats/survay.csv",
]


def run_ingestion():
    root_dir = backend_dir.parent
    repo = CloudRepository()
    repo.initialize()

    print("=" * 80)
    print(f"INGESTING BENCHMARK CORPUS INTO WORKSPACE: {WORKSPACE_ID}")
    print("=" * 80)

    # 1. Ensure workspace exists in DynamoDB
    repo.create_workspace(
        name=WORKSPACE_NAME,
        industry="Multi-Domain Benchmark",
        description="Fresh evaluation corpus covering academic AI papers, SEC 10-Q financial filing, NITI Aayog AI policy, NFHS-5 district health data, Rajya Sabha legislative bills, and Enterprise survey data.",
        workspace_id=WORKSPACE_ID,
    )
    print(f"Workspace '{WORKSPACE_ID}' ready.")

    total_start = time.time()
    total_chunks_all = 0

    for idx, rel_path in enumerate(FILES, 1):
        abs_path = root_dir / rel_path
        if not abs_path.exists():
            print(f"[{idx}/{len(FILES)}] ERROR: File not found: {abs_path}")
            continue

        filename = abs_path.name
        doc_id = generate_deterministic_doc_id(filename, workspace_id=WORKSPACE_ID)
        print(f"\n[{idx}/{len(FILES)}] Processing {filename} (doc_id={doc_id})...")

        # Step 1: Parse file
        t0 = time.time()
        parsed_doc = parse_file(abs_path, document_id=doc_id, filename=filename, workspace_id=WORKSPACE_ID)
        chunks = list(parsed_doc.chunks)
        parse_dur = time.time() - t0
        print(f"   -> Parsed {len(chunks)} chunks in {parse_dur:.2f}s")

        # Step 2: Embed chunks (fast 384d + full 1024d)
        t1 = time.time()
        embedded_chunks = embed_chunks_dual(chunks, provider="prod")
        embed_dur = time.time() - t1
        print(f"   -> Embedded {len(embedded_chunks)} chunks in {embed_dur:.2f}s")

        # Step 3: Construct Document
        doc = Document(
            id=doc_id,
            filename=filename,
            source_format=parsed_doc.source_format,
            chunks=tuple(embedded_chunks),
            workspace_id=WORKSPACE_ID,
        )

        # Step 4: Save to DynamoDB and Qdrant
        t2 = time.time()
        repo.save(doc)
        raw_bytes = abs_path.read_bytes()
        repo.add_document_to_workspace(
            workspace_id=WORKSPACE_ID,
            document=doc,
            raw_bytes=raw_bytes,
            size_str=f"{len(raw_bytes)/1024:.1f} KB",
        )
        save_dur = time.time() - t2
        print(f"   -> Persisted to DynamoDB & Qdrant in {save_dur:.2f}s")

        total_chunks_all += len(embedded_chunks)

    total_dur = time.time() - total_start
    print("\n" + "=" * 80)
    print(f"INGESTION COMPLETE in {total_dur:.2f}s! Total chunks ingested: {total_chunks_all}")
    print("=" * 80)

    # Verify counts in DynamoDB and Qdrant
    all_chunks = repo.get_all_chunks(workspace_id=WORKSPACE_ID)
    q_info = repo.qclient.get_collection(repo.collection_name)
    print(f"Verification: DynamoDB workspace chunks = {len(all_chunks)}, Qdrant points = {q_info.points_count}")


if __name__ == "__main__":
    run_ingestion()
