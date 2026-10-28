"""Bounded idempotent resume for missing Qdrant vector points.

Targets:
- Workspace: ws_canonical_comparison_0004
- Document: survay.csv (34202080-60de-532e-a0c2-01f45b0af39a)

Design guarantees:
1. Pure read first verification: Queries Qdrant Cloud to determine exact existing point IDs.
2. Reads chunk text and metadata for all 5000 chunks.
3. Calculates deterministic Qdrant point UUID5: uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)).
4. Filters out existing points, identifying only missing points.
5. In default dry run mode, reports:
   - Existing points count
   - Missing points count
   - Estimated Titan V2 embedding calls
   - Estimated token count and cost ($0.02 / 1M tokens)
   - Bails out safely without making paid calls.
6. When --execute is explicitly supplied:
   - Dispatches missing chunks in bounded batches (default batch size 25).
   - Generates local BGE fast embeddings (384-dim) and Bedrock Titan Text Embeddings V2 (1024-dim).
   - Upserts points with wait=False or wait=True with 120s timeout and exponential backoff retry.
   - Verifies completed points by scrolling Qdrant and reporting the updated total.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
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

    logger.info("Parsing source chunks from %s...", csv_path)
    parsed = parse_file(csv_path, document_id=args.document_id, filename=csv_path.name, workspace_id=args.workspace_id)
    all_chunks = list(parsed.chunks)
    logger.info("Total parsed chunks: %d", len(all_chunks))

    missing_chunks = []
    for ch in all_chunks:
        point_id = str(uuid.uuid5(uuid.NAMESPACE_OID, str(ch.id)))
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

        for attempt in range(3):
            try:
                qclient.upsert(
                    collection_name=collection_name,
                    points=points,
                    wait=args.wait,
                )
                upserted_count += len(points)
                logger.info(
                    "Upserted batch %d-%d / %d (points=%d, wait=%s)",
                    i, i + len(points), missing_count, len(points), args.wait
                )
                time.sleep(0.1)
                break
            except Exception as qe:
                logger.warning("Upsert retry attempt %d/3 failed: %s", attempt + 1, qe)
                if attempt == 2:
                    raise
                time.sleep(2.0 * (attempt + 1))

    logger.info("Verifying updated point count in Qdrant...")
    final_ids = set()
    for poll in range(5):
        final_ids = get_existing_point_ids(
            client=qclient,
            collection_name=collection_name,
            workspace_id=args.workspace_id,
            document_id=args.document_id,
        )
        if len(final_ids) >= len(all_chunks) or args.wait:
            break
        logger.info("Eventual consistency poll %d/5: %d/%d points visible...", poll + 1, len(final_ids), len(all_chunks))
        time.sleep(2.0)

    logger.info(
        "Verification complete: Qdrant now holds %d points for document %s (expected %d)",
        len(final_ids), args.document_id, len(all_chunks)
    )


if __name__ == "__main__":
    main()
