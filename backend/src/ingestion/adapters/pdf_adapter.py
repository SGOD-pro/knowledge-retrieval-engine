"""PDF adapter — invokes the ODL Parser Lambda for extraction in prod,
or falls back immediately to pypdf in dev/local environments.
"""

import base64
import json
import logging
import uuid
from pathlib import Path

from config import settings
from schemas.models import Chunk

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lambda invocation path (prod / staging)
# ---------------------------------------------------------------------------

def _invoke_lambda(path: Path, document_id: str) -> list[dict] | None:
    """Upload PDF to S3 then invoke the ODL Parser Lambda.

    Returns the raw list of element dicts from the Lambda response, or None
    if invocation fails (caller falls back to pypdf).
    """
    # In dev/test mode, skip lambda to avoid remote calls during local workflows
    if settings.ENVIRONMENT in ("test", "dev"):
        logger.debug("pdf_adapter.dev_mode skipping lambda for local parsing")
        return None

    try:
        import boto3
        from aws.infra import get_client
        from botocore.exceptions import BotoCoreError, ClientError

        # S3 upload must reach real AWS S3 where the cloud Lambda is executed
        from botocore.config import Config
        s3_cfg = Config(connect_timeout=10, read_timeout=25, retries={"max_attempts": 2})
        try:
            session = boto3.Session(profile_name="aws")
            s3 = session.client("s3", region_name="us-east-1", config=s3_cfg)
            lambda_client = session.client("lambda", region_name="ap-south-1")
        except Exception:
            s3 = get_client("s3")
            lambda_client = get_client("lambda", region_name="ap-south-1")

        # Upload to S3 under a temp key scoped to the document_id
        s3_key = f"tmp/pdf-extraction/{document_id}/{path.name}"
        pdf_bytes = path.read_bytes()
        s3.put_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=s3_key,
            Body=pdf_bytes,
            ContentType="application/pdf",
        )
        logger.info(
            "pdf_adapter.s3_upload doc_id=%s bucket=%s key=%s",
            document_id,
            settings.S3_BUCKET_NAME,
            s3_key,
        )

        # Invoke the extraction Lambda
        payload = {
            "s3_bucket": settings.S3_BUCKET_NAME,
            "s3_key": s3_key,
            "document_id": document_id,
        }
        response = lambda_client.invoke(
            FunctionName=settings.ODL_PARSER_LAMBDA_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )

        result_raw = response["Payload"].read()
        result = json.loads(result_raw)

        # Lambda may return an error envelope
        if "errorMessage" in result or result.get("error"):
            logger.error(
                "pdf_adapter.lambda_error doc_id=%s error=%s",
                document_id,
                result.get("errorMessage") or result.get("error"),
            )
            return None

        # Unpack ODL Parser envelope: {"results": [{"document_id": "...", "elements": {"kids": [...]}}]}
        chunks_raw = []
        if isinstance(result, dict):
            if "results" in result and isinstance(result["results"], list) and result["results"]:
                r0 = result["results"][0]
                if isinstance(r0, dict):
                    elements_obj = r0.get("elements", {})
                    if isinstance(elements_obj, dict):
                        chunks_raw = elements_obj.get("kids", [])
                    elif isinstance(elements_obj, list):
                        chunks_raw = elements_obj
            if not chunks_raw:
                chunks_raw = result.get("chunks") or result.get("kids") or result.get("pages") or []
        elif isinstance(result, list):
            chunks_raw = result

        logger.info(
            "pdf_adapter.lambda_done doc_id=%s chunk_count=%d",
            document_id,
            len(chunks_raw),
        )
        return chunks_raw

    except (ImportError, BotoCoreError, ClientError) as exc:
        logger.warning(
            "pdf_adapter.lambda_unavailable doc_id=%s reason=%s — falling back to pypdf",
            document_id,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pdf_adapter.lambda_failed doc_id=%s error=%s — falling back to pypdf",
            document_id,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Fallback: pypdf plain-text extraction (dev mode)
# ---------------------------------------------------------------------------

def _extract_with_pypdf(path: Path, document_id: str) -> list[dict]:
    """Extract text via pypdf. No bounding boxes, paragraph-level chunks."""
    try:
        from pypdf import PdfReader  # type: ignore[import]
    except ImportError:
        logger.error("pdf_adapter: pypdf not installed and Lambda unavailable — returning empty")
        return []

    try:
        reader = PdfReader(str(path))
        elements: list[dict] = []
        for page_num, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            # Split into rough paragraphs on blank lines
            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
            if not paragraphs and text.strip():
                paragraphs = [text.strip()]
            for para in paragraphs:
                elements.append(
                    {
                        "content": para,
                        "type": "paragraph",
                        "page_number": page_num,
                    }
                )
        logger.info(
            "pdf_adapter.pypdf_done doc_id=%s pages=%d elements=%d",
            document_id,
            len(reader.pages),
            len(elements),
        )
        return elements
    except Exception as e:
        logger.error("pdf_adapter.pypdf_error doc_id=%s error=%s", document_id, e)
        return []


# ---------------------------------------------------------------------------
# Chunk normaliser (shared by both paths)
# ---------------------------------------------------------------------------

def _normalize_elements(
    elements: list[dict], document_id: str, workspace_id: str = ""
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            continue

        text = str(
            element.get("content") or element.get("source") or element.get("text") or ""
        ).strip()
        if not text or (
            element.get("type") == "image" and text.endswith((".png", ".jpg", ".jpeg"))
        ):
            continue

        page_number = int(
            element.get("page number")
            or element.get("page_number")
            or element.get("page")
            or 1
        )
        raw_box = (
            element.get("bounding box")
            or element.get("bounding_box")
            or element.get("bbox")
        )
        bounding_box = None
        if isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
            bounding_box = {
                "x1": float(raw_box[0]),
                "y1": float(raw_box[1]),
                "x2": float(raw_box[2]),
                "y2": float(raw_box[3]),
            }
        elif isinstance(raw_box, dict):
            bounding_box = {k: float(v) for k, v in raw_box.items()}
        else:
            bounding_box = {
                "x1": 0.0,
                "y1": 0.0,
                "x2": 1.0,
                "y2": 1.0,
                "page_number": page_number,
            }

        element_type = str(
            element.get("type") or element.get("element_type") or "paragraph"
        )

        chunks.append(
            Chunk(
                id=f"{document_id}:page:{page_number}:element:{index}",
                document_id=document_id,
                source_format="pdf",
                text=text,
                element_type=element_type,
                page_number=page_number,
                bounding_box=bounding_box,
                location_reference=f"Page: {page_number}",
                workspace_id=workspace_id,
            )
        )

    from .chunk_util import merge_and_split_chunks

    return merge_and_split_chunks(chunks)


# ---------------------------------------------------------------------------
# Public entry point (called by format_router)
# ---------------------------------------------------------------------------

def parse(path: Path, document_id: str, workspace_id: str = "") -> list[Chunk]:
    """Parse a PDF via Lambda (prod) or pypdf fallback (dev).

    Lambda path: upload to S3 → invoke ODL_PARSER_LAMBDA_NAME → receive JSON.
    Fallback path: pypdf text extraction (no bounding boxes).
    """
    elements = _invoke_lambda(path, document_id)

    if elements is None:
        logger.info(
            "pdf_adapter.using_fallback doc_id=%s path=%s", document_id, path.name
        )
        elements = _extract_with_pypdf(path, document_id)

    return _normalize_elements(elements, document_id, workspace_id=workspace_id)
