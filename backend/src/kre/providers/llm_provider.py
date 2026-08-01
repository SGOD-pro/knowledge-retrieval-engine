"""LLM provider — query-time text generation.

Model Provider Matrix (ARCHITECTURE.md rev 5):
  - Prod: amazon.nova-lite-v1 (Bedrock) or claude-haiku
  - Dev:  openai/gpt-oss-20b or nvidia/nemotron-nano-9b (OpenRouter)

Rule 2: Maximum ONE LLM call per query.
Rule 4: Max tokens to LLM: 1200.
Rule 28: All LLM calls route through this module.
BOUNDARIES.md: Temperature = 0 on all LLM calls.
"""

import json
import logging
import os

from kre.providers.provider_client import get_active_provider

logger = logging.getLogger(__name__)

# Model IDs per ARCHITECTURE.md Model Provider Matrix
_PROD_MODEL_ID = "amazon.nova-lite-v1:0"
_DEV_MODEL_ID = os.environ.get("DEV_LLM_MODEL", "nvidia/nemotron-nano-9b-v2:free")

# Hard constraint: max tokens to LLM (Rule 4)
_MAX_TOKENS = 1200


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

            messages = [{"role": "user", "content": [{"text": user_prompt}]}]

            response = client.converse(
                modelId=_PROD_MODEL_ID,
                messages=messages,
                system=[{"text": system_prompt}],
                inferenceConfig={
                    "temperature": temperature,
                    "maxTokens": _MAX_TOKENS,
                },
            )

            return response["output"]["message"]["content"][0]["text"]

        except Exception as e:
            logger.error("Prod LLM failed: %s", str(e))
            raise e

    elif active == "dev":
        openrouter_key = os.environ.get("OPENROUTER_API_KEY")
        if openrouter_key:
            try:
                import requests

                response = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {openrouter_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": _DEV_MODEL_ID,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": temperature,
                        "max_tokens": _MAX_TOKENS,
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
