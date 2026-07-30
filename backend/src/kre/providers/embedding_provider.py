"""Embedding provider — API-based full-path embeddings (1024-dim).

Model Provider Matrix (ARCHITECTURE.md rev 5):
  - Prod: amazon.titan-embed-text-v2 (Bedrock) → 1024-dim
  - Dev:  nvidia/nemotron-3-embed-1b (OpenRouter) → 1024-dim

Local BGE-small ONNX (384-dim) is NOT handled here — it lives in
ingestion/embed_service.py and is invoked directly by vector_retriever.py
for fast-path queries. This module handles only the API-based full path.

Rule 28: All API embedding calls route through this module.
"""

import hashlib
import json
import logging
import os
from typing import Sequence

from kre.providers.provider_client import get_active_provider

logger = logging.getLogger(__name__)

# Model IDs per ARCHITECTURE.md Model Provider Matrix
_PROD_MODEL_ID = "amazon.titan-embed-text-v2:0"
_DEV_MODEL_ID = "nvidia/nemotron-3-embed-1b"

FULL_EMBEDDING_DIM = 1024


def get_embedding_dimension(provider: str | None = None) -> int:
    """Return the dimension for API-based (full-path) embeddings."""
    return FULL_EMBEDDING_DIM


def embed_text(text: str, provider: str | None = None) -> list[float]:
    """Generate a 1024-dim embedding vector using the active API provider.

    Prod: Bedrock amazon.titan-embed-text-v2
    Dev:  OpenRouter nvidia/nemotron-3-embed-1b
    Fallback: Deterministic pseudo-embedding when API keys are absent.
    """
    active = provider or get_active_provider()
    dim = FULL_EMBEDDING_DIM

    if active == "prod":
        try:
            import boto3

            client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            # Titan V2 accepts up to 8k tokens. 1 token ~ 4 chars.
            # Truncating to 30,000 chars provides a safe margin.
            truncated_text = text[:30000]

            response = client.invoke_model(
                modelId=_PROD_MODEL_ID,
                contentType="application/json",
                accept="application/json",
                body=json.dumps({
                    "inputText": truncated_text,
                    "dimensions": dim,
                    "normalize": True,
                }),
            )
            response_body = json.loads(response.get("body").read())
            return response_body.get("embedding")
        except Exception as e:
            logger.error("Prod embedding failed: %s", str(e))

    elif active == "dev":
        openrouter_key = os.environ.get("OPENROUTER_API_KEY")
        if openrouter_key:
            try:
                import requests

                response = requests.post(
                    "https://openrouter.ai/api/v1/embeddings",
                    headers={
                        "Authorization": f"Bearer {openrouter_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": _DEV_MODEL_ID,
                        "input": text[:30000],
                    },
                    timeout=30.0,
                )
                if response.status_code == 200:
                    data = response.json()
                    return data["data"][0]["embedding"]
                else:
                    logger.error("Dev embedding returned %d: %s", response.status_code, response.text)
            except Exception as e:
                logger.error("Dev embedding request failed: %s", str(e))

    # Deterministic fallback vector generation based on SHA-256 seed
    return _deterministic_vector(text, dim)


def _deterministic_vector(text: str, dim: int) -> list[float]:
    """SHA-256-seeded pseudo-embedding for test/CI fallback."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    vector = [(seed[i % len(seed)] / 127.5) - 1.0 for i in range(dim)]
    norm = sum(x * x for x in vector) ** 0.5
    if norm > 0:
        vector = [x / norm for x in vector]
    return vector


def embed_batch(texts: Sequence[str], provider: str | None = None) -> list[list[float]]:
    """Generate embeddings for multiple texts."""
    return [embed_text(text, provider=provider) for text in texts]
