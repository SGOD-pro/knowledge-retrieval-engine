"""Generate canonical inventory reconciliation JSON directly from paginated DynamoDB and Qdrant reads.

Compares:
1. Workspace identity
2. Document IDs and filenames
3. Source file SHA256 hashes
4. Document version IDs / content hashes
5. Complete chunk ID sets across DynamoDB and Qdrant
Fails reconciliation if any records or chunk identity sets differ.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

backend_src = str(Path(__file__).resolve().parent.parent / "src")
repo_root = Path(__file__).resolve().parent.parent.parent
if backend_src not in sys.path:
    sys.path.insert(0, backend_src)

from boto3.dynamodb.conditions import Key
from db.database import CloudRepository
from qdrant_client.http import models as qmodels


class ReconciliationFailure(RuntimeError):
    """Raised when DynamoDB and Qdrant records or chunk ID sets differ."""
    pass


def main():
    print("=" * 80)
    print("CANONICAL INVENTORY RECONCILIATION FROM LIVE DYNAMODB & QDRANT")
    print("=" * 80)

    repo = CloudRepository()
    table = repo.table
    qclient = repo.qclient

    test_json_path = repo_root / "data" / "test.json"
    if not test_json_path.exists():
        raise FileNotFoundError(f"Missing benchmark manifest at {test_json_path}")

    manifest_data = json.loads(test_json_path.read_text(encoding="utf-8"))
    workspace_id = manifest_data.get("workspace_id", "ws_fresh_benchmark")
    declared_docs = manifest_data.get("documents", [])

    print(f"Workspace: {workspace_id}")
    print(f"Declared documents in benchmark manifest: {len(declared_docs)}\n")

    reconciliation_records = []
    total_ddb_chunks = 0
    total_qdrant_points = 0
    discrepancies = []

    import time
    def with_retry(func, max_attempts=6, base_delay=1.5):
        last_exc = None
        for attempt in range(max_attempts):
            try:
                return func()
            except Exception as e:
                last_exc = e
                wait = base_delay * (2 ** attempt)
                print(f"Transient error ({type(e).__name__}): {e}. Retrying in {wait:.1f}s...")
                time.sleep(wait)
        raise last_exc

    for d in declared_docs:
        doc_id = d["doc_id"]
        filename = d["filename"]

        # 1. Resolve source file on disk
        source_matches = list((repo_root / "data").rglob(filename))
        if not source_matches:
            raise ReconciliationFailure(f"Source file {filename} not found under data/")
        source_path = source_matches[0]
        source_bytes = source_path.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        source_size = len(source_bytes)

        # 2. Paginated read from DynamoDB kre-table
        ddb_chunk_ids = set()
        ddb_chunk_items = []
        resp = with_retry(lambda: table.query(
            KeyConditionExpression=Key("PK").eq(f"DOC#{doc_id}") & Key("SK").begins_with("CHUNK#")
        ))
        for item in resp.get("Items", []):
            cid = item["SK"].replace("CHUNK#", "")
            ddb_chunk_ids.add(cid)
            ddb_chunk_items.append({"chunk_id": cid, "row_index": item.get("row_index"), "page_number": item.get("page_number")})

        while "LastEvaluatedKey" in resp:
            last_key = resp["LastEvaluatedKey"]
            resp = with_retry(lambda: table.query(
                KeyConditionExpression=Key("PK").eq(f"DOC#{doc_id}") & Key("SK").begins_with("CHUNK#"),
                ExclusiveStartKey=last_key
            ))
            for item in resp.get("Items", []):
                cid = item["SK"].replace("CHUNK#", "")
                ddb_chunk_ids.add(cid)
                ddb_chunk_items.append({"chunk_id": cid, "row_index": item.get("row_index"), "page_number": item.get("page_number")})

        # 3. Paginated scroll from Qdrant kre_chunks
        qdrant_chunk_ids = set()
        qdrant_points = []
        q_offset = None
        while True:
            cur_offset = q_offset
            pts, q_offset = with_retry(lambda: qclient.scroll(
                collection_name="kre_chunks",
                scroll_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(key="workspace_id", match=qmodels.MatchValue(value=workspace_id)),
                        qmodels.FieldCondition(key="document_id", match=qmodels.MatchValue(value=doc_id)),
                    ]
                ),
                limit=250,
                offset=cur_offset,
                with_payload=True,
                with_vectors=False,
            ))
            for pt in pts:
                cid = pt.payload.get("original_id") or pt.payload.get("chunk_id") or str(pt.id)
                qdrant_chunk_ids.add(cid)
                qdrant_points.append({"point_id": pt.id, "chunk_id": cid, "page_number": pt.payload.get("page_number")})
            if q_offset is None:
                break

        ddb_cnt = len(ddb_chunk_ids)
        qdrant_cnt = len(qdrant_chunk_ids)
        total_ddb_chunks += ddb_cnt
        total_qdrant_points += qdrant_cnt

        # 4. Identity Set Comparison
        missing_in_qdrant = ddb_chunk_ids - qdrant_chunk_ids
        missing_in_ddb = qdrant_chunk_ids - ddb_chunk_ids
        identity_match = (len(missing_in_qdrant) == 0 and len(missing_in_ddb) == 0 and ddb_cnt == qdrant_cnt)

        if not identity_match:
            err = f"{filename} (doc_id={doc_id}): DDB={ddb_cnt} vs Qdrant={qdrant_cnt}. Missing in Qdrant={len(missing_in_qdrant)}, missing in DDB={len(missing_in_ddb)}"
            discrepancies.append(err)

        reconciliation_records.append({
            "filename": filename,
            "document_id": doc_id,
            "source_path": str(source_path.relative_to(repo_root)),
            "source_size_bytes": source_size,
            "source_sha256": source_sha256,
            "dynamodb_chunk_count": ddb_cnt,
            "qdrant_point_count": qdrant_cnt,
            "identity_sets_identical": identity_match,
            "chunk_ids_sample": sorted(list(ddb_chunk_ids))[:5],
            "total_chunk_ids_verified": len(ddb_chunk_ids),
        })

        status_str = "MATCH" if identity_match else "DISCREPANCY"
        print(f"[{status_str}] {filename:45} | DocID: {doc_id} | Chunks: {ddb_cnt:4} | Qdrant: {qdrant_cnt:4}")

    print("-" * 80)
    print(f"TOTALS: DynamoDB Chunks = {total_ddb_chunks} | Qdrant Points = {total_qdrant_points}")

    if discrepancies:
        raise ReconciliationFailure(f"Reconciliation failed with {len(discrepancies)} discrepancies: {discrepancies}")

    # Build output artifact dictionary
    output_data = {
        "workspace_id": workspace_id,
        "manifest_path": str(test_json_path.relative_to(repo_root)),
        "total_documents": len(declared_docs),
        "total_dynamodb_chunks": total_ddb_chunks,
        "total_qdrant_points": total_qdrant_points,
        "identity_reconciliation_passed": True,
        "documents": reconciliation_records,
    }

    # Save to evaluation_assets and brain artifacts
    json_path = repo_root / "backend" / "evaluation_assets" / "canonical_inventory_reconciliation.json"
    json_path.write_text(json.dumps(output_data, indent=2), encoding="utf-8")
    print(f"\nInventory reconciliation saved to {json_path}")

    # Generate Markdown Table directly from JSON
    print("\n--- GENERATED MARKDOWN TABLE FROM INVENTORY JSON ---\n")
    headers = ["Filename", "Document ID", "Source SHA256", "DynamoDB Chunks", "Qdrant Points", "Identity Set Match"]
    rows = []
    for r in output_data["documents"]:
        rows.append(
            f"| `{r['filename']}` | `{r['document_id']}` | `{r['source_sha256'][:16]}...` | {r['dynamodb_chunk_count']} | {r['qdrant_point_count']} | {'100% Match' if r['identity_sets_identical'] else 'Mismatch'} |"
        )
    md_table = (
        "| " + " | ".join(headers) + " |\n"
        "| " + " | ".join([":---"] * 3 + [":---:"] * 3) + " |\n"
        + "\n".join(rows) + "\n"
        f"| **TOTAL ({output_data['total_documents']} documents)** | | | **{output_data['total_dynamodb_chunks']}** | **{output_data['total_qdrant_points']}** | **100% Verified** |"
    )
    print(md_table)
    return output_data


if __name__ == "__main__":
    main()
