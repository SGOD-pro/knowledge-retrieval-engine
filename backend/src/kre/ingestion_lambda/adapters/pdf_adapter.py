"""PDF adapter — invokes odl-parser-lambda via boto3.

ARCHITECTURE.md rev 5: PDF Extraction Lambda is a separate container image
deployed to ECR, invoked synchronously via boto3 from the Ingestion Lambda.

DECISION.md: Payload IN is an S3 object reference. Payload OUT is pending
verification against the live function.
"""

import json
import logging
import os

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
    """
    import os
    import sys
    import json
    
    environment = os.environ.get("ENVIRONMENT", "dev")
    
    if environment == "dev":
        # DEV BYPASS: Direct import to avoid S3 dependency
        odl_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "odl"))
        if odl_path not in sys.path:
            sys.path.append(odl_path)
        import main as odl_main
        
        event = {"local_file_path": os.path.abspath(path)}
        response = odl_main.lambda_handler(event, None)
        response_payload = response
    else:
        # PROD PATH: Invoke deployed Lambda via boto3
        from kre.shared.config import get_boto3_client
        client = get_boto3_client("lambda")
        
        # In a real S3 trigger, these would come from the event. 
        # For now, we mock them based on the path.
        s3_bucket = os.environ.get("S3_BUCKET_NAME", "kre-documents-prod")
        s3_key = path.name
        
        payload = {
            "s3_bucket": s3_bucket,
            "s3_key": s3_key,
            "document_id": document_id,
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

    # Parse the response items into Chunk objects
    items = response_payload if isinstance(response_payload, list) else (
        response_payload.get("chunks") or response_payload.get("kids") or
        response_payload.get("pages") or response_payload.get("elements") or []
    )

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
        ))

    return chunks
