"""Ingestion Lambda handler entry point.

Receives S3 event notifications for new documents, orchestrates parsing,
OKF extraction, and dual-column embedding before persisting to Postgres.
"""

import json
import logging

logger = logging.getLogger(__name__)


def handler(event, context):
    """AWS Lambda entry point for the Ingestion Lambda."""
    logger.info("Ingestion Lambda invoked with event: %s", json.dumps(event))

    # TODO: Implement full ingestion orchestration
    # 1. Extract S3 bucket/key from event
    # 2. Route to appropriate adapter (PDF → invoke odl-parser-lambda, others → in-process)
    # 3. Run page_index_service, concept_service, normalize_service, okf_builder
    # 4. Run embed_service.embed_chunks_dual() to populate both embedding columns
    # 5. Persist to Postgres via shared/db/postgres.py

    return {"statusCode": 200, "body": json.dumps({"message": "Ingestion complete"})}
