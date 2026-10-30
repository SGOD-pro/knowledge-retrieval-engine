"""Bounded idempotent resume for missing Qdrant vector points.

Targets:
- Workspace: ws_canonical_comparison_0004
- Document: survay.csv (34202080-60de-532e-a0c2-01f45b0af39a)

Design guarantees:
1. Pure read first verification: Queries Qdrant Cloud to determine exact existing point IDs.
2. Fails closed on boundary snapshot lookup failure or source hash mismatch.
3. Verifies required chunk records, IDs, and provenance in DynamoDB before execution.
4. Calculates deterministic Qdrant point UUID5: uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)).
5. Validates existing point identity, payload, and vector dimensions before skipping.
6. Does not infer model provenance from dimensions alone; treats unverifiable provenance as unresolved.
7. In default dry run mode, reports:
   - Existing points count and provenance status
   - Missing points count
   - Unresolved provenance count
   - Estimated Titan V2 embedding calls
   - Estimated token count and cost ($0.02 / 1M tokens)
   - Exits cleanly without making paid calls.
8. When --execute is explicitly supplied:
   - Dispatches target chunks in bounded batches (default batch size 25).
   - Generates local BGE fast embeddings (384-dim) and Bedrock Titan Text Embeddings V2 (1024-dim).
   - Upserts points with 120s timeout and exponential backoff retry.
   - Treats timed out upserts as unknown outcomes, performing point level read back to find landed points.
   - Strictly verifies exact expected IDs, vector dimensions, and payloads after writes, failing on any missing ID.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import sys
import time
from typing import Any
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

SUPPORTED_FAST_MODELS = frozenset({"BAAI/bge-small-en-v1.5", "bge-small-en-v1.5"})
SUPPORTED_FULL_MODELS = frozenset({"amazon.titan-embed-text-v2:0", "amazon.titan-embed-text-v2"})


@dataclass
class PointValidationResult:
    point_id: str
    chunk_id: str
    is_present: bool
    identity_valid: bool
    payload_valid: bool
    vector_dims_valid: bool
    model_provenance_verified: bool
    status: str  # "missing", "valid_verified", "valid_unresolved_provenance", "corrupted"
    reason: str | None = None


def verify_source_version(
    csv_path: Path,
    workspace_id: str,
    document_id: str,
    boundary_repo: Any | None = None,
) -> str:
    """Verify that source CSV hash matches DocumentSnapshot version in configured boundary repo.
    
    Fails closed if boundary repository cannot be resolved, snapshot is missing,
    or source hash prefix does not match snapshot version.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Source file not found: {csv_path}")

    file_bytes = csv_path.read_bytes()
    file_sha256 = hashlib.sha256(file_bytes).hexdigest()
    computed_version = file_sha256[:32]

    if boundary_repo is None:
        try:
            from db.workspace_boundary_repo import get_workspace_boundary_repo
            boundary_repo = get_workspace_boundary_repo(require_durable=True)
        except Exception as repo_err:
            logger.error("Failed to resolve configured workspace boundary repository: %s", repo_err)
            raise RuntimeError(f"Cannot resolve configured workspace boundary repository: {repo_err}") from repo_err

    snapshot = boundary_repo.get_document_snapshot(workspace_id=workspace_id, document_id=document_id)
    if snapshot is None:
        logger.error(
            "No DocumentSnapshot found in configured boundary repository for workspace=%s doc=%s",
            workspace_id,
            document_id,
        )
        raise RuntimeError(
            f"Preflight failed: missing DocumentSnapshot for workspace={workspace_id} doc={document_id}"
        )

    snapshot_version = getattr(snapshot, "version", "")
    if computed_version != snapshot_version[:32]:
        logger.error(
            "Source version mismatch for %s: file hash prefix=%s, DocumentSnapshot version=%s",
            csv_path.name,
            computed_version,
            snapshot_version,
        )
        raise ValueError(
            f"Source version mismatch: expected {snapshot_version}, got {computed_version}"
        )

    logger.info("Source version verified: %s (matches DocumentSnapshot)", computed_version)
    return computed_version


