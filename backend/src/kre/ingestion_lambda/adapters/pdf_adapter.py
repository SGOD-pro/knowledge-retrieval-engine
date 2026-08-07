"""PDF adapter — invokes odl-parser-lambda via boto3.

ARCHITECTURE.md rev 5: PDF Extraction Lambda is a separate container image
deployed to ECR, invoked synchronously via boto3 from the Ingestion Lambda.

DECISION.md: Payload IN is a documents[] array with {document_id, s3_bucket, s3_key}.
Payload OUT is {results: [...], failed: [...]}.

Response shape expected from odl-parser (post Prompt 1 normalizer fix):
    {
        "results": [
            {
                "document_id": "<uuid>",
                "elements": [...],          # list of element dicts
                "image_s3_keys": [...]      # S3 keys of extracted images (may be absent)
            }
        ],
        "failed": ["<uuid>", ...]           # document_ids that errored
    }

The prod invoke payload wraps the request in {"documents": [...]} to match
the odl-parser normalizer contract (same shape as the dev direct-import path).
"""

import json
import logging
import os
import sys

import boto3

from kre.shared.models import Chunk

logger = logging.getLogger(__name__)

# Lambda function name — configurable via env var for dev/prod routing
_ODL_PARSER_FUNCTION_NAME = os.environ.get("ODL_PARSER_LAMBDA_NAME", "odl-parser-lambda")


def parse(path, document_id: str) -> list[Chunk]:
    """Invoke odl-parser-lambda synchronously or locally for dev.

    Args:
        path: Path to the local PDF file.
        document_id: UUID for the document being ingested.

    Returns:
        List of Chunk objects parsed from the PDF.

    Raises:
        RuntimeError: If odl-parser returns a Lambda-level error, or if
            document_id appears in the response's "failed" list.
    """
    environment = os.environ.get("ENVIRONMENT", "dev")

    if environment == "dev":
        # DEV BYPASS: Direct import to avoid S3 dependency.
        # odl/main.py lambda_handler expects a documents[] batch event.
        odl_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "odl"))
        if odl_path not in sys.path:
            sys.path.append(odl_path)
        import main as odl_main

        event = {
            "documents": [{
                "document_id": document_id,
                "s3_bucket": os.environ.get("S3_BUCKET_NAME", "kre-documents-dev"),
                "s3_key": str(path),
            }]
        }
        batch_response = odl_main.lambda_handler(event, None)
        # Dev path: odl_main already returns {results: [...], failed: [...]}.
        # We re-use the same result-parsing block below — no special-casing needed.
        response_payload = batch_response

    else:
        # PROD PATH: Invoke deployed Lambda via boto3.
        #
        # Payload wraps the request in {"documents": [...]} so it matches the
        # odl-parser normalizer contract (Prompt 1 fix). Flat {s3_bucket,
        # s3_key, document_id} was wrong — the Lambda expects a batch envelope.
        from kre.shared.aws import get_client
        client = get_client("lambda")

        s3_bucket = os.environ.get("S3_BUCKET_NAME", "kre-documents-prod")
        s3_key = path.name

        payload = {
            "documents": [{
                "document_id": document_id,
                "s3_bucket": s3_bucket,
                "s3_key": s3_key,
            }]
        }

        logger.info("Invoking %s for s3://%s/%s", _ODL_PARSER_FUNCTION_NAME, s3_bucket, s3_key)

        response = client.invoke(
            FunctionName=_ODL_PARSER_FUNCTION_NAME,
            InvocationType="RequestResponse",  # Synchronous
            Payload=json.dumps(payload),
        )

        response_payload = json.loads(response["Payload"].read())

        if response.get("FunctionError"):
            error_msg = response_payload.get("errorMessage", "Unknown error")
            logger.error("odl-parser-lambda returned error: %s", error_msg)
            raise RuntimeError(f"PDF extraction failed: {error_msg}")

    # ---------------------------------------------------------------------------
    # Parse the normalised response_payload: {results: [...], failed: [...]}
    # ---------------------------------------------------------------------------

    # Check the failed list first — raise early rather than silently returning [].
    failed_ids: list[str] = response_payload.get("failed", [])
    if document_id in failed_ids:
        raise RuntimeError(
            f"odl-parser reported extraction failure for document_id={document_id!r}. "
            f"Full failed list: {failed_ids}"
        )

    results: list[dict] = response_payload.get("results", [])
    if not results:
        logger.warning("odl-parser returned empty results for document_id=%s", document_id)
        return []

    # Find our document's result entry (there should be exactly one in a
    # single-document invocation, but be defensive and match by id).
    doc_result: dict | None = None
    for entry in results:
        if str(entry.get("document_id", "")) == document_id:
            doc_result = entry
            break
    if doc_result is None:
        # Fall back to first entry if document_id matching fails
        logger.warning(
            "odl-parser result missing document_id=%s; falling back to results[0]",
            document_id,
        )
        doc_result = results[0]

    # Real element data is at results[0]["elements"] — NOT at the top-level dict.
    raw_elements = doc_result.get("elements", [])
    if isinstance(raw_elements, dict):
        # opendataloader_pdf sometimes returns {"kids": [...]} or {"text": [...]} instead of a flat list
        items: list[dict] = raw_elements.get("kids", [])
        if not items:
            items = raw_elements.get("text", [])
        if not items:
            items = raw_elements.get("elements", [])
    else:
        items: list[dict] = raw_elements

    # Image S3 keys for this document (may be absent for text-only PDFs).
    doc_image_s3_keys: tuple[str, ...] = tuple(doc_result.get("image_s3_keys", []))

    chunks: list[Chunk] = []

    for index, element in enumerate(items):
        if not isinstance(element, dict):
            continue
        text = str(element.get("content") or element.get("source") or element.get("text") or "").strip()
        if not text or (element.get("type") == "image" and text.endswith((".png", ".jpg", ".jpeg"))):
            continue

        page_number = int(element.get("page number") or element.get("page_number") or element.get("page") or 1)
        raw_box = element.get("bounding box") or element.get("bounding_box") or element.get("bbox")
        bounding_box = None
        if isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
            bounding_box = {"x1": float(raw_box[0]), "y1": float(raw_box[1]), "x2": float(raw_box[2]), "y2": float(raw_box[3])}
        elif isinstance(raw_box, dict):
            bounding_box = {k: float(v) for k, v in raw_box.items()}

        element_type = str(element.get("type") or element.get("element_type") or "paragraph")

        chunks.append(Chunk(
            id=f"{document_id}:page:{page_number}:element:{index}",
            document_id=document_id,
            source_format="pdf",
            text=text,
            element_type=element_type,
            page_number=page_number,
            bounding_box=bounding_box,
            location_reference=f"Page: {page_number}",
            # Attach the document-level image keys to every chunk so that
            # any chunk in a figure-heavy document can surface the images.
            # Chunk-level image filtering (by page/element) can be added
            # later once odl-parser exposes per-element image associations.
            image_s3_keys=doc_image_s3_keys,
        ))

    from kre.ingestion.adapters.chunk_util import merge_and_split_chunks
    return merge_and_split_chunks(chunks)
