import boto3
from botocore.config import Config

from kre.shared.config import settings

LOCAL_SERVICES = [
    "dynamodb",
    "s3",
    "sqs",
    "sns",
    "rds",
    "elasticache",
]

def get_client(service: str):
    config = Config(retries={'max_attempts': 3, 'mode': 'adaptive'})

    if settings.ENVIRONMENT == "dev":
        if service in ("bedrock-runtime", "lambda"):
            aws_session = boto3.Session(
                profile_name="aws",
                region_name="ap-south-1",
            )
            return aws_session.client(service, config=config)
        else:
            local_session = boto3.Session(
                profile_name="local",
                region_name="us-east-1",
            )
            return local_session.client(
                service, 
                endpoint_url="http://localhost:4566", 
                config=config
            )
    else:
        session = boto3.Session()
        return session.client(service, config=config)
