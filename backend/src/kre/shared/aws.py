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

_clients = {}

def get_client(service: str):
    global _clients
    if service in _clients:
        return _clients[service]
        
    config = Config(retries={'max_attempts': 3, 'mode': 'adaptive'})

    if settings.ENVIRONMENT == "dev":
        if service in ("bedrock-runtime", "lambda", "dynamodb"):
            aws_session = boto3.Session(
                profile_name="aws",
                region_name="ap-south-1",
            )
            client = aws_session.client(service, config=config)
        else:
            local_session = boto3.Session(
                profile_name="local",
                region_name="us-east-1",
            )
            client = local_session.client(
                service, 
                endpoint_url="http://localhost:4566", 
                config=config
            )
    else:
        session = boto3.Session()
        client = session.client(service, config=config)
        
    _clients[service] = client
    return client
_resources = {}

def get_resource(service: str):
    global _resources
    if service in _resources:
        return _resources[service]
        
    config = Config(retries={'max_attempts': 3, 'mode': 'adaptive'})

    if settings.ENVIRONMENT == "dev":
        if service in ("bedrock-runtime", "lambda", "dynamodb"):
            aws_session = boto3.Session(
                profile_name="aws",
                region_name="ap-south-1",
            )
            res = aws_session.resource(service, config=config)
        else:
            local_session = boto3.Session(
                profile_name="local",
                region_name="us-east-1",
            )
            res = local_session.resource(
                service, 
                endpoint_url="http://localhost:4566", 
                config=config
            )
    else:
        session = boto3.Session()
        res = session.resource(service, config=config)
        
    _resources[service] = res
    return res
