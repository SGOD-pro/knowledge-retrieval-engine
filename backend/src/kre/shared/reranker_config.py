import boto3
from botocore.config import Config
from kre.shared.config import settings

def get_reranker_client():
    """
    Returns a Bedrock Runtime client specifically for the reranker model.
    Since Cohere rerank models are typically in us-east-1, this client 
    hardcodes us-east-1 for the dev environment to avoid ValidationExceptions.
    """
    config = Config(retries={'max_attempts': 3, 'mode': 'adaptive'})
    
    if settings.ENVIRONMENT == "dev":
        aws_session = boto3.Session(
            profile_name="aws",
            region_name="us-east-1",  # Cohere reranker is usually in us-east-1
        )
        return aws_session.client("bedrock-runtime", config=config)
    else:
        # Prod uses the default execution role and region
        session = boto3.Session()
        return session.client("bedrock-runtime", config=config)
