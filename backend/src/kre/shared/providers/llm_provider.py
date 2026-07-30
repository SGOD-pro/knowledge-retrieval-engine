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

from kre.shared.providers.provider_client import get_active_provider
from kre.shared.config import get_llm_model

logger = logging.getLogger(__name__)

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
    model_id = get_llm_model(active)

    if active == "prod":
        try:
            from kre.shared.config import get_boto3_client
            client = get_boto3_client("bedrock-runtime")

            messages = [{"role": "user", "content": [{"text": user_prompt}]}]

            response = client.converse(
                modelId=model_id,
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
                        "model": model_id,
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
