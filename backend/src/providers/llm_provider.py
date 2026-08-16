"""LLM provider — query-time text generation.

Model Provider Matrix (ARCHITECTURE.md rev 5):
  - Prod: amazon.nova-lite-v1 (Bedrock) or claude-haiku
  - Dev:  Same as Prod

Rule 2: Maximum ONE LLM call per query.
Rule 4: Max tokens to LLM: 1200.
Rule 28: All LLM calls route through this module.
BOUNDARIES.md: Temperature = 0 on all LLM calls.

Returns: (text: str, usage: dict) — usage contains input_tokens, output_tokens.
"""

import logging

from providers.bedrock_models import get_llm_model

logger = logging.getLogger(__name__)

# Hard constraint: max tokens to LLM (Rule 4)
_MAX_TOKENS = 1200


def generate_completion(
    system_prompt: str,
    user_prompt: str,
    provider: str | None = None,
    temperature: float = 0.0,
) -> tuple[str, dict]:
    """Generate LLM completion strictly enforcing max 1 LLM call per query.

    Returns:
        (text, usage) where usage = {"input_tokens": N, "output_tokens": M}
    """
    try:
        from aws.infra import get_client

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

        text = response["output"]["message"]["content"][0]["text"]

        # Extract real token counts from Bedrock Converse response
        raw_usage = response.get("usage", {})
        usage = {
            "input_tokens": raw_usage.get("inputTokens", 0),
            "output_tokens": raw_usage.get("outputTokens", 0),
        }

        logger.info(
            "llm_provider.tokens input=%d output=%d model=%s",
            usage["input_tokens"],
            usage["output_tokens"],
            get_llm_model(),
        )

        return text, usage

    except Exception as e:
        logger.error("LLM request failed: %s", str(e))
        raise e
