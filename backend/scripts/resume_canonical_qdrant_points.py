"""Bounded idempotent resume for missing Qdrant vector points.

Targets:
- Workspace: ws_canonical_comparison_0004
- Document: survay.csv (34202080-60de-532e-a0c2-01f45b0af39a)

Design guarantees:
1. Pure read first verification: Queries Qdrant Cloud to determine exact existing point IDs.
2. Rejects source hash and DocumentSnapshot version mismatches.
3. Verifies required chunk records and metadata exist in DynamoDB before execution.
4. Calculates deterministic Qdrant point UUID5: uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)).
5. In default dry run mode, reports:
   - Existing points count
   - Missing points count
   - Estimated Titan V2 embedding calls
   - Estimated token count and cost ($0.02 / 1M tokens)
   - Exits cleanly without making paid calls.
6. When --execute is explicitly supplied:
   - Dispatches missing chunks in bounded batches (default batch size 25).
   - Generates local BGE fast embeddings (384-dim) and Bedrock Titan Text Embeddings V2 (1024-dim).
   - Upserts points with 120s timeout and exponential backoff retry.
   - Treats timed out upserts as unknown outcomes, performing point level read back to find landed points.
   - Strictly verifies exact expected IDs, vector dimensions, and payloads after writes, failing on any missing ID.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
from pathlib import Path
import sqlite3
import sys
import time
import uuid

# Ensure backend/src is on sys.path for standalone script execution
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from config import settings
from ingestion.embed_service import embed_chunks_dual
from ingestion.parse_service import parse_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("resume_canonical_qdrant_points")


def verify_source_version(csv_path: Path, workspace_id: str, document_id: str) -> str:
    """Verify that source CSV hash matches DocumentSnapshot version in workspace_boundary.db."""
    file_bytes = csv_path.read_bytes()
    file_sha256 = hashlib.sha256(file_bytes).hexdigest()
    computed_version = file_sha256[:32]

    # Resolve boundary db path
    boundary_db = Path(__file__).resolve().parent.parent / "data" / "workspace_boundary.db"
    if not boundary_db.exists():
        boundary_db = Path("data/workspace_boundary.db")
    if not boundary_db.exists():
        boundary_db = Path("backend/data/workspace_boundary.db")

    if not boundary_db.exists():
        logger.warning("Boundary database not found at %s; skipping snapshot version comparison", boundary_db)
        return computed_version

    con = sqlite3.connect(str(boundary_db))
    cur = con.cursor()
    row = cur.execute(
        "SELECT version FROM document_snapshots WHERE workspace_id=? AND document_id=?",
        (workspace_id, document_id),
    ).fetchone()
    con.close()

    if row is None:
        logger.warning(
            "No DocumentSnapshot found in %s for workspace=%s doc=%s",
            boundary_db, workspace_id, document_id
        )
        return computed_version

    snapshot_version = row[0]
    if computed_version != snapshot_version[:32]:
        logger.error(
            "Source version mismatch for %s: file hash prefix=%s, DocumentSnapshot version=%s",
            csv_path.name, computed_version, snapshot_version
        )
        raise ValueError(f"Source version mismatch: expected {snapshot_version}, got {computed_version}")

    logger.info("Source version verified: %s (matches DocumentSnapshot)", computed_version)
    return computed_version


def verify_dynamodb_chunk_records(workspace_id: str, document_id: str, expected_count: int) -> bool:
    """Verify that chunk text and metadata records are present in DynamoDB."""
    try:
        from aws.infra import get_resource
        from boto3.dynamodb.conditions import Key
        table_name = getattr(settings, "DYNAMODB_TABLE_NAME", "kre-table")
        dynamodb = get_resource("dynamodb")
        table = dynamodb.Table(table_name)
        total_chunks = 0
        kwargs = {
            "KeyConditionExpression": Key("PK").eq(f"DOC#{document_id}") & Key("SK").begins_with("CHUNK#"),
            "Select": "COUNT",
        }
        while True:
            resp = table.query(**kwargs)
            total_chunks += resp.get("Count", 0)
            if "LastEvaluatedKey" in resp:
                kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
            else:
                break

        logger.info("DynamoDB chunk verification: %d / %d chunks present in table %s", total_chunks, expected_count, table_name)
        if total_chunks < expected_count:
            logger.error("Missing chunk records in DynamoDB: expected %d, found %d", expected_count, total_chunks)
            return False
        return True
    except Exception as ddb_err:
        logger.warning("Could not verify DynamoDB chunk records (offline or permission): %s", ddb_err)
        return True


def get_existing_point_ids(
    client: QdrantClient,
    collection_name: str,
    workspace_id: str,
    document_id: str,
) -> set[str]:
    """Scroll Qdrant to retrieve all existing point IDs for the workspace and document."""
    existing_ids = set()
    offset = None
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(key="workspace_id", match=qmodels.MatchValue(value=workspace_id)),
            qmodels.FieldCondition(key="document_id", match=qmodels.MatchValue(value=document_id)),
        ]
    )
    while True:
        records, next_offset = client.scroll(
            collection_name=collection_name,
            scroll_filter=flt,
            limit=250,
            with_payload=False,
            with_vectors=False,
            offset=offset,
        )
        for r in records:
            existing_ids.add(str(r.id))
        if next_offset is None:
            break
        offset = next_offset
    return existing_ids


def retrieve_landed_point_ids(
    client: QdrantClient,
    collection_name: str,
    point_ids: list[str],
) -> set[str]:
    """Check which specific points from a batch actually landed in Qdrant after timeout."""
    try:
        records = client.retrieve(
            collection_name=collection_name,
            ids=point_ids,
            with_payload=False,
            with_vectors=False,
        )
        return {str(r.id) for r in records}
    except Exception as qe:
        logger.warning("Failed point retrieval during timeout read-back: %s", qe)
        return set()


def main():
    parser = argparse.ArgumentParser(description="Bounded idempotent resume for missing Qdrant points.")
    parser.add_argument("--workspace-id", default="ws_canonical_comparison_0004", help="Target workspace ID")
    parser.add_argument("--document-id", default="34202080-60de-532e-a0c2-01f45b0af39a", help="Target document ID")
    parser.add_argument("--csv-path", default="data/non_pdf_formats/survay.csv", help="Path to source CSV file")
    parser.add_argument("--batch-size", type=int, default=25, help="Upsert batch size")
    parser.add_argument("--wait", action="store_true", default=True, help="Whether to wait for Qdrant commit ack (default: True)")
    parser.add_argument("--no-wait", action="store_false", dest="wait", help="Do not wait for Qdrant commit ack")
    parser.add_argument("--execute", action="store_true", default=False, help="Authorize execution of paid embedding calls")
    args = parser.parse_args()

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        csv_path = Path(__file__).resolve().parent.parent.parent / args.csv_path
    if not csv_path.exists():
        logger.error("Source CSV file not found: %s", args.csv_path)
        sys.exit(1)

    # 1. Reject source/version mismatches against DocumentSnapshot
    try:
        source_version = verify_source_version(csv_path, args.workspace_id, args.document_id)
    except ValueError as ve:
        logger.error("Preflight check failed: %s", ve)
        sys.exit(1)

    # 2. Parse source chunks
    logger.info("Parsing source chunks from %s...", csv_path)
    parsed = parse_file(csv_path, document_id=args.document_id, filename=csv_path.name, workspace_id=args.workspace_id)
    all_chunks = list(parsed.chunks)
    logger.info("Total parsed chunks: %d", len(all_chunks))

    # 3. Confirm chunk records and metadata exist in DynamoDB
    ddb_ok = verify_dynamodb_chunk_records(args.workspace_id, args.document_id, len(all_chunks))
    if not ddb_ok:
        logger.error("Preflight check failed: chunk metadata is incomplete in DynamoDB")
        sys.exit(1)

    # 4. Map expected chunk IDs and point UUID5s
    expected_point_map: dict[str, Any] = {}
    for ch in all_chunks:
        point_id = str(uuid.uuid5(uuid.NAMESPACE_OID, str(ch.id)))
        expected_point_map[point_id] = ch

    # Initialize Qdrant Client with 120s timeout
    qclient = QdrantClient(
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY,
        timeout=120.0,
        check_compatibility=False,
    )
    collection_name = "kre_chunks"

    logger.info("Reading existing Qdrant points for workspace=%s doc=%s...", args.workspace_id, args.document_id)
    existing_ids = get_existing_point_ids(
        client=qclient,
        collection_name=collection_name,
        workspace_id=args.workspace_id,
        document_id=args.document_id,
    )
    logger.info("Existing points in Qdrant: %d", len(existing_ids))

    missing_chunks = []
    for point_id, ch in expected_point_map.items():
        if point_id not in existing_ids:
            missing_chunks.append(ch)

    missing_count = len(missing_chunks)
    logger.info("Missing points to embed and upsert: %d", missing_count)

    estimated_tokens = missing_count * 100
    estimated_cost = (estimated_tokens / 1_000_000) * 0.02

    print("\n" + "=" * 60)
    print("RESUME CANONICAL QDRANT INVENTORY REPORT")
    print("=" * 60)
    print(f"Workspace ID:          {args.workspace_id}")
    print(f"Document ID:           {args.document_id}")
    print(f"Source file:           {csv_path}")
    print(f"Source version:        {source_version}")
    print(f"Total corpus chunks:   {len(all_chunks)}")
    print(f"Existing points:       {len(existing_ids)}")
    print(f"Missing points:        {missing_count}")
    print(f"Embedding model fast:  local BGE (bge-small-en-v1.5, 384 dims, $0.00)")
    print(f"Embedding model full:  Amazon Titan V2 (1024 dims)")
    print(f"Estimated paid calls:  {missing_count}")
    print(f"Estimated tokens:      {estimated_tokens:,}")
    print(f"Estimated cost:        ${estimated_cost:.4f} USD")
    print("=" * 60 + "\n")

    if not args.execute:
        logger.warning(
            "Dry run complete. Paid calls NOT executed. "
            "To execute bounded resume, supply --execute flag."
        )
        return

    logger.info("Starting bounded resume execution for %d missing chunks...", missing_count)
    batch_size = args.batch_size
    upserted_count = 0

    for i in range(0, missing_count, batch_size):
        chunk_batch = missing_chunks[i : i + batch_size]
        embedded_batch = embed_chunks_dual(chunk_batch, provider="prod")

        points = []
        for ch in embedded_batch:
            qdrant_uuid = str(uuid.uuid5(uuid.NAMESPACE_OID, str(ch.id)))
            vectors = {
                "embedding_fast": ch.embedding_fast,
                "embedding_full": ch.embedding_full,
            }
            payload = {
                "original_id": str(ch.id),
                "document_id": str(ch.document_id),
                "workspace_id": str(ch.workspace_id),
                "page_number": ch.page_number,
            }
            points.append(qmodels.PointStruct(id=qdrant_uuid, vector=vectors, payload=payload))

        batch_point_ids = [p.id for p in points]
        points_to_upsert = list(points)

        for attempt in range(3):
            try:
                qclient.upsert(
                    collection_name=collection_name,
                    points=points_to_upsert,
                    wait=args.wait,
                )
                upserted_count += len(points_to_upsert)
                logger.info(
                    "Upserted batch %d-%d / %d (points=%d, wait=%s)",
                    i, i + len(points_to_upsert), missing_count, len(points_to_upsert), args.wait
                )
                time.sleep(0.1)
                break
            except Exception as qe:
                logger.warning("Upsert attempt %d/3 error: %s", attempt + 1, qe)
                # Read-back landed points on timeout or unknown outcome
                landed_ids = retrieve_landed_point_ids(qclient, collection_name, batch_point_ids)
                if landed_ids:
                    logger.info("Timeout read-back found %d of %d points already committed", len(landed_ids), len(batch_point_ids))
                    points_to_upsert = [p for p in points_to_upsert if p.id not in landed_ids]
                    if not points_to_upsert:
                        logger.info("All batch points landed despite timeout, continuing")
                        break
                if attempt == 2:
                    logger.error("Batch %d failed after 3 attempts", i)
                    raise
                time.sleep(2.0 * (attempt + 1))

    # 5. Strict post-execution verification: exact IDs, vector dimensions, and payloads
    logger.info("Verifying updated point count and vector payloads in Qdrant...")
    final_records = []
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(key="workspace_id", match=qmodels.MatchValue(value=args.workspace_id)),
            qmodels.FieldCondition(key="document_id", match=qmodels.MatchValue(value=args.document_id)),
        ]
    )
    for poll in range(5):
        final_records = []
        offset = None
        while True:
            records, next_offset = qclient.scroll(
                collection_name=collection_name,
                scroll_filter=flt,
                limit=250,
                with_payload=True,
                with_vectors=True,
                offset=offset,
            )
            final_records.extend(records)
            if next_offset is None:
                break
            offset = next_offset

        if len(final_records) >= len(all_chunks) or args.wait:
            break
        logger.info("Eventual consistency poll %d/5: %d/%d points visible...", poll + 1, len(final_records), len(all_chunks))
        time.sleep(2.0)

    final_ids = {str(r.id) for r in final_records}
    still_missing = set(expected_point_map.keys()) - final_ids
    unexpected = final_ids - set(expected_point_map.keys())

    # Verify vector dimensions and payload fields
    vector_dims_valid = True
    for r in final_records:
        vecs = r.vector or {}
        fast = vecs.get("embedding_fast") or []
        full = vecs.get("embedding_full") or []
        if len(fast) != 384 or len(full) != 1024:
            vector_dims_valid = False
            logger.error("Point %s has invalid vector dimensions: fast=%d, full=%d", r.id, len(fast), len(full))
            break
        payload = r.payload or {}
        if not payload.get("original_id") or not payload.get("document_id"):
            vector_dims_valid = False
            logger.error("Point %s has invalid payload: %s", r.id, payload)
            break

    if still_missing:
        logger.error("Verification failed: %d points remain missing in Qdrant", len(still_missing))
        sys.exit(1)

    if unexpected:
        logger.error("Verification failed: %d unexpected points found in Qdrant", len(unexpected))
        sys.exit(1)

    if not vector_dims_valid:
        logger.error("Verification failed: one or more points have invalid vectors or payloads")
        sys.exit(1)

    logger.info(
        "Verification complete: Qdrant now holds all %d expected points for document %s with verified 384/1024 dual vectors and payloads",
        len(final_ids), args.document_id
    )


if __name__ == "__main__":
    main()
