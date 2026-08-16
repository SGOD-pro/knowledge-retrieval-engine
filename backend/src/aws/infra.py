"""AWS infrastructure helpers — client/resource factory with profile routing.

Profile routing rules:
  - Local services  (DynamoDB, S3, SQS, SNS, ElastiCache, RDS):
        dev  → profile=local, endpoint=http://localhost:4566 (FLOCI)
        prod → no profile, standard boto3 session from IAM role
  - Cloud services  (bedrock-runtime, lambda):
        dev  → profile=aws, standard AWS endpoint
        prod → no profile, standard boto3 session from IAM role
  - BGE microservice:
        # TODO: After deploy, call bge-embedding-lambda via profile=aws boto3.
        # Currently routed through ingestion.embed_service._run_onnx (local ONNX).
        # No changes needed here until the Lambda ARN is available.

ENVIRONMENT values:
  test  → infra calls should be mocked in tests; this module is patched out.
  dev   → profile=local for local services, profile=aws for cloud services.
  prod  → standard IAM role-based auth for everything.
"""

import logging

import boto3
from botocore.config import Config

from config import settings

logger = logging.getLogger(__name__)

# Services that run locally via FLOCI in dev mode
_LOCAL_SERVICES = frozenset(
    {
        "dynamodb",
        "s3",
        "sqs",
        "sns",
        "rds",
        "elasticache",
    }
)

# Services that always use the real AWS endpoint (profile=aws in dev)
_CLOUD_SERVICES = frozenset(
    {
        "bedrock-runtime",
        "lambda",
    }
)

_FLOCI_ENDPOINT = "http://localhost:4566"
_AWS_PROFILE = "aws"
_LOCAL_PROFILE = "local"
_LOCAL_REGION = "us-east-1"

_clients: dict = {}
_resources: dict = {}

_RETRY_CONFIG = Config(retries={"max_attempts": 3, "mode": "adaptive"})


def _build_client(service: str, region_name: str | None = None):
    env = settings.ENVIRONMENT

    if service in _CLOUD_SERVICES:
        # User requirement: lambda and bedrock always use profile=aws, region=ap-south-1
        # regardless of env (prod or dev).
        region = region_name or "ap-south-1"
        logger.debug(
            "aws.infra.client service=%s profile=%s region=%s",
            service,
            _AWS_PROFILE,
            region,
        )
        session = boto3.Session(profile_name=_AWS_PROFILE, region_name=region)
        return session.client(service, config=_RETRY_CONFIG)

    if env == "dev":
        # Dev local services use local profile and endpoint
        logger.debug(
            "aws.infra.client service=%s profile=%s endpoint=%s",
            service,
            _LOCAL_PROFILE,
            _FLOCI_ENDPOINT,
        )
        session = boto3.Session(profile_name=_LOCAL_PROFILE, region_name=_LOCAL_REGION)
        return session.client(
            service, endpoint_url=_FLOCI_ENDPOINT, config=_RETRY_CONFIG
        )
    else:
        # Prod local services use aws profile and ap-south-1
        region = region_name or "ap-south-1"
        logger.debug(
            "aws.infra.client service=%s env=%s profile=%s region=%s",
            service,
            env,
            _AWS_PROFILE,
            region,
        )
        session = boto3.Session(profile_name=_AWS_PROFILE, region_name=region)
        return session.client(service, config=_RETRY_CONFIG)


def _build_resource(service: str):
    env = settings.ENVIRONMENT

    if service in _CLOUD_SERVICES:
        logger.debug(
            "aws.infra.resource service=%s profile=%s region=%s",
            service,
            _AWS_PROFILE,
            "ap-south-1",
        )
        session = boto3.Session(profile_name=_AWS_PROFILE, region_name="ap-south-1")
        return session.resource(service, config=_RETRY_CONFIG)

    if env == "dev":
        logger.debug(
            "aws.infra.resource service=%s profile=%s endpoint=%s",
            service,
            _LOCAL_PROFILE,
            _FLOCI_ENDPOINT,
        )
        session = boto3.Session(profile_name=_LOCAL_PROFILE, region_name=_LOCAL_REGION)
        return session.resource(
            service, endpoint_url=_FLOCI_ENDPOINT, config=_RETRY_CONFIG
        )
    else:
        logger.debug(
            "aws.infra.resource service=%s env=%s profile=%s region=%s",
            service,
            env,
            _AWS_PROFILE,
            "ap-south-1",
        )
        session = boto3.Session(profile_name=_AWS_PROFILE, region_name="ap-south-1")
        return session.resource(service, config=_RETRY_CONFIG)


def get_client(service: str, region_name: str | None = None):
    """Return a cached boto3 client for the given service.

    Routing:
      dev  + local service  → FLOCI  (profile=local, localhost:4566)
      dev  + cloud service  → Real AWS    (profile=aws)
      prod                  → Real AWS    (IAM role)
    """
    cache_key = f"{service}:{region_name}" if region_name else service
    if cache_key not in _clients:
        _clients[cache_key] = _build_client(service, region_name=region_name)
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
