"""AWS infrastructure helpers — client/resource factory with unified AWS SDK credential chain.

Routing rules:
  - dev / prod -> standard AWS SDK credential chain (honoring AWS_PROFILE if set)
  - test       -> offline mocks, or explicit AWS_ENDPOINT_URL if configured for emulator tests
"""

import logging
import os

import boto3
from botocore.config import Config

from config import settings

logger = logging.getLogger(__name__)

_clients: dict = {}
_resources: dict = {}

_RETRY_CONFIG = Config(
    connect_timeout=15,
    read_timeout=30,
    retries={"max_attempts": 5, "mode": "adaptive"},
)


def _resolve_cloud_session(region_name: str | None = None) -> boto3.Session:
    """Build a boto3 Session using the standard AWS SDK credential chain (honoring AWS_PROFILE if set)."""
    region = region_name or getattr(settings, "AWS_REGION", None) or os.getenv("AWS_REGION") or "ap-south-1"
    profile = os.getenv("AWS_PROFILE") or getattr(settings, "AWS_PROFILE", None) or None
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def _build_client(
    service: str,
    region_name: str | None = None,
    config: Config | None = None,
):
    env = settings.ENVIRONMENT
    client_config = config or _RETRY_CONFIG
    session = _resolve_cloud_session(region_name)
    explicit_endpoint = os.getenv("AWS_ENDPOINT_URL") if env == "test" else None
    logger.debug(
        "aws.infra.client service=%s env=%s profile=%s region=%s",
        service,
        env,
        session.profile_name,
        session.region_name,
    )
    if explicit_endpoint:
        return session.client(service, endpoint_url=explicit_endpoint, config=client_config)
    return session.client(service, config=client_config)


def _build_resource(service: str):
    env = settings.ENVIRONMENT
    session = _resolve_cloud_session()
    explicit_endpoint = os.getenv("AWS_ENDPOINT_URL") if env == "test" else None
    logger.debug(
        "aws.infra.resource service=%s env=%s profile=%s region=%s",
        service,
        env,
        session.profile_name,
        session.region_name,
    )
    if explicit_endpoint:
        return session.resource(service, endpoint_url=explicit_endpoint, config=_RETRY_CONFIG)
    return session.resource(service, config=_RETRY_CONFIG)


def get_client(
    service: str,
    region_name: str | None = None,
    config: Config | None = None,
):
    """Return a cached boto3 client for the given service.

    Routing:
      dev / prod -> standard AWS SDK credential chain (honoring AWS_PROFILE if set)
      test       -> offline mocks, or explicit AWS_ENDPOINT_URL if configured
    """
    cache_key = (
        f"{service}:{region_name}:{id(config)}"
        if config is not None
        else (f"{service}:{region_name}" if region_name else service)
    )
    if cache_key not in _clients:
        _clients[cache_key] = _build_client(
            service, region_name=region_name, config=config
        )
    return _clients[cache_key]


def get_resource(service: str):
    """Return a cached boto3 resource for the given service. Same routing as get_client."""
    if service not in _resources:
        _resources[service] = _build_resource(service)
    return _resources[service]


def setup_infrastructure():
    """Verify or create necessary AWS resources at startup."""
    logger.info("aws.infra.setup_start env=%s", settings.ENVIRONMENT)

    # DynamoDB table
    dynamodb = get_client("dynamodb")
    table_name = settings.DYNAMODB_TABLE_NAME
    try:
        dynamodb.describe_table(TableName=table_name)
        logger.info("aws.infra.dynamodb_exists table=%s", table_name)
    except dynamodb.exceptions.ResourceNotFoundException:
        logger.info("aws.infra.dynamodb_creating table=%s", table_name)
        dynamodb.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName=table_name)
        logger.info("aws.infra.dynamodb_created table=%s", table_name)
    except Exception as e:
        logger.error("aws.infra.dynamodb_check_failed table=%s error=%s", table_name, e)

    # OKF Entities table
    try:
        dynamodb.describe_table(TableName="okf_entities")
        logger.info("aws.infra.dynamodb_exists table=okf_entities")
    except dynamodb.exceptions.ResourceNotFoundException:
        logger.info("aws.infra.dynamodb_creating table=okf_entities")
        dynamodb.create_table(
            TableName="okf_entities",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName="okf_entities")
        logger.info("aws.infra.dynamodb_created table=okf_entities")
    except Exception as e:
        logger.error("aws.infra.dynamodb_check_failed table=okf_entities error=%s", e)

    # OKF Properties table
    try:
        dynamodb.describe_table(TableName="okf_properties")
        logger.info("aws.infra.dynamodb_exists table=okf_properties")
    except dynamodb.exceptions.ResourceNotFoundException:
        logger.info("aws.infra.dynamodb_creating table=okf_properties")
        dynamodb.create_table(
            TableName="okf_properties",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName="okf_properties")
        logger.info("aws.infra.dynamodb_created table=okf_properties")
    except Exception as e:
        logger.error("aws.infra.dynamodb_check_failed table=okf_properties error=%s", e)

    # OKF Relations table (knowledge graph edges — System 2)
    try:
        dynamodb.describe_table(TableName="okf_relations")
        logger.info("aws.infra.dynamodb_exists table=okf_relations")
    except dynamodb.exceptions.ResourceNotFoundException:
        logger.info("aws.infra.dynamodb_creating table=okf_relations")
        dynamodb.create_table(
            TableName="okf_relations",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName="okf_relations")
        logger.info("aws.infra.dynamodb_created table=okf_relations")
    except Exception as e:
        logger.error("aws.infra.dynamodb_check_failed table=okf_relations error=%s", e)

    # S3 Bucket
    s3 = get_client("s3")
    bucket = settings.S3_BUCKET_NAME
    try:
        s3.head_bucket(Bucket=bucket)
        logger.info("aws.infra.s3_exists bucket=%s", bucket)
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "404":
            logger.info("aws.infra.s3_creating bucket=%s", bucket)
            region = settings.AWS_REGION
            if region == "us-east-1":
                s3.create_bucket(Bucket=bucket)
            else:
                s3.create_bucket(
                    Bucket=bucket,
                    CreateBucketConfiguration={"LocationConstraint": region},
                )
            logger.info("aws.infra.s3_created bucket=%s", bucket)
        else:
            logger.warning("aws.infra.s3_check_failed bucket=%s error=%s", bucket, e)