def verify_dynamodb_chunk_records(
    workspace_id: str,
    document_id: str,
    expected_chunk_ids: set[str],
    expected_source_format: str = "csv",
    dynamodb_resource: Any | None = None,
) -> bool:
    """Verify chunk text and metadata records exist in DynamoDB with exact provenance.
    
    Fails closed on read errors, missing chunk IDs, unexpected chunk IDs,
    or workspace/document/format provenance mismatches.
    """
    try:
        from boto3.dynamodb.conditions import Key
        if dynamodb_resource is None:
            from aws.infra import get_resource
            dynamodb_resource = get_resource("dynamodb")
        table_name = getattr(settings, "DYNAMODB_TABLE_NAME", "kre-table")
        table = dynamodb_resource.Table(table_name)

        found_chunk_ids: set[str] = set()
        kwargs = {
            "KeyConditionExpression": Key("PK").eq(f"DOC#{document_id}") & Key("SK").begins_with("CHUNK#"),
            "ProjectionExpression": "#cid, #ws, #doc, #fmt",
            "ExpressionAttributeNames": {
                "#cid": "id",
                "#ws": "workspace_id",
                "#doc": "document_id",
                "#fmt": "source_format",
            },
        }

        while True:
            resp = table.query(**kwargs)
            items = resp.get("Items", [])
            for item in items:
                raw_cid = item.get("id")
                if not raw_cid:
                    raise RuntimeError("DynamoDB chunk record missing required 'id' attribute")
                cid = str(raw_cid)
                ws = str(item.get("workspace_id"))
                doc = str(item.get("document_id"))
                fmt = str(item.get("source_format"))

                if ws != workspace_id:
                    raise RuntimeError(
                        f"Chunk provenance mismatch for {cid}: workspace_id expected {workspace_id}, found {ws}"
                    )
                if doc != document_id:
                    raise RuntimeError(
                        f"Chunk provenance mismatch for {cid}: document_id expected {document_id}, found {doc}"
                    )
                if fmt != expected_source_format:
                    raise RuntimeError(
                        f"Chunk provenance mismatch for {cid}: source_format expected {expected_source_format}, found {fmt}"
                    )
                found_chunk_ids.add(cid)

            if "LastEvaluatedKey" in resp:
                kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
            else:
                break

        missing = expected_chunk_ids - found_chunk_ids
        unexpected = found_chunk_ids - expected_chunk_ids

        if missing:
            logger.error("Missing chunk records in DynamoDB: expected %d, missing %d", len(expected_chunk_ids), len(missing))
            raise RuntimeError(f"DynamoDB chunk verification failed: {len(missing)} chunk records missing in {table_name}")

        if unexpected:
            logger.error("Unexpected chunk records in DynamoDB: %d unexpected items", len(unexpected))
            raise RuntimeError(f"DynamoDB chunk verification failed: {len(unexpected)} unexpected chunk records found in {table_name}")

        logger.info(
            "DynamoDB chunk verification: all %d chunks present with verified provenance in table %s",
            len(found_chunk_ids),
            table_name,
        )
        return True
    except Exception as ddb_err:
        logger.error("DynamoDB chunk verification failed: %s", ddb_err)
        raise RuntimeError(f"Preflight DynamoDB chunk verification failed: {ddb_err}") from ddb_err


