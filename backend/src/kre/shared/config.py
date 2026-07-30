"""Centralized AWS and environment configuration.

Handles routing between local (LocalStack/floci) and real AWS endpoints.
As requested, Bedrock and Lambda clients MUST ALWAYS use real AWS endpoints,
even if AWS_ENDPOINT_URL is set to a local LocalStack instance (e.g., localhost:4566).
"""

import os
import boto3
from botocore.config import Config


def is_local_env() -> bool:
    """Check if we are in a local environment (e.g. LocalStack)."""
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    return "localhost" in endpoint or "127.0.0.1" in endpoint


def get_boto3_client(service_name: str):
    """Get a boto3 client, enforcing real AWS for Bedrock and Lambda."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    
    # Always use real AWS for Lambda and Bedrock
    if service_name in ("lambda", "bedrock-runtime", "bedrock"):
        # We explicitly omit endpoint_url here to force real AWS
        return boto3.client(service_name, region_name=region)
    
    # For other services (like S3), respect AWS_ENDPOINT_URL if present
    endpoint_url = os.environ.get("AWS_ENDPOINT_URL")
    if endpoint_url:
        return boto3.client(service_name, region_name=region, endpoint_url=endpoint_url)
    
    return boto3.client(service_name, region_name=region)
