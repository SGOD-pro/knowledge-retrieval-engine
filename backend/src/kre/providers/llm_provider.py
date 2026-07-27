import os
import json
import logging
from typing import Any
from kre.providers.provider_client import get_active_provider

logger = logging.getLogger(__name__)

def generate_completion(
    system_prompt: str,
    user_prompt: str,
    provider: str | None = None,
    temperature: float = 0.0,
) -> str:
    """Generate LLM completion strictly enforcing max 1 LLM call per query.
    Returns the raw string output.
    """
    active = provider or get_active_provider()
    
    if active == "prod":
        try:
            import boto3
            client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            
            # Use Nova Lite v1 via Bedrock
            # Nova models use the converse API or generic invoke_model
            # Converse API is cleaner for messages
            messages = [{"role": "user", "content": [{"text": user_prompt}]}]
            
            response = client.converse(
                modelId="amazon.nova-lite-v1:0",
                messages=messages,
                system=[{"text": system_prompt}],
                inferenceConfig={"temperature": temperature}
            )
            
            return response["output"]["message"]["content"][0]["text"]
            
        except Exception as e:
            logger.error("Prod LLM failed: %s", str(e))
            raise e
            
    elif active == "dev" and os.environ.get("OPENROUTER_API_KEY"):
        try:
            import requests
            openrouter_key = os.environ.get("OPENROUTER_API_KEY")
            
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openrouter_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "nvidia/nemotron-nano-9b-v2:free",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    "temperature": temperature,
                },
                timeout=30.0,
            )
            if response.status_code == 200:
                data = response.json()
                return data["choices"][0]["message"]["content"]
            else:
                logger.error("Dev LLM returned %d: %s", response.status_code, response.text)
        except Exception as e:
            logger.error("Dev LLM request failed: %s", str(e))
            raise e
            
    return "{}"  # Fallback empty JSON response if no provider config exists