def inspect_existing_qdrant_points(
    client: QdrantClient,
    collection_name: str,
    workspace_id: str,
    document_id: str,
    expected_point_map: dict[str, Any],
) -> tuple[dict[str, PointValidationResult], set[str]]:
    """Scroll Qdrant and validate point identity, required payload, and vector dimensions.
    
    Treats unverifiable model provenance as unresolved; does not infer model provenance
    from dimensions alone.
    Returns (results_by_point_id, unexpected_point_ids).
    """
    results: dict[str, PointValidationResult] = {}
    unexpected_ids: set[str] = set()

    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(key="workspace_id", match=qmodels.MatchValue(value=workspace_id)),
            qmodels.FieldCondition(key="document_id", match=qmodels.MatchValue(value=document_id)),
        ]
    )

    offset = None
    while True:
        records, next_offset = client.scroll(
            collection_name=collection_name,
            scroll_filter=flt,
            limit=250,
            with_payload=True,
            with_vectors=True,
            offset=offset,
        )
        for r in records:
            point_id = str(r.id)
            if point_id not in expected_point_map:
                unexpected_ids.add(point_id)
                continue

            expected_chunk = expected_point_map[point_id]
            expected_uuid = str(uuid.uuid5(uuid.NAMESPACE_OID, str(expected_chunk.id)))
            identity_valid = (point_id == expected_uuid)

            payload = r.payload or {}
            orig_id = payload.get("original_id")
            p_doc = payload.get("document_id")
            p_ws = payload.get("workspace_id")
            payload_valid = (
                orig_id == str(expected_chunk.id)
                and p_doc == document_id
                and p_ws == workspace_id
            )

            vecs = r.vector or {}
            fast = vecs.get("embedding_fast") if isinstance(vecs, dict) else None
            full = vecs.get("embedding_full") if isinstance(vecs, dict) else None
            vector_dims_valid = (
                isinstance(vecs, dict)
                and len(fast or []) == 384
                and len(full or []) == 1024
            )

            fast_model = payload.get("fast_model") or payload.get("model_fast")
            full_model = payload.get("full_model") or payload.get("model_full")
            has_fast_tag = bool(fast_model)
            has_full_tag = bool(full_model)

            fast_compatible = fast_model in SUPPORTED_FAST_MODELS
            full_compatible = full_model in SUPPORTED_FULL_MODELS
            model_provenance_verified = fast_compatible and full_compatible

            if not identity_valid:
                status = "corrupted"
                reason = f"Identity mismatch: point_id={point_id} != expected_uuid={expected_uuid}"
            elif not payload_valid:
                status = "corrupted"
                reason = f"Payload mismatch: original_id={orig_id}, doc={p_doc}, ws={p_ws}"
            elif not vector_dims_valid:
                fast_len = len(fast or []) if isinstance(fast, (list, tuple)) else 0
                full_len = len(full or []) if isinstance(full, (list, tuple)) else 0
                status = "corrupted"
                reason = f"Vector dimensions mismatch: fast={fast_len}/384, full={full_len}/1024"
            elif has_fast_tag or has_full_tag:
                if model_provenance_verified:
                    status = "valid_verified"
                    reason = f"Verified fast_model={fast_model}, full_model={full_model}"
                else:
                    status = "corrupted"
                    reason = (
                        f"Incompatible or unverified model provenance: "
                        f"fast_model={fast_model!r} (expected {sorted(SUPPORTED_FAST_MODELS)}), "
                        f"full_model={full_model!r} (expected {sorted(SUPPORTED_FULL_MODELS)})"
                    )
            else:
                status = "valid_unresolved_provenance"
                reason = "Valid geometry (384/1024) and payload, but model provenance is unverified in store payload"

            results[point_id] = PointValidationResult(
                point_id=point_id,
                chunk_id=str(expected_chunk.id),
                is_present=True,
                identity_valid=identity_valid,
                payload_valid=payload_valid,
                vector_dims_valid=vector_dims_valid,
                model_provenance_verified=model_provenance_verified,
                status=status,
                reason=reason,
            )

        if next_offset is None:
            break
        offset = next_offset

    # Mark expected points that were not found in store
    for point_id, chunk in expected_point_map.items():
        if point_id not in results:
            results[point_id] = PointValidationResult(
                point_id=point_id,
                chunk_id=str(chunk.id),
                is_present=False,
                identity_valid=True,
                payload_valid=False,
                vector_dims_valid=False,
                model_provenance_verified=False,
                status="missing",
                reason="Point not present in Qdrant store",
            )

    return results, unexpected_ids


def retrieve_landed_point_ids(
    client: QdrantClient,
    collection_name: str,
    point_ids: list[str],
    workspace_id: str,
    document_id: str,
    expected_point_map: dict[str, Any],
    require_verified_provenance: bool = False,
) -> set[str]:
    """Check which specific points from a batch actually landed in Qdrant after timeout.
    
    Validates retrieved point payload, vector dimensions, and model provenance before considering them landed.
    """
    try:
        records = client.retrieve(
            collection_name=collection_name,
            ids=point_ids,
            with_payload=True,
            with_vectors=True,
        )
        landed_valid_ids = set()
        for r in records:
            pid = str(r.id)
            if pid not in expected_point_map:
                continue
            expected_chunk = expected_point_map[pid]
            payload = r.payload or {}
            vecs = r.vector or {}
            fast = vecs.get("embedding_fast") if isinstance(vecs, dict) else None
            full = vecs.get("embedding_full") if isinstance(vecs, dict) else None

            matches_prov = True
            if require_verified_provenance:
                p_fast = payload.get("fast_model") or payload.get("model_fast")
                p_full = payload.get("full_model") or payload.get("model_full")
                matches_prov = p_fast in SUPPORTED_FAST_MODELS and p_full in SUPPORTED_FULL_MODELS

            if (
                payload.get("original_id") == str(expected_chunk.id)
                and payload.get("document_id") == document_id
                and payload.get("workspace_id") == workspace_id
                and len(fast or []) == 384
                and len(full or []) == 1024
                and matches_prov
            ):
                landed_valid_ids.add(pid)
        return landed_valid_ids
    except Exception as qe:
        logger.warning("Failed point retrieval during timeout read-back: %s", qe)
        return set()


