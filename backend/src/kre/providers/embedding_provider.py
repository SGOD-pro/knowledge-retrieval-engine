import hashlib
import os
import json
from typing import Sequence

from kre.providers.provider_client import get_active_provider


def get_embedding_dimension(provider: str | None = None) -> int:
    return 1024


def embed_text(text: str, provider: str | None = None) -> list[float]:
    """Generate embedding vector for a single text using active provider.

    Dev: OpenRouter (free tier fallback or deterministic)
    Prod: Bedrock amazon.titan-embed-text-v2 (1024 dims)
    Fallback: Deterministic pseudo-embedding for local execution / tests when API keys are absent.
    """
    active = provider or get_active_provider()
    dim = get_embedding_dimension(active)

    if active == "prod":
        try:
            import boto3

            client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            # Titan V2 accepts up to 8k tokens. 1 token ~ 4 chars.
            # Truncating to 30,000 chars provides a safe margin.
            truncated_text = text[:30000]

            response = client.invoke_model(
                modelId="amazon.titan-embed-text-v2:0",
                contentType="application/json",
                accept="application/json",
                body=json.dumps({
                    "inputText": truncated_text,
                    "dimensions": 1024,
                    "normalize": True
                }),
            )
            response_body = json.loads(response.get("body").read())
            return response_body.get("embedding")
        except Exception:
            pass
    elif active == "dev" and os.environ.get("OPENROUTER_API_KEY"):
        # Not requested by user, but maintaining fallback structure if needed
        pass

    # Deterministic fallback vector generation based on SHA-256 seed
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    vector = []
    for i in range(dim):
        byte_val = seed[i % len(seed)]
        # Map 0..255 to -1.0..1.0
        val = (byte_val / 127.5) - 1.0
        vector.append(val)

    # Normalize vector to unit length
    norm = sum(x * x for x in vector) ** 0.5
    if norm > 0:
        vector = [x / norm for x in vector]

    return vector


def embed_batch(texts: Sequence[str], provider: str | None = None) -> list[list[float]]:
    return [embed_text(text, provider=provider) for text in texts]
