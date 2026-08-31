import sys
import time
from pathlib import Path
from dotenv import load_dotenv

# Ensure src is on python path and env is loaded
backend_dir = Path(__file__).resolve().parent.parent
load_dotenv(backend_dir / ".env")
sys.path.insert(0, str(backend_dir / "src"))

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from qdrant_client.http import models as qmodels

from aws.infra import setup_infrastructure
from db.database import CloudRepository
from ingestion.embed_service import embed_chunks_dual
from ingestion.format_router import route as route_adapter
from ingestion.okf_builder import build_okf
from ingestion.parse_service import generate_deterministic_doc_id
from schemas.models import Document

print("=" * 80)
print("=== CANONICAL MASTER WIPE & RE-INGESTION (SYSTEM 1 + OKF SYSTEM 2) ===")
print("=" * 80)

# 1. Provision / Verify All Infrastructure Tables
print("\n[1/5] Ensuring AWS Infrastructure (DynamoDB tables & S3 buckets)...")
setup_infrastructure()
repo = CloudRepository()
print(
    "  - All DynamoDB tables verified active (kre-table, okf_entities, okf_properties, okf_relations)."
)

# 2. Wipe & Recreate Qdrant Collection with Indices
print("\n[2/5] Recreating Qdrant collection 'kre_chunks'...")
try:
    if repo.qclient.collection_exists(repo.collection_name):
        repo.qclient.delete_collection(repo.collection_name)
        print("  - Deleted existing Qdrant collection.")

    repo.qclient.create_collection(
        collection_name=repo.collection_name,
        vectors_config={
            "embedding_fast": qmodels.VectorParams(
                size=384, distance=qmodels.Distance.COSINE
            ),
            "embedding_full": qmodels.VectorParams(
                size=1024, distance=qmodels.Distance.COSINE
            ),
        },
    )
    repo.qclient.create_payload_index(
        collection_name=repo.collection_name,
        field_name="page_number",
        field_schema=qmodels.PayloadSchemaType.INTEGER,
    )
    repo.qclient.create_payload_index(
        collection_name=repo.collection_name,
        field_name="document_id",
        field_schema=qmodels.PayloadSchemaType.KEYWORD,
    )
    repo.qclient.create_payload_index(
        collection_name=repo.collection_name,
        field_name="original_id",
        field_schema=qmodels.PayloadSchemaType.KEYWORD,
    )
    repo.qclient.create_payload_index(
        collection_name=repo.collection_name,
        field_name="workspace_id",
        field_schema=qmodels.PayloadSchemaType.KEYWORD,
    )
    print(
        "  - Created fresh Qdrant collection 'kre_chunks' with dual vectors (384, 1024) and payload indexes."
    )
except Exception as e:
    print(f"  - Error recreating Qdrant collection: {e}")
    sys.exit(1)

# 3. Clear All DynamoDB Tables
print("\n[3/5] Clearing all DynamoDB tables...")
tables_to_clear = [
    ("kre-table", repo.table),
    ("okf_entities", repo.okf_entities_table),
    ("okf_properties", repo.okf_properties_table),
    ("okf_relations", repo.okf_relations_table),
]

for tbl_name, tbl in tables_to_clear:
    try:
        scan_resp = tbl.scan(ProjectionExpression="PK, SK")
        items = scan_resp.get("Items", [])
        while "LastEvaluatedKey" in scan_resp:
            scan_resp = tbl.scan(
                ProjectionExpression="PK, SK",
                ExclusiveStartKey=scan_resp["LastEvaluatedKey"],
            )
            items.extend(scan_resp.get("Items", []))

        if items:
            print(f"  - Deleting {len(items)} items from {tbl_name}...")
            with tbl.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
        print(f"  - {tbl_name} cleared.")
    except Exception as e:
        print(f"  - Error clearing {tbl_name}: {e}")
        sys.exit(1)

# 4. Ingest All Documents (Advance + Baseline) with Dual Embedding + OKF
print("\n[4/5] Ingesting documents (Dual Embeddings + OKF Graph Extraction)...")

DEFAULT_WORKSPACE_ID = "ws_001"
try:
    repo.create_workspace(name="Default Benchmark Workspace", workspace_id=DEFAULT_WORKSPACE_ID)
    print(f"  - Created default workspace '{DEFAULT_WORKSPACE_ID}'")
except Exception as e:
    print(f"  - Workspace '{DEFAULT_WORKSPACE_ID}' status: {e}")

BASE_DIR = Path(__file__).resolve().parent.parent
ADVANCE_DIR = BASE_DIR / "tests" / "data" / "advance"
BASELINE_DIR = BASE_DIR / "tests" / "data"

