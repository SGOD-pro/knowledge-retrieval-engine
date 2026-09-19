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
from collections.abc import Sequence

from providers.bedrock_models import get_embedding_model

logger = logging.getLogger(__name__)

FULL_EMBEDDING_DIM = 1024

# Module-level token accumulator for cost tracking across a benchmark run.
# Reset with reset_token_counter(). Read with get_token_counter().
_token_counter = {"embed_calls": 0, "embed_input_tokens": 0}


def reset_token_counter() -> None:
    """Reset the embedding token counter (call at start of each benchmark query)."""
    _token_counter["embed_calls"] = 0
    _token_counter["embed_input_tokens"] = 0


def get_token_counter() -> dict:
    """Return a copy of the current embedding token counter."""
    return dict(_token_counter)


def get_embedding_dimension(provider: str | None = None) -> int:
    """Return the dimension for API-based (full-path) embeddings."""
    return FULL_EMBEDDING_DIM


def embed_text(
    text: str, provider: str | None = None, max_retries: int = 5
) -> list[float]:
    """Generate a 1024-dim embedding vector using Bedrock API.
    Fallback: Deterministic pseudo-embedding when API keys are absent.
    """
    dim = FULL_EMBEDDING_DIM

    last_err = None
    import time

    for attempt in range(max_retries):
        try:
            from aws.infra import get_client

            client = get_client("bedrock-runtime")
            # Titan V2 accepts up to 8k tokens. 1 token ~ 4 chars.
            # Truncating to 30,000 chars provides a safe margin.
            truncated_text = text[:30000]

            response = client.invoke_model(
                modelId=get_embedding_model(),
                contentType="application/json",
                accept="application/json",
                body=json.dumps(
                    {
                        "inputText": truncated_text,
                        "dimensions": dim,
                        "normalize": True,
                    }
                ),
            )
            response_body = json.loads(response.get("body").read())
            # Track token usage from Titan V2 response
            input_token_count = response_body.get("inputTextTokenCount", 0)
            _token_counter["embed_calls"] += 1
            _token_counter["embed_input_tokens"] += input_token_count
            logger.info(
                "embed_provider.tokens input=%d total_embed_tokens=%d",
                input_token_count,
                _token_counter["embed_input_tokens"],
            )
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
            last_err = e
            if "ThrottlingException" in str(e) or "Too many requests" in str(e):
                wait_time = (2**attempt) * 1.5
                logger.warning(
                    "embed_text: Throttled (attempt %d/%d), sleeping %.1fs...",
                    attempt + 1,
                    max_retries,
                    wait_time,
                )
                time.sleep(wait_time)
            else:
                logger.error("Embedding request failed: %s", str(e))
                break

    from config import settings

    if provider == "test" or (provider is None and settings.ENVIRONMENT == "test"):
        logger.warning("Falling back to deterministic pseudo-embedding")
        return _deterministic_vector(text, dim)

    raise RuntimeError(
        f"API embedding failed: {last_err}. Check rate limits or API key."
    )


def _deterministic_vector(text: str, dim: int) -> list[float]:
    """Word-hashed pseudo-embedding preserving semantic overlap for test/CI fallback."""
    import re

    _STOPWORDS = {
        "what", "is", "the", "a", "an", "in", "on", "at", "to", "for",
        "of", "and", "or", "with", "by", "from", "as", "are",
    }
    raw_words = re.findall(r"\w+", text.lower())
    words = [w for w in raw_words if w not in _STOPWORDS and len(w) > 2]
    if not words:
        words = raw_words or [text]
    vector = [0.0] * dim
    for word in words:
        seed = hashlib.sha256(word.encode("utf-8")).digest()
        for i in range(dim):
            vector[i] += (seed[i % len(seed)] / 127.5) - 1.0
    norm = sum(x * x for x in vector) ** 0.5
    if norm > 0:
        vector = [x / norm for x in vector]
    return vector


def embed_batch(
    texts: Sequence[str], provider: str | None = None, max_retries: int = 3
) -> list[list[float]]:
    from config import settings

    if provider == "test" or (provider is None and settings.ENVIRONMENT == "test"):
        return [_deterministic_vector(t, FULL_EMBEDDING_DIM) for t in texts]

    from concurrent.futures import ThreadPoolExecutor

    def _embed_single(text: str) -> list[float]:
        return embed_text(text, provider=provider)

    with ThreadPoolExecutor(max_workers=3) as executor:
        return list(executor.map(_embed_single, texts))
