import os
import json
import uuid
import shutil
import logging
from pathlib import Path

import boto3
import opendataloader_pdf

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client('s3')

def lambda_handler(event, context):
    s3_bucket = event.get('s3_bucket')
    s3_key = event.get('s3_key')
    local_file_path = event.get('local_file_path')
    
    if not local_file_path and (not s3_bucket or not s3_key):
        raise ValueError("Either 'local_file_path' or both 's3_bucket' and 's3_key' must be provided")
        
    # Use a unique directory to prevent cross-invocation pollution
    run_id = str(uuid.uuid4())
    # In Lambda /tmp is writable. Locally we use a local ./tmp dir
    tmp_dir = Path(f"/tmp/{run_id}") if not local_file_path else Path(f"./tmp/{run_id}")
    out_dir = tmp_dir / "out"
    
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    input_pdf = tmp_dir / "input.pdf"
    
    try:
        if local_file_path:
            logger.info(f"Processing local file: {local_file_path}")
            shutil.copy(local_file_path, input_pdf)
        else:
            # Download PDF from S3
            logger.info(f"Downloading s3://{s3_bucket}/{s3_key} to {input_pdf}")
            s3_client.download_file(s3_bucket, s3_key, str(input_pdf))
        
        # Run opendataloader_pdf
        logger.info("Running opendataloader_pdf.convert")
        opendataloader_pdf.convert(
            input_path=[str(input_pdf)], 
            output_dir=str(out_dir), 
            format="json,markdown"
        )
        
        # ODL outputs files named after the input file.
        # Check both direct output directory and nested directory just in case.
        md_file = out_dir / "input" / "input.md"
        json_file = out_dir / "input" / "input.json"
        
        if not md_file.exists() and not json_file.exists():
            md_file = out_dir / "input.md"
            json_file = out_dir / "input.json"
            
        markdown_content = ""
        elements = []
        
        if md_file.exists():
            markdown_content = md_file.read_text(encoding="utf-8")
        else:
            logger.warning(f"Markdown output not found in {out_dir}")
            
        if json_file.exists():
            elements = json.loads(json_file.read_text(encoding="utf-8"))
        else:
            logger.warning(f"JSON output not found in {out_dir}")
            
        return {
            "markdown": markdown_content,
            "elements": elements
        }
        
    except Exception as e:
        logger.error(f"Error processing document: {e}")
        raise
        
    finally:
        # Clean up /tmp/ to avoid filling ephemeral storage on warm invocations
        logger.info(f"Cleaning up {tmp_dir}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
