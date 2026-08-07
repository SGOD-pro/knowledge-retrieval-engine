"""Embedding provider — API-based full-path embeddings (1024-dim).

Model Provider Matrix (ARCHITECTURE.md rev 5):
  - Prod: amazon.titan-embed-text-v2 (Bedrock) → 1024-dim
  - Dev:  Same as Prod

Local BGE-small ONNX (384-dim) is NOT handled here — it lives in
ingestion/embed_service.py and is invoked directly by vector_retriever.py
for fast-path queries. This module handles only the API-based full path.

Rule 28: All API embedding calls route through this module.
"""

import hashlib
import json
import logging
from typing import Sequence

from kre.providers.provider_client import get_active_provider
from kre.shared.bedrock_models import get_embedding_model

logger = logging.getLogger(__name__)

FULL_EMBEDDING_DIM = 1024

def get_embedding_dimension(provider: str | None = None) -> int:
    """Return the dimension for API-based (full-path) embeddings."""
    return FULL_EMBEDDING_DIM

def embed_text(text: str, provider: str | None = None) -> list[float]:
    """Generate a 1024-dim embedding vector using Bedrock API.
    Fallback: Deterministic pseudo-embedding when API keys are absent.
    """
    dim = FULL_EMBEDDING_DIM

    try:
        from kre.shared.aws import get_client
        client = get_client("bedrock-runtime")
        # Titan V2 accepts up to 8k tokens. 1 token ~ 4 chars.
        # Truncating to 30,000 chars provides a safe margin.
        truncated_text = text[:30000]

        response = client.invoke_model(
            modelId=get_embedding_model(),
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "inputText": truncated_text,
                "dimensions": dim,
                "normalize": True,
            }),
        )
        response_body = json.loads(response.get("body").read())
        vec = response_body.get("embedding")
        if vec:
            import numpy as np
            arr = np.array(vec, dtype=np.float32)
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr = arr / norm
            return arr.tolist()
        return vec
    except Exception as e:
        logger.error("Embedding request failed: %s", str(e))

    # Raise error instead of falling back to deterministic vector
    raise RuntimeError("API embedding failed. Check rate limits or API key.")

def _deterministic_vector(text: str, dim: int) -> list[float]:
    """SHA-256-seeded pseudo-embedding for test/CI fallback."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    vector = [(seed[i % len(seed)] / 127.5) - 1.0 for i in range(dim)]
    return vector

def embed_batch(texts: Sequence[str], provider: str | None = None) -> list[list[float]]:
    import time
    res = []
    for t in texts:
        res.append(embed_text(t, provider=provider))
        time.sleep(0.5)
    return res