def verify_qdrant_post_write(
    client: QdrantClient,
    collection_name: str,
    workspace_id: str,
    document_id: str,
    expected_point_map: dict[str, Any],
    wait: bool = True,
    require_verified_provenance: bool = False,
) -> tuple[bool, str]:
    """Strict post-write verification: exact IDs, vector dimensions, and payloads.
    
    Returns (True, success_message) or (False, error_message).
    """
    logger.info("Performing post-write verification against Qdrant collection %s...", collection_name)
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(key="workspace_id", match=qmodels.MatchValue(value=workspace_id)),
            qmodels.FieldCondition(key="document_id", match=qmodels.MatchValue(value=document_id)),
        ]
    )

    final_records = []
    max_polls = 1 if wait else 5
    for poll in range(max_polls):
        final_records = []
        offset = None
        while True:
            records, next_offset = client.scroll(
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

        if len(final_records) >= len(expected_point_map) or wait:
            break
        logger.info("Eventual consistency poll %d/%d: %d/%d points visible...", poll + 1, max_polls, len(final_records), len(expected_point_map))
        time.sleep(2.0)

    found_ids = {str(r.id) for r in final_records}
    expected_ids = set(expected_point_map.keys())

    missing = expected_ids - found_ids
    unexpected = found_ids - expected_ids

    if missing:
        msg = f"Post-write verification failed: {len(missing)} points remain missing in Qdrant"
        logger.error(msg)
        return False, msg

    if unexpected:
        msg = f"Post-write verification failed: {len(unexpected)} unexpected points found in Qdrant"
        logger.error(msg)
        return False, msg

    for r in final_records:
        pid = str(r.id)
        expected_chunk = expected_point_map[pid]
        payload = r.payload or {}
        orig_id = payload.get("original_id")
        p_doc = payload.get("document_id")
        p_ws = payload.get("workspace_id")

        if orig_id != str(expected_chunk.id) or p_doc != document_id or p_ws != workspace_id:
            msg = f"Post-write verification failed: Point {pid} has invalid payload {payload}"
            logger.error(msg)
            return False, msg

        if require_verified_provenance:
            fast_model = payload.get("fast_model") or payload.get("model_fast")
            full_model = payload.get("full_model") or payload.get("model_full")
            if fast_model not in SUPPORTED_FAST_MODELS or full_model not in SUPPORTED_FULL_MODELS:
                msg = (
                    f"Post-write verification failed: Point {pid} lacks exact verified model provenance "
                    f"(fast_model={fast_model!r}, full_model={full_model!r})"
                )
                logger.error(msg)
                return False, msg

        vecs = r.vector or {}
        fast = vecs.get("embedding_fast") if isinstance(vecs, dict) else None
        full = vecs.get("embedding_full") if isinstance(vecs, dict) else None
        if len(fast or []) != 384 or len(full or []) != 1024:
            msg = f"Post-write verification failed: Point {pid} has invalid vector dimensions fast={len(fast or [])}, full={len(full or [])}"
            logger.error(msg)
            return False, msg

    success_msg = f"Post-write verification succeeded: all {len(found_ids)} points verified in Qdrant with valid payloads and 384/1024 dual vectors"
    logger.info(success_msg)
    return True, success_msg


def validate_batch_before_upsert(
    points: list[Any],
    chunk_batch: list[Any],
    workspace_id: str,
    document_id: str,
    expected_point_map: dict[str, Any],
) -> tuple[bool, str]:
    """Validate produced vectors, model provenance, and point payload before Qdrant upsert.

    Guarantees no invalid, fallback, mixed, or unverified vector is ever written to Qdrant.
    """
    if len(points) != len(chunk_batch):
        return False, f"Incomplete batch: points count ({len(points)}) != chunks count ({len(chunk_batch)})"

    for p, ch in zip(points, chunk_batch):
        expected_id = str(uuid.uuid5(uuid.NAMESPACE_OID, str(ch.id)))
        if getattr(p, "id", None) != expected_id:
            return False, f"Point ID mismatch: point.id={getattr(p, 'id', None)} != expected={expected_id}"

        if expected_id not in expected_point_map:
            return False, f"Point ID {expected_id} not found in expected point map"
        if str(expected_point_map[expected_id].id) != str(ch.id):
            return False, f"Point ID {expected_id} chunk ID mismatch in expected point map"

        payload = getattr(p, "payload", {}) or {}
        p_orig = payload.get("original_id")
        p_doc = payload.get("document_id")
        p_ws = payload.get("workspace_id")
        if p_orig != str(ch.id) or p_doc != document_id or p_ws != workspace_id:
            return False, f"Payload identity mismatch for point {p.id}: orig={p_orig}, doc={p_doc}, ws={p_ws}"

        vecs = getattr(p, "vector", {}) or {}
        fast = vecs.get("embedding_fast") if isinstance(vecs, dict) else None
        full = vecs.get("embedding_full") if isinstance(vecs, dict) else None

        if not isinstance(fast, (list, tuple)) or len(fast) != 384:
            return (
                False,
                f"Point {p.id} fast embedding dimension invalid: "
                f"{len(fast or []) if isinstance(fast, (list, tuple)) else 'none'} != 384",
            )
        if not isinstance(full, (list, tuple)) or len(full) != 1024:
            return (
                False,
                f"Point {p.id} full embedding dimension invalid: "
                f"{len(full or []) if isinstance(full, (list, tuple)) else 'none'} != 1024",
            )

        fast_model = payload.get("fast_model") or payload.get("model_fast")
        full_model = payload.get("full_model") or payload.get("model_full")

        if not fast_model:
            return False, f"Point {p.id} lacks fast_model provenance tag"
        if str(fast_model).startswith("fallback") or fast_model not in SUPPORTED_FAST_MODELS:
            return (
                False,
                f"Point {p.id} has invalid or fallback fast_model provenance: {fast_model!r} "
                f"(expected {sorted(SUPPORTED_FAST_MODELS)})",
            )

        if not full_model:
            return False, f"Point {p.id} lacks full_model provenance tag"
        if (
            str(full_model).startswith("fallback")
            or str(full_model).startswith("mixed")
            or str(full_model).startswith("mock")
            or full_model not in SUPPORTED_FULL_MODELS
        ):
            return (
                False,
                f"Point {p.id} has invalid, fallback, or mixed full_model provenance: {full_model!r} "
                f"(expected {sorted(SUPPORTED_FULL_MODELS)})",
            )

    return True, f"All {len(points)} points in batch verified for upsert"


def verify_provider_availability(
    require_bge_lambda: bool = True,
    require_bedrock: bool = True,
) -> tuple[bool, str, dict[str, Any]]:
    """Read-only preflight check verifying that required embedding providers are reachable and configured."""
    from config import settings
    import os

    details: dict[str, Any] = {}
    is_test_env = getattr(settings, "ENVIRONMENT", "") == "test" or os.environ.get("ENVIRONMENT") == "test"
    if is_test_env:
        msg = "Preflight check failed: ENVIRONMENT is configured as 'test'. Live provider execution disabled."
        logger.error(msg)
        return False, msg, details

    from aws.infra import get_client

    if require_bge_lambda:
        func_name = getattr(settings, "BGE_EMBEDDING_LAMBDA_NAME", "") or "bge-text-embeder"
        details["bge_lambda_function"] = func_name
        try:
            lam = get_client("lambda")
            config = lam.get_function_configuration(FunctionName=func_name)
            state = config.get("State", "Unknown")
            details["bge_lambda_state"] = state
            if state != "Active":
                msg = f"Preflight check failed: BGE Lambda {func_name!r} state is {state!r} (expected 'Active')"
                logger.error(msg)
                return False, msg, details
        except Exception as e:
            msg = f"Preflight check failed: Unable to verify BGE Lambda {func_name!r}: {e}"
            logger.error(msg)
            return False, msg, details

    if require_bedrock:
        model_id = getattr(settings, "PROD_EMBEDDING_MODEL", "")
        details["bedrock_model_id"] = model_id
        if model_id not in SUPPORTED_FULL_MODELS:
            msg = (
                f"Preflight check failed: Configured PROD_EMBEDDING_MODEL {model_id!r} not in supported "
                f"models {sorted(SUPPORTED_FULL_MODELS)}"
            )
            logger.error(msg)
            return False, msg, details
        try:
            sts = get_client("sts")
            ident = sts.get_caller_identity()
            arn = ident.get("Arn", "unknown")
            details["aws_caller_arn"] = arn
        except Exception as e:
            msg = f"Preflight check failed: AWS authentication check failed: {e}"
            logger.error(msg)
            return False, msg, details

    msg = (
        f"Provider preflight verified: BGE Lambda={details.get('bge_lambda_function')} ({details.get('bge_lambda_state')}), "
        f"Bedrock={details.get('bedrock_model_id')} (Caller={details.get('aws_caller_arn')})"
    )
    logger.info(msg)
    return True, msg, details


def main():
    parser = argparse.ArgumentParser(description="Bounded idempotent resume for missing Qdrant points.")
    parser.add_argument("--workspace-id", default="ws_canonical_comparison_0004", help="Target workspace ID")
    parser.add_argument("--document-id", default="34202080-60de-532e-a0c2-01f45b0af39a", help="Target document ID")
    parser.add_argument("--csv-path", default="data/non_pdf_formats/survay.csv", help="Path to source CSV file")
    parser.add_argument("--batch-size", type=int, default=25, help="Upsert batch size")
    parser.add_argument("--wait", action="store_true", default=True, help="Whether to wait for Qdrant commit ack (default: True)")
    parser.add_argument("--no-wait", action="store_false", dest="wait", help="Do not wait for Qdrant commit ack")
    parser.add_argument(
        "--allow-unresolved-provenance",
        action="store_true",
        default=False,
        help="Skip existing points that match ID, payload, and 384/1024 dimensions even if model provenance is unverified in store payload",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="Maximum number of batches to process before halting (useful for sample batch verification)",
    )
    parser.add_argument("--execute", action="store_true", default=False, help="Authorize execution of paid embedding calls")
    args = parser.parse_args()

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        csv_path = Path(__file__).resolve().parent.parent.parent / args.csv_path
    if not csv_path.exists():
        logger.error("Source CSV file not found: %s", args.csv_path)
        sys.exit(1)

    # 1. Reject source/version mismatches against DocumentSnapshot in configured boundary repo
    try:
        source_version = verify_source_version(csv_path, args.workspace_id, args.document_id)
    except Exception as ve:
        logger.error("Preflight check failed: %s", ve)
        sys.exit(1)

    # 2. Parse source chunks
    logger.info("Parsing source chunks from %s...", csv_path)
    parsed = parse_file(csv_path, document_id=args.document_id, filename=csv_path.name, workspace_id=args.workspace_id)
    all_chunks = list(parsed.chunks)
    logger.info("Total parsed chunks: %d", len(all_chunks))
    expected_chunk_ids = {str(ch.id) for ch in all_chunks}

    # 3. Confirm chunk records and metadata exist in DynamoDB with exact provenance
    try:
        verify_dynamodb_chunk_records(args.workspace_id, args.document_id, expected_chunk_ids, expected_source_format="csv")
    except Exception as ddb_err:
        logger.error("Preflight check failed: %s", ddb_err)
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

    logger.info("Inspecting existing Qdrant points for workspace=%s doc=%s...", args.workspace_id, args.document_id)
    inspection_results, unexpected_ids = inspect_existing_qdrant_points(
        client=qclient,
        collection_name=collection_name,
        workspace_id=args.workspace_id,
        document_id=args.document_id,
        expected_point_map=expected_point_map,
    )

    missing_results = [r for r in inspection_results.values() if r.status == "missing"]
    corrupted_results = [r for r in inspection_results.values() if r.status == "corrupted"]
    unresolved_results = [r for r in inspection_results.values() if r.status == "valid_unresolved_provenance"]
    verified_results = [r for r in inspection_results.values() if r.status == "valid_verified"]

    existing_total = len(inspection_results) - len(missing_results)

    # Determine candidate chunks for embedding
    if args.allow_unresolved_provenance:
        target_point_ids = {r.point_id for r in missing_results} | {r.point_id for r in corrupted_results}
        skipped_count = len(verified_results) + len(unresolved_results)
    else:
        target_point_ids = {r.point_id for r in missing_results} | {r.point_id for r in corrupted_results} | {r.point_id for r in unresolved_results}
        skipped_count = len(verified_results)

    target_chunks = [ch for ch in all_chunks if str(uuid.uuid5(uuid.NAMESPACE_OID, str(ch.id))) in target_point_ids]
    target_count = len(target_chunks)

    # Cost calculations
    cost_missing_only = (len(missing_results) * 100 / 1_000_000) * 0.02
    cost_target = (target_count * 100 / 1_000_000) * 0.02
    cost_all_5000 = (len(all_chunks) * 100 / 1_000_000) * 0.02

    # Provider availability preflight (read-only)
    provider_ok, provider_msg, provider_details = verify_provider_availability()

    print("\n" + "=" * 65)
    print("RESUME CANONICAL QDRANT INVENTORY REPORT")
    print("=" * 65)
    print(f"Workspace ID:                     {args.workspace_id}")
    print(f"Document ID:                      {args.document_id}")
    print(f"Source file:                      {csv_path}")
    print(f"Source version:                   {source_version}")
    print(f"Total corpus chunks:              {len(all_chunks):,}")
    print(f"Existing points in store:         {existing_total:,}")
    print(f"  - Verified model provenance:    {len(verified_results):,}")
    print(f"  - Unresolved provenance:        {len(unresolved_results):,}")
    print(f"  - Corrupted (payload/vectors):  {len(corrupted_results):,}")
    print(f"Missing points:                   {len(missing_results):,}")
    print(f"Unexpected points:                {len(unexpected_ids):,}")
    print(f"Skipped points:                   {skipped_count:,}")
    print(f"Target points to embed/write:     {target_count:,}")
    print(f"Allow unresolved provenance:      {args.allow_unresolved_provenance}")
    print("-" * 65)
    print(f"Provider preflight status:        {'VERIFIED AVAILABLE' if provider_ok else 'UNAVAILABLE / BLOCKED'}")
    if provider_ok:
        print(f"  - BGE Lambda function:          {provider_details.get('bge_lambda_function')} ({provider_details.get('bge_lambda_state')})")
        print(f"  - Bedrock target model:         {provider_details.get('bedrock_model_id')}")
        print(f"  - AWS caller identity:          {provider_details.get('aws_caller_arn')}")
    else:
        print(f"  - Preflight reason:             {provider_msg}")
    print("-" * 65)
    print(f"Embedding model fast:             local BGE (bge-small-en-v1.5, 384 dims, $0.00)")
    print(f"Embedding model full:             Amazon Titan V2 (1024 dims, $0.02/1M tokens)")
    print(f"Cost estimate (target {target_count:,} chunks):    ${cost_target:.4f} USD (~{target_count * 100:,} tokens)")
    print(f"Cost estimate (missing only 4,950): ${cost_missing_only:.4f} USD (~{len(missing_results) * 100:,} tokens)")
    print(f"Cost estimate (all 5,000 chunks):  ${cost_all_5000:.4f} USD (~{len(all_chunks) * 100:,} tokens)")
    print("=" * 65 + "\n")

    if unexpected_ids:
        logger.warning("Found %d unexpected point IDs in collection: %s", len(unexpected_ids), list(unexpected_ids)[:5])

    if not args.execute:
        logger.warning(
            "Dry run complete. Paid calls NOT executed. "
            "To execute bounded resume, supply --execute flag."
        )
        return

    if not provider_ok:
        logger.error("Provider availability preflight failed. Halting before executing paid calls or writes: %s", provider_msg)
        sys.exit(1)

    logger.info("Starting bounded resume execution for %d target chunks...", target_count)
    batch_size = args.batch_size
    upserted_count = 0
    completed_batches = 0
    completed_point_ids: list[str] = []
    total_planned_batches = (target_count + batch_size - 1) // batch_size

    for batch_idx, i in enumerate(range(0, target_count, batch_size)):
        if args.max_batches is not None and batch_idx >= args.max_batches:
            logger.info("Reached maximum requested batches (%d). Halting bounded execution.", args.max_batches)
            break

        chunk_batch = target_chunks[i : i + batch_size]
        try:
            embedded_batch = embed_chunks_dual(chunk_batch, provider="prod")
        except Exception as embed_err:
            logger.error(
                "Embedding failed at batch %d (offset %d): %s. Completed %d points across %d batches.",
                batch_idx + 1,
                i,
                embed_err,
                len(completed_point_ids),
                completed_batches,
            )
            logger.error("Completed point IDs: %s", completed_point_ids)
            raise

        points = []
        for ch in embedded_batch:
            qdrant_uuid = str(uuid.uuid5(uuid.NAMESPACE_OID, str(ch.id)))
            vectors = {
                "embedding_fast": ch.embedding_fast,
                "embedding_full": ch.embedding_full,
            }
            ch_meta = getattr(ch, "metadata", {}) or {}
            actual_fast = ch_meta.get("fast_model")
            actual_full = ch_meta.get("full_model")

            payload = {
                "original_id": str(ch.id),
                "document_id": str(ch.document_id),
                "workspace_id": str(ch.workspace_id),
                "page_number": ch.page_number,
            }
            # Write exact canonical model if established; write fallback origin string otherwise for downstream validation
            if actual_fast in SUPPORTED_FAST_MODELS:
                payload["fast_model"] = actual_fast
            elif actual_fast:
                payload["fast_model"] = str(actual_fast)

            if actual_full in SUPPORTED_FULL_MODELS:
                payload["full_model"] = actual_full
            elif actual_full:
                payload["full_model"] = str(actual_full)

            points.append(qmodels.PointStruct(id=qdrant_uuid, vector=vectors, payload=payload))

        batch_point_ids = [p.id for p in points]
        points_to_upsert = list(points)

        # Pre-upsert batch validation gate
        is_valid_batch, batch_reason = validate_batch_before_upsert(
            points=points_to_upsert,
            chunk_batch=chunk_batch,
            workspace_id=args.workspace_id,
            document_id=args.document_id,
            expected_point_map=expected_point_map,
        )
        if not is_valid_batch:
            logger.error(
                "PRE-UPSERT VALIDATION REJECTED batch %d (chunks %d-%d / %d): %s",
                batch_idx + 1,
                i,
                i + len(chunk_batch),
                target_count,
                batch_reason,
            )
            logger.error(
                "RESUME HALTED TO PREVENT STORE CONTAMINATION. "
                "Completed batches: %d, Completed points: %d, Safe resume offset: %d, Remaining chunks: %d",
                completed_batches,
                upserted_count,
                i,
                target_count - upserted_count,
            )
            logger.error("Completed point IDs: %s", completed_point_ids)
            sys.exit(1)

        for attempt in range(3):
            try:
                qclient.upsert(
                    collection_name=collection_name,
                    points=points_to_upsert,
                    wait=args.wait,
                )
                upserted_count += len(points_to_upsert)
                completed_batches += 1
                completed_point_ids.extend(batch_point_ids)
                logger.info(
                    "Upserted batch %d-%d / %d (points=%d, wait=%s)",
                    i, i + len(points_to_upsert), target_count, len(points_to_upsert), args.wait
                )
                time.sleep(0.1)
                break
            except Exception as qe:
                logger.warning("Upsert attempt %d/3 error: %s", attempt + 1, qe)
                # Read-back landed points on timeout or unknown outcome
                landed_ids = retrieve_landed_point_ids(
                    qclient,
                    collection_name,
                    batch_point_ids,
                    args.workspace_id,
                    args.document_id,
                    expected_point_map,
                    require_verified_provenance=not args.allow_unresolved_provenance,
                )
                if landed_ids:
                    logger.info("Timeout read-back found %d of %d points already committed", len(landed_ids), len(batch_point_ids))
                    points_to_upsert = [p for p in points_to_upsert if p.id not in landed_ids]
                    if not points_to_upsert:
                        upserted_count += len(batch_point_ids)
                        completed_batches += 1
                        completed_point_ids.extend(batch_point_ids)
                        logger.info("All batch points landed despite timeout, continuing")
                        break
                if attempt == 2:
                    logger.error(
                        "Batch %d failed after 3 attempts. Completed %d points across %d batches.",
                        batch_idx + 1,
                        len(completed_point_ids),
                        completed_batches,
                    )
                    logger.error("Completed point IDs: %s", completed_point_ids)
                    raise
                time.sleep(2.0 * (attempt + 1))

    logger.info("Batch upserts completed: successfully upserted %d points", upserted_count)

    is_partial_run = args.max_batches is not None and completed_batches < total_planned_batches
    if is_partial_run:
        logger.info(
            "Sample run of %d batch(es) completed (%d points upserted). Performing sample read-back verification...",
            completed_batches,
            upserted_count,
        )
        sample_point_ids = completed_point_ids
        landed_sample_ids = retrieve_landed_point_ids(
            qclient,
            collection_name,
            sample_point_ids,
            args.workspace_id,
            args.document_id,
            expected_point_map,
            require_verified_provenance=not args.allow_unresolved_provenance,
        )
        if len(landed_sample_ids) != len(sample_point_ids):
            missing_sample = set(sample_point_ids) - landed_sample_ids
            msg = f"Sample batch verification failed: {len(missing_sample)} of {len(sample_point_ids)} points failed read-back verification"
            logger.error(msg)
            sys.exit(1)
        logger.info(
            "Sample batch verification verified: all %d sample points confirmed in Qdrant with valid provenance.",
            len(landed_sample_ids),
        )
        return

    # 5. Strict post-execution verification: exact IDs, vector dimensions, and payloads
    ok, vmsg = verify_qdrant_post_write(
        client=qclient,
        collection_name=collection_name,
        workspace_id=args.workspace_id,
        document_id=args.document_id,
        expected_point_map=expected_point_map,
        wait=args.wait,
        require_verified_provenance=not args.allow_unresolved_provenance,
    )
    if not ok:
        logger.error("Resume failed post-write verification: %s", vmsg)
        sys.exit(1)

    logger.info("Resume process completed successfully: %s", vmsg)
    from providers.embedding_provider import _token_counter
    actual_tokens = _token_counter.get("embed_input_tokens", 0)
    actual_calls = _token_counter.get("embed_calls", 0)
    actual_cost = (actual_tokens / 1_000_000) * 0.02
    print("\n" + "=" * 65)
    print("RESUME EXECUTION SUMMARY")
    print("=" * 65)
    print(f"Target points upserted:          {upserted_count:,}")
    print(f"Total batches completed:         {completed_batches:,}")
    print(f"BGE Lambda batch calls:          {completed_batches:,}")
    print(f"Bedrock Titan embed calls:       {actual_calls:,}")
    print(f"Bedrock Titan input tokens:      {actual_tokens:,}")
    print(f"Observed Bedrock cost:           ${actual_cost:.4f} USD")
    print(f"Qdrant post-write verification:  {vmsg}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
