"""Ingestion Lambda handler entry point.

Receives S3 event notifications for new documents, orchestrates parsing,
OKF extraction, and dual-column embedding before persisting to DynamoDB and Qdrant.
"""

import json
import logging

logger = logging.getLogger(__name__)


def handler(event, context):
    """AWS Lambda entry point for the Ingestion Lambda."""
    logger.info("Ingestion Lambda invoked with event: %s", json.dumps(event))
    return {"statusCode": 200, "body": json.dumps({"message": "Ingestion complete"})}
