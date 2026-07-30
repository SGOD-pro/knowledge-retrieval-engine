import os
import json
import boto3
import requests
from dotenv import load_dotenv

# Load the environment variables from .env
load_dotenv()

def verify_openrouter():
    print("Verifying OpenRouter API access...")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is missing in the environment.")
    
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "nvidia/nemotron-nano-9b-v2:free",
            "messages": [{"role": "user", "content": "Ping!"}],
            "max_tokens": 10
        },
        timeout=10.0
    )
    if response.status_code == 200:
        print("✅ OpenRouter connection successful.")
    else:
        raise ValueError(f"OpenRouter connection failed: {response.status_code} - {response.text}")

def verify_bedrock():
    print("Verifying AWS Bedrock access...")
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    region = os.environ.get("AWS_REGION", "us-east-1")
    
    if not access_key or not secret_key:
        raise ValueError("AWS credentials missing in the environment.")
    
    client = boto3.client(
        "bedrock-runtime",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region
    )
    
    messages = [{"role": "user", "content": [{"text": "Ping!"}]}]
    try:
        response = client.converse(
            modelId="amazon.nova-lite-v1:0",
            messages=messages,
            inferenceConfig={"maxTokens": 10},
        )
        print("✅ Bedrock connection successful.")
    except Exception as e:
        raise ValueError(f"Bedrock connection failed: {e}")

if __name__ == "__main__":
    try:
        verify_openrouter()
        verify_bedrock()
        print("\nAll prerequisite API checks passed!")
    except Exception as e:
        print(f"\n❌ Pre-requisite check failed: {e}")
        exit(1)
