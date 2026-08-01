import json
import logging
import re
import shutil
import uuid
from pathlib import Path

import boto3
import opendataloader_pdf

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client('s3')


def lambda_handler(event, context):
    documents = event.get('documents', [])
    save_images = event.get('save_images', False)

    if not documents:
        raise ValueError("Missing 'documents' array in event payload")

    logger.info(f"Processing batch of {len(documents)} documents")

    # Use a unique directory in /tmp to prevent cross-invocation pollution
    run_id = str(uuid.uuid4())
    tmp_dir = Path(f"/tmp/{run_id}")
    inputs_dir = tmp_dir / "inputs"
    out_dir = tmp_dir / "out"

    inputs_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    input_paths = []
    doc_map = {}  # document_id -> doc info

    try:
        # 1. Download all PDFs
        for doc in documents:
            doc_id = doc.get('document_id')
            bucket = doc.get('s3_bucket')
            key = doc.get('s3_key')

            if not doc_id or not bucket or not key:
                logger.warning(f"Skipping invalid document entry: {doc}")
                continue

            input_pdf = inputs_dir / f"{doc_id}.pdf"
            logger.info(f"Downloading s3://{bucket}/{key} to {input_pdf}")
            s3_client.download_file(bucket, key, str(input_pdf))

            input_paths.append(str(input_pdf))
            doc_map[doc_id] = doc

        if not input_paths:
            raise ValueError("No valid documents to process after download")

        # 2. Run opendataloader_pdf on all files in one call.
        logger.info(f"Running opendataloader_pdf.convert on {len(input_paths)} files")
        convert_kwargs = {
            "input_path": input_paths,
            "output_dir": str(out_dir),
            "format": "json,markdown",
        }
        if save_images:
            convert_kwargs["image_output"] = "external"
            convert_kwargs["image_dir"] = str(out_dir)
        else:
            convert_kwargs["image_output"] = "off"

        try:
            opendataloader_pdf.convert(**convert_kwargs)
        except Exception as e:
            # Tested (2026-08-01): convert() raises CalledProcessError when any file
            # has an invalid PDF header, but it still writes .md/.json output for all
            # valid files processed before the failure. We catch here and rely on the
            # presence of output files below to identify which documents succeeded.
            logger.warning(f"opendataloader_pdf.convert raised (likely a corrupt file in batch): {e}")

        # 3 & 4. Per-doc result assembly
        results = []
        failed = []

        for doc_id, doc_info in doc_map.items():
            md_file = out_dir / f"{doc_id}.md"
            json_file = out_dir / f"{doc_id}.json"

            if not md_file.exists() and not json_file.exists():
                logger.error(f"Output for {doc_id} not found, marking as failed.")
                failed.append(doc_id)
                continue

            markdown_content = ""
            elements = []
            image_s3_keys = []

            if md_file.exists():
                markdown_content = md_file.read_text(encoding="utf-8")

                if save_images:
                    # ODL writes images to a {doc_id}_images/ subdirectory inside out_dir,
                    # e.g. out/valid_0_images/imageFile1.png (confirmed by test 2026-08-01).
                    doc_img_dir = out_dir / f"{doc_id}_images"
                    s3_key = doc_info.get('s3_key')
                    bucket = doc_info.get('s3_bucket')
                    if doc_img_dir.is_dir():
                        for img_file in doc_img_dir.iterdir():
                            if img_file.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
                                continue
                            s3_img_key = f"{s3_key}_images/{img_file.name}"
                            try:
                                logger.info(f"Uploading image {img_file.name} to s3://{bucket}/{s3_img_key}")
                                s3_client.upload_file(str(img_file), bucket, s3_img_key)
                                image_s3_keys.append(s3_img_key)
                                # Rewrite local path reference in markdown to the S3 key.
                                pattern = r'(!\[.*?\]\()([^\)]*?' + re.escape(img_file.name) + r')(\))'
                                markdown_content = re.sub(pattern, r'\g<1>' + s3_img_key + r'\g<3>', markdown_content)
                            except Exception as upload_err:
                                # One broken image must not discard this document's results.
                                logger.error(f"Failed to upload {img_file.name} for {doc_id}: {upload_err}")

            if json_file.exists():
                elements = json.loads(json_file.read_text(encoding="utf-8"))

            results.append({
                "document_id": doc_id,
                "markdown": markdown_content,
                "elements": elements,
                "image_s3_keys": image_s3_keys,
            })

        # 5. Response
        return {
            "results": results,
            "failed": failed,
        }

    except Exception as e:
        logger.error(f"Error processing batch: {e}")
        raise

    finally:
        # Clean up /tmp/ to avoid filling ephemeral storage on warm invocations
        logger.info(f"Cleaning up {tmp_dir}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
