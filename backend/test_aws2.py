import boto3

session = boto3.Session(profile_name="local", region_name="ap-south-1")
rds = session.client('rds', endpoint_url="http://localhost:4566")
try:
    print("RDS ap-south-1:", rds.describe_db_instances())
except Exception as e:
    print("RDS Error:", e)
