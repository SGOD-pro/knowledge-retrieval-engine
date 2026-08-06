"""LLM provider — query-time text generation.

Model Provider Matrix (ARCHITECTURE.md rev 5):
  - Prod: amazon.nova-lite-v1 (Bedrock) or claude-haiku
  - Dev:  Same as Prod

Rule 2: Maximum ONE LLM call per query.
Rule 4: Max tokens to LLM: 1200.
Rule 28: All LLM calls route through this module.
BOUNDARIES.md: Temperature = 0 on all LLM calls.
"""

import json
import logging
import os

from kre.providers.provider_client import get_active_provider
from kre.shared.bedrock_models import get_llm_model

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
    try:
        from kre.shared.aws import get_client
        client = get_client("bedrock-runtime")

        messages = [{"role": "user", "content": [{"text": user_prompt}]}]

        response = client.converse(
            modelId=get_llm_model(),
            messages=messages,
            system=[{"text": system_prompt}],
            inferenceConfig={
                "temperature": temperature,
                "maxTokens": _MAX_TOKENS,
            },
        )

        return response["output"]["message"]["content"][0]["text"]

    except Exception as e:
        logger.error("LLM request failed: %s", str(e))
        raise e

    return "{}"  # Fallback empty JSON response if no provider config exists