docs_to_ingest = [
    # 9 Canonical Benchmark Documents
    ADVANCE_DIR / "1706.03762v7.pdf",
    ADVANCE_DIR / "2204.13154v1.pdf",
    ADVANCE_DIR / "2507.19595v3.pdf",
    ADVANCE_DIR / "Govt_Colleges_TeachingStaff_Position_2024_25_0.csv",
    ADVANCE_DIR / "National-Strategy-for-Artificial-Intelligence.pdf",
    ADVANCE_DIR / "Workflow Documentation.docx",
    ADVANCE_DIR / "hdfc-mutual-fund-handbook.pdf",
    ADVANCE_DIR / "rs_Bills_Passed_Returned_from_session_217-241.csv",
    ADVANCE_DIR / "submission.pptx",
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
        raw_bytes = filepath.read_bytes()
        # A. Parse into raw chunks
        source_format, adapter = route_adapter(filepath)
        doc_id = generate_deterministic_doc_id(filepath)
        raw_chunks = adapter(filepath, doc_id, workspace_id=DEFAULT_WORKSPACE_ID)
        parse_elapsed = time.perf_counter() - t0
        print(f"    1. Parsed {len(raw_chunks)} raw chunks in {parse_elapsed:.2f}s")

        # B. Dual-embed chunks (BGE ONNX 384-dim + Titan V2 1024-dim)
        t_emb = time.perf_counter()
        embedded_chunks = embed_chunks_dual(raw_chunks, provider="aws")
        emb_elapsed = time.perf_counter() - t_emb
        print(f"    2. Embedded {len(embedded_chunks)} chunks in {emb_elapsed:.2f}s")

        # C. Save Document + Chunks to DynamoDB (kre-table) and Qdrant
        doc = Document(
            id=doc_id,
            filename=filename,
            source_format=source_format,
            chunks=tuple(embedded_chunks),
            workspace_id=DEFAULT_WORKSPACE_ID,
        )
        repo.save(doc)
        repo.add_document_to_workspace(DEFAULT_WORKSPACE_ID, doc, raw_bytes=raw_bytes)
        print(f"    3. Saved to DynamoDB (kre-table), Qdrant (kre_chunks), and workspace {DEFAULT_WORKSPACE_ID}.")

        # D. Build OKF Knowledge Graph (Tier 1 regex + Tier 3 Nova Micro + System 2 edges)
        t_okf = time.perf_counter()
        build_okf(doc)
        okf_elapsed = time.perf_counter() - t_okf
        print(f"    4. Built OKF Knowledge Graph in {okf_elapsed:.2f}s.")

        total_doc_elapsed = time.perf_counter() - t0
        print(f"    -> Completed {filename} in {total_doc_elapsed:.2f}s total.")
        ingested_summary[filename] = {
            "doc_id": doc_id,
            "format": source_format,
            "chunks": len(embedded_chunks),
        }
    except Exception as e:
        print(f"    [-] FAILED to ingest {filename}: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

print("\n" + "=" * 80)
print("=== [5/5] POST-INGESTION HARD INTEGRITY ASSERTIONS ===")
print("=" * 80)

# Check DynamoDB kre-table
scan_kre = repo.table.scan(ProjectionExpression="PK, SK")
kre_items = scan_kre.get("Items", [])
while "LastEvaluatedKey" in scan_kre:
    scan_kre = repo.table.scan(
        ProjectionExpression="PK, SK",
        ExclusiveStartKey=scan_kre["LastEvaluatedKey"],
    )
    kre_items.extend(scan_kre.get("Items", []))

doc_headers = sum(1 for i in kre_items if i["SK"].startswith("DOC#"))
chunk_records = sum(1 for i in kre_items if i["SK"].startswith("CHUNK#"))
okf_usages = sum(1 for i in kre_items if i["SK"] == "OKF_USAGE")

print(f"  * kre-table: {len(kre_items)} total items")
print(f"      - Doc Headers (SK=DOC#):      {doc_headers}")
print(f"      - Chunk Records (SK=CHUNK#):  {chunk_records}")
print(f"      - Token Usages (SK=OKF_USAGE):{okf_usages}")

if chunk_records == 0:
    raise RuntimeError("AssertionFailed: Zero CHUNK items found in kre-table!")

# Check DynamoDB okf_entities
scan_entities = repo.okf_entities_table.scan(ProjectionExpression="PK, SK")
entity_items = scan_entities.get("Items", [])
while "LastEvaluatedKey" in scan_entities:
    scan_entities = repo.okf_entities_table.scan(
        ProjectionExpression="PK, SK",
        ExclusiveStartKey=scan_entities["LastEvaluatedKey"],
    )
    entity_items.extend(scan_entities.get("Items", []))

concept_count = sum(1 for i in entity_items if i["SK"] == "META")
print(
    f"  * okf_entities: {len(entity_items)} total items ({concept_count} concept nodes)"
)
if concept_count == 0:
    raise RuntimeError("AssertionFailed: Zero CONCEPT items found in okf_entities!")

# Check DynamoDB okf_properties
scan_props = repo.okf_properties_table.scan(ProjectionExpression="PK, SK")
prop_items = scan_props.get("Items", [])
while "LastEvaluatedKey" in scan_props:
    scan_props = repo.okf_properties_table.scan(
        ProjectionExpression="PK, SK",
        ExclusiveStartKey=scan_props["LastEvaluatedKey"],
    )
    prop_items.extend(scan_props.get("Items", []))

print(f"  * okf_properties: {len(prop_items)} property facts")
if len(prop_items) == 0:
    raise RuntimeError("AssertionFailed: Zero PROPERTY items found in okf_properties!")

# Check DynamoDB okf_relations
scan_rels = repo.okf_relations_table.scan(ProjectionExpression="PK, SK")
rel_items = scan_rels.get("Items", [])
while "LastEvaluatedKey" in scan_rels:
    scan_rels = repo.okf_relations_table.scan(
        ProjectionExpression="PK, SK",
        ExclusiveStartKey=scan_rels["LastEvaluatedKey"],
    )
    rel_items.extend(scan_rels.get("Items", []))

print(f"  * okf_relations: {len(rel_items)} graph edges")
if len(rel_items) == 0:
    raise RuntimeError("AssertionFailed: Zero RELATION items found in okf_relations!")

# Check Qdrant Vector Store
q_info = repo.qclient.get_collection(repo.collection_name)
points_count = q_info.points_count
print(
    f"  * Qdrant kre_chunks: {points_count} vector points (expected >= {chunk_records})"
)
if points_count < chunk_records:
    raise RuntimeError(
        f"AssertionFailed: Qdrant point count ({points_count}) < chunk records ({chunk_records})"
    )

print("\n" + "=" * 80)
print("ALL INTEGRITY ASSERTIONS PASSED! DATABASE FULLY POPULATED & VERIFIED.")
print("=" * 80)
