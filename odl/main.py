import os
import json
import uuid
import shutil
import logging
import re
from pathlib import Path

import boto3

# opendataloader_pdf is only present inside the deployed Lambda image.
# When absent (local dev / test environments) we set a None sentinel so that
# the module is still importable and tests can patch the attribute normally.
try:
    import opendataloader_pdf  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    opendataloader_pdf = None  # type: ignore[assignment]

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Supported image extensions produced by opendataloader_pdf
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# Content-type map for image uploads
_CONTENT_TYPES = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _image_s3_key(source_key: str, doc_id: str, filename: str) -> str:
    """
    Derive a canonical S3 key for an image extracted from *source_key*.

    Layout:
        <source_prefix>/images/<doc_id>/<filename>

    Example:
        source_key = "jobs/j1/resumes/abc.pdf"
        doc_id     = "abc"
        filename   = "abc-img_001.png"
        → "jobs/j1/resumes/images/abc/abc-img_001.png"

    The image lives in the *same bucket* as the source PDF, co-located
    under the same prefix so IAM / bucket-policy rules apply uniformly.
    """
    # Drop the bare filename from source_key to get the prefix directory.
    prefix = "/".join(source_key.split("/")[:-1])
    if prefix:
        return f"{prefix}/images/{doc_id}/{filename}"
    return f"images/{doc_id}/{filename}"


def lambda_handler(event, context):
    # ── Payload normaliser: accept single-doc flat fields OR documents[] ──────
    if "documents" not in event and any(k in event for k in ("s3_bucket", "s3_key", "document_id")):
        documents = [{
            "s3_bucket": event.get("s3_bucket"),
            "s3_key":    event.get("s3_key"),
            "document_id": event.get("document_id"),
        }]
    else:
        documents = event.get("documents", [])

    save_images = event.get("save_images", False)

    if not documents:
        raise ValueError("Missing 'documents' array (or equivalent single-doc fields) in event payload")

    logger.info("Processing batch of %d documents (save_images=%s)", len(documents), save_images)

    if opendataloader_pdf is None:  # pragma: no cover
        raise RuntimeError(
            "opendataloader_pdf is not installed in this environment. "
            "Deploy to Lambda or install the package manually."
        )

    # Create S3 client here (not at module level) so that moto can intercept
    # boto3.client() calls in tests when mock_aws() is active.
    s3_client = boto3.client("s3")

    # ── Unique /tmp workspace per invocation — prevents cross-invocation pollution
    run_id    = str(uuid.uuid4())
    tmp_dir   = Path(f"/tmp/{run_id}")
    inputs_dir = tmp_dir / "inputs"
    out_dir   = tmp_dir / "out"

    inputs_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    input_paths: list = []
    doc_map: dict = {}  # document_id → doc info dict

    try:
        # ── 1. Download all source PDFs from S3 ──────────────────────────────
        for doc in documents:
            doc_id = doc.get("document_id")
            bucket = doc.get("s3_bucket")
            key    = doc.get("s3_key")

            if not doc_id or not bucket or not key:
                logger.warning("Skipping invalid document entry: %s", doc)
                continue

            input_pdf = inputs_dir / f"{doc_id}.pdf"
            if Path(key).exists():
                logger.info("Local file found for %s, copying → %s", key, input_pdf)
                import shutil
                shutil.copy(key, str(input_pdf))
            else:
                logger.info("Downloading s3://%s/%s → %s", bucket, key, input_pdf)
                s3_client.download_file(bucket, key, str(input_pdf))

            input_paths.append(str(input_pdf))
            doc_map[doc_id] = doc

        if not input_paths:
            raise ValueError("No valid documents to process after download")

        # ── 2. Run opendataloader_pdf on all files in ONE JVM invocation ──────
        logger.info("Running opendataloader_pdf.convert on %d file(s)", len(input_paths))
        convert_kwargs = {
            "input_path": input_paths,
            "output_dir": str(out_dir),
            "format":     "json,markdown",
        }
        if save_images:
            convert_kwargs["image_output"] = "external"
            convert_kwargs["image_dir"]    = str(out_dir)
        else:
            convert_kwargs["image_output"] = "off"

        try:
            opendataloader_pdf.convert(**convert_kwargs)
        except Exception as exc:
            # convert() raises if ANY file is corrupt; valid files still produce
            # output.  Missing output files identify the actual failures below.
            logger.warning(
                "opendataloader_pdf.convert raised (likely partial failure): %s", exc
            )

        # ── 3 & 4. Per-document result assembly ──────────────────────────────
        results = []
        failed  = []

        for doc_id, doc_info in doc_map.items():
            md_file   = out_dir / f"{doc_id}.md"
            json_file = out_dir / f"{doc_id}.json"

            if not md_file.exists() and not json_file.exists():
                logger.error("Output for %s not found — marking as failed.", doc_id)
                failed.append(doc_id)
                continue

            markdown_content = ""
            elements         = []
            image_s3_keys    = []

            # ── Markdown ──────────────────────────────────────────────────────
            if md_file.exists():
                markdown_content = md_file.read_text(encoding="utf-8")

                if save_images:
                    src_bucket  = doc_info["s3_bucket"]
                    src_key     = doc_info["s3_key"]

                    # Walk out_dir for images belonging to this doc (sorted for
                    # deterministic ordering in image_s3_keys list).
                    for img_file in sorted(out_dir.iterdir()):
                        if (
                            img_file.is_file()
                            and img_file.name.startswith(doc_id)
                            and img_file.suffix.lower() in _IMAGE_EXTS
                        ):
                            s3_key     = _image_s3_key(src_key, doc_id, img_file.name)
                            s3_uri     = f"s3://{src_bucket}/{s3_key}"
                            ctype      = _CONTENT_TYPES.get(img_file.suffix.lower(),
                                                             "application/octet-stream")

                            logger.info("Uploading image %s → %s", img_file.name, s3_uri)
                            s3_client.upload_file(
                                str(img_file),
                                src_bucket,
                                s3_key,
                                ExtraArgs={"ContentType": ctype},
                            )
                            image_s3_keys.append(s3_uri)

                            # Rewrite markdown: replace ANY reference to this
                            # filename (relative, absolute /tmp/… path, or bare
                            # name) with the canonical s3:// URI so downstream
                            # consumers can fetch images without a separate
                            # bucket lookup.
                            pattern = (
                                r'(!\[.*?\]\()([^)]*?'
                                + re.escape(img_file.name)
                                + r')(\))'
                            )
                            markdown_content = re.sub(
                                pattern, rf'\g<1>{s3_uri}\g<3>', markdown_content
                            )

            # ── Structured elements (JSON) ────────────────────────────────────
            if json_file.exists():
                elements = json.loads(json_file.read_text(encoding="utf-8"))

            results.append({
                "document_id":  doc_id,
                "markdown":     markdown_content,
                "elements":     elements,
                "image_s3_keys": image_s3_keys,
            })

        # ── 5. Return batch response ──────────────────────────────────────────
        return {
            "results": results,
            "failed":  failed,
        }

    except Exception as exc:
        logger.error("Fatal error processing batch: %s", exc, exc_info=True)
        raise

    finally:
        # Always clean up /tmp/ — critical on warm Lambda invocations to avoid
        # filling the 512 MB ephemeral storage limit.
        logger.info("Cleaning up %s", tmp_dir)
        shutil.rmtree(tmp_dir, ignore_errors=True)
