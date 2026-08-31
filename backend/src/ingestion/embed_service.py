"""Ingestion and query-time fast embedding service.

Populates BOTH embedding columns:
- embedding_fast (384-dim): BGE-small-en-v1.5 via AWS Lambda function.
    Lambda Function: settings.BGE_EMBEDDING_LAMBDA_NAME (default: bge-microservice-stack-BGELambdaFunction-roIuowXCDxCe)
    test mode: deterministic SHA-256 pseudo-embedding for fast offline tests.
- embedding_full (1024-dim): API provider (Titan V2 / OpenRouter).

Neither column is left null after ingestion.
"""

import hashlib
import json
import logging
import time
from dataclasses import replace

from config import settings
from schemas.models import Chunk

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core Lambda Invocation
# ---------------------------------------------------------------------------


def _call_bge_lambda_batch(texts: list[str]) -> list[list[float]]:
    """Invoke the deployed BGE Lambda with a batch of texts."""
    if not texts:
        return []

    from aws.infra import get_client

    client = get_client("lambda")
    function_name = settings.BGE_EMBEDDING_LAMBDA_NAME
    payload = json.dumps({"texts": texts})

    try:
        response = client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=payload.encode("utf-8"),
        )
        response_payload = json.loads(response["Payload"].read())

        if "embeddings" in response_payload:
            embeddings = response_payload["embeddings"]
            logger.debug(
                "bge.lambda_success count=%d dim=%s",
                len(embeddings),
                response_payload.get("dim", 384),
            )
            return embeddings
        elif "embedding" in response_payload:
            return [response_payload["embedding"]]
        elif "error" in response_payload:
            raise RuntimeError(f"Lambda returned an error: {response_payload['error']}")
        else:
            raise RuntimeError(f"Unexpected response from BGE Lambda: {response_payload}")
    except Exception as e:
        logger.error("bge.lambda_failed func=%s error=%s", function_name, e)
        raise


def _call_bge_lambda(text: str) -> list[float]:
    """Invoke the deployed BGE Lambda for a single text and return the 384-dim embedding."""
    results = _call_bge_lambda_batch([text])
    if not results:
        raise RuntimeError("BGE Lambda returned empty embeddings list")
    return results[0]


# ---------------------------------------------------------------------------
# Public Embedding Functions
# ---------------------------------------------------------------------------


def embed_fast_local(text: str) -> list[float]:
    """Generate a 384-dim embedding via BGE Lambda (or deterministic fallback in test mode)."""
    environment = settings.ENVIRONMENT
    t0 = time.perf_counter()

    if environment == "test":
        vec = _deterministic_vector(text, 384)
        logger.debug(
            "bge.mode=deterministic_fallback latency_ms=%.2f",
            (time.perf_counter() - t0) * 1000,
        )
        return vec

    vec = _call_bge_lambda(text)
    logger.info(
        "bge.mode=lambda latency_ms=%.2f", (time.perf_counter() - t0) * 1000
    )
    return vec


def embed_fast_batch(texts: list[str]) -> list[list[float]]:
    """Batch fast embeddings via BGE Lambda (or deterministic fallback in test mode)."""
    if not texts:
        return []

    environment = settings.ENVIRONMENT
    t0 = time.perf_counter()

    if environment == "test":
        result = [_deterministic_vector(t, 384) for t in texts]
    else:
        result = _call_bge_lambda_batch(texts)

    logger.info(
        "bge.batch_embed count=%d env=%s latency_ms=%.2f",
        len(texts),
        environment,
        (time.perf_counter() - t0) * 1000,
    )
    return result


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


# ---------------------------------------------------------------------------
# Dual Ingestion Embedding
# ---------------------------------------------------------------------------


def embed_chunks_dual(chunks: list[Chunk], provider: str | None = None) -> list[Chunk]:
    """Populate both embedding columns for all chunks at ingestion time.

    - embedding_fast: BGE-small (384-dim) via AWS Lambda.
    - embedding_full: Titan V2 API / OpenRouter (1024-dim).
    """
    if not chunks:
        return []

    texts = [c.text for c in chunks]

    t0 = time.perf_counter()
    fast_embeddings = embed_fast_batch(texts)
    logger.info(
        "embed_service.fast_done count=%d latency_ms=%.2f",
        len(texts),
        (time.perf_counter() - t0) * 1000,
    )

    from providers.embedding_provider import embed_batch as api_embed_batch

    t1 = time.perf_counter()
    full_embeddings = api_embed_batch(texts, provider=provider)
    logger.info(
        "embed_service.full_done count=%d latency_ms=%.2f",
        len(texts),
        (time.perf_counter() - t1) * 1000,
    )

    # Hard integrity check before returning
    for i, (f_emb, full_emb) in enumerate(zip(fast_embeddings, full_embeddings)):
        if f_emb is None or len(f_emb) != 384:
            raise ValueError(
                f"Chunk {chunks[i].id} has invalid embedding_fast (expected 384-dim, got {len(f_emb) if f_emb else None})"
            )
        if full_emb is None or len(full_emb) != 1024:
            raise ValueError(
                f"Chunk {chunks[i].id} has invalid embedding_full (expected 1024-dim, got {len(full_emb) if full_emb else None})"
            )
        if all(x == 0.0 for x in f_emb):
            raise ValueError(f"Chunk {chunks[i].id} has all-zero embedding_fast")
        if all(x == 0.0 for x in full_emb):
            raise ValueError(f"Chunk {chunks[i].id} has all-zero embedding_full")
        if all(x == 0.0 for x in full_emb[384:]):
            raise ValueError(
                f"Chunk {chunks[i].id} has zero-padded embedding_full (dimensions 384:1024 are all zero)"
            )

    return [
        replace(chunk, embedding_fast=emb_fast, embedding_full=emb_full)
        for chunk, emb_fast, emb_full in zip(chunks, fast_embeddings, full_embeddings)
    ]
