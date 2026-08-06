import boto3

from botocore.config import Config
config = Config(connect_timeout=2, read_timeout=2, retries={'max_attempts': 1})

session = boto3.Session(profile_name="local", region_name="us-east-1")
try:
    rds = session.client('rds', endpoint_url="http://localhost:4566", config=config)
    print("RDS:", rds.describe_db_instances())
except Exception as e:
    print("RDS Error:", e)

try:
    ec = session.client('elasticache', endpoint_url="http://localhost:4566", config=config)
    print("ElastiCache:", ec.describe_cache_clusters())
except Exception as e:
    print("ElastiCache Error:", e)
