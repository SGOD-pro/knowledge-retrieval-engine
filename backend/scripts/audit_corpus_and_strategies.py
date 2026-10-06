import hashlib
import json
import logging
from pathlib import Path
import sys

# Add src to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from config import settings
from modules.documents.documents_repository import DocumentsRepository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("audit")

def audit_corpus_and_strategies(workspace_id: str = "ws_fresh_benchmark"):
    print("=" * 60)
    print(f"AUDIT FOR WORKSPACE: {workspace_id}")
    print("=" * 60)

    # 1. Corpus files on disk
    data_dir = BASE_DIR.parent / "data"
    corpus_files = [
        "2212.14776v3.pdf",
        "2412.20875v1.pdf",
        "2501.05730v1.pdf",
        "2501.09166v1.pdf",
        "NFHS_5_India_Districts_Factsheet_Data.xls",
        "National-Strategy-for-Artificial-Intelligence.pdf",
        "SEC-Form-10Q.pdf",
        "rs_status_bill_passed_assent-1952-2016.csv",
        "survay.csv",
    ]

    print("\n1. SOURCE FILE DISK AUDIT:")
    source_stats = {}
    for fn in corpus_files:
        matches = list(data_dir.rglob(fn))
        if not matches:
            print(f"  [MISSING] {fn}")
            continue
        p = matches[0]
        size = p.stat().st_size
        sha256 = hashlib.sha256(p.read_bytes()).hexdigest()
        
        # Source metric: rows or pages
        unit_info = ""
        if fn.endswith(".csv"):
            import csv
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                rows = sum(1 for _ in csv.reader(f)) - 1
            unit_info = f"{rows:,} rows"
            source_stats[fn] = {"type": "csv", "source_rows": rows, "bytes": size, "sha256": sha256, "path": str(p)}
        elif fn.endswith((".xls", ".xlsx")):
            try:
                import pandas as pd
                xl = pd.read_excel(p)
                unit_info = f"{len(xl):,} rows"
                source_stats[fn] = {"type": "excel", "source_rows": len(xl), "bytes": size, "sha256": sha256, "path": str(p)}
            except Exception as e:
                unit_info = f"excel read error: {e}"
        elif fn.endswith(".pdf"):
            try:
                import pypdf
                reader = pypdf.PdfReader(str(p))
                pages = len(reader.pages)
                unit_info = f"{pages} pages"
                source_stats[fn] = {"type": "pdf", "source_pages": pages, "bytes": size, "sha256": sha256, "path": str(p)}
            except Exception as e:
                unit_info = f"pdf error: {e}"

        print(f"  {fn:45} | Size: {size:10,} B | {unit_info:15} | SHA: {sha256[:12]}...")

    # 2. DynamoDB Chunks Audit
    print("\n2. DYNAMODB STORAGE AUDIT:")
    repo = DocumentsRepository()
    chunks = repo.get_all_chunks(workspace_id=workspace_id)
    print(f"  Total persisted chunks for {workspace_id}: {len(chunks)}")
    
    chunks_per_doc = {}
    for c in chunks:
        doc_fn = getattr(c, "document_filename", None)
        if not doc_fn and hasattr(c, "metadata") and isinstance(c.metadata, dict):
            doc_fn = c.metadata.get("document_filename")
        if not doc_fn:
            doc_fn = str(getattr(c, "document_id", "unknown"))
        chunks_per_doc[doc_fn] = chunks_per_doc.get(doc_fn, 0) + 1

    for doc_fn, count in sorted(chunks_per_doc.items()):
        print(f"  - {doc_fn:45} : {count:4} chunks")

    # 3. Qdrant Vector Index Audit
    print("\n3. QDRANT VECTOR INDEX AUDIT:")
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        q_client = QdrantClient(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY)
        col_name = "kre_chunks"
        col_info = q_client.get_collection(collection_name=col_name)
        print(f"  Qdrant collection: {col_name}")
        print(f"  Vectors config: {col_info.config.params.vectors}")
        print(f"  Total points in collection: {col_info.points_count}")

        # Count points for this workspace
        ws_filter = Filter(
            must=[FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))]
        )
        ws_count = q_client.count(collection_name=col_name, count_filter=ws_filter).count
        print(f"  Points indexed for workspace {workspace_id}: {ws_count}")
    except Exception as e:
        print(f"  [ERROR] Qdrant inspection failed: {e}")

    # 4. Structured Table Store Audit
    print("\n4. STRUCTURED TABLE STORE AUDIT:")
    try:
        from db.table_store.dynamo_store import DynamoDBTableStore
        dyn_store = DynamoDBTableStore()
        tables = dyn_store.list_tables(workspace_id=workspace_id)
        print(f"  DynamoDBTableStore tables for {workspace_id}: {len(tables)}")
        for t in tables:
            cov = dyn_store.get_table_coverage(workspace_id=workspace_id, table_id=t.get("table_id"))
            print(f"  - Table {t.get('table_id')} ({t.get('source_file')}): total_rows={cov.total_rows} status={cov.status}")
    except Exception as e:
        print(f"  [INFO] DynamoDBTableStore check: {e}")

    try:
        from db.table_store.sqlite_store import SQLiteTableStore
        sql_store = SQLiteTableStore()
        tables_sql = sql_store.list_tables(workspace_id=workspace_id)
        print(f"  SQLiteTableStore tables for {workspace_id}: {len(tables_sql)}")
        for t in tables_sql:
            cov = sql_store.get_table_coverage(workspace_id=workspace_id, table_id=t.get("table_id"))
            print(f"  - SQLite Table {t.get('table_id')} ({t.get('source_file')}): total_rows={cov.total_rows} status={cov.status}")
    except Exception as e:
        print(f"  [INFO] SQLiteTableStore check: {e}")

    # 5. OKF Runtime Facts Audit
    print("\n5. OKF RUNTIME FACTS AUDIT:")
    try:
        from services.retrieval.okf_retriever import OKFRetriever
        okf = OKFRetriever()
        facts = okf.get_all_facts(workspace_id=workspace_id)
        print(f"  OKF facts for {workspace_id}: {len(facts)}")
    except Exception as e:
        print(f"  [INFO] OKFRetriever check: {e}")

    # 6. Knowledge Graph Audit
    print("\n6. KNOWLEDGE GRAPH AUDIT:")
    try:
        from services.retrieval.graph_retriever import GraphRetriever
        gr = GraphRetriever()
        nodes, edges = gr.get_workspace_graph(workspace_id=workspace_id)
        print(f"  KG nodes: {len(nodes)}, edges: {len(edges)} for {workspace_id}")
    except Exception as e:
        print(f"  [INFO] GraphRetriever check: {e}")

    print("\n" + "=" * 60)
    print("AUDIT COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    ws = sys.argv[1] if len(sys.argv) > 1 else "ws_fresh_benchmark"
    audit_corpus_and_strategies(ws)
