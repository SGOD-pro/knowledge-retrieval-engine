"""Ingestion-time embedding service.

Populates BOTH embedding columns at ingestion time for every chunk:
- embedding_fast (384-dim): BGE-small-en-v1.5 ONNX.
    prod  → call bge-embedding-lambda via boto3 (zero ONNX weight in query Lambda)
    dev   → local ONNX inference from bge-onnx/ directory
    test  → deterministic SHA-256 pseudo-embedding
- embedding_full (1024-dim): API provider (Titan V2).

Neither column is left null after ingestion.
"""

import hashlib
import json
import logging
import os
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from schemas.models import Chunk
from providers.embedding_provider import embed_text as api_embed_text

logger = logging.getLogger(__name__)

from config import settings

# ---------------------------------------------------------------------------
# BGE Lambda (prod) — from config
# ---------------------------------------------------------------------------

_BGE_LAMBDA_NAME = settings.BGE_EMBEDDING_LAMBDA_NAME

# ---------------------------------------------------------------------------
# Local ONNX (dev) — paths
# ---------------------------------------------------------------------------

_BGE_MODEL_DIR = os.environ.get(
    "BGE_SMALL_MODEL_DIR",
    str(Path(__file__).resolve().parent.parent.parent.parent / "bge_microservice" / "bge-onnx"),
)

_BGE_ONNX_SESSION = None
_BGE_TOKENIZER = None


def _get_bge_session():
    global _BGE_ONNX_SESSION
    if _BGE_ONNX_SESSION is None:
        import onnxruntime as ort
        model_path = os.path.join(_BGE_MODEL_DIR, "model.onnx")
        if os.path.exists(model_path):
            _BGE_ONNX_SESSION = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"]
            )
            logger.info("bge.local_session_loaded path=%s", model_path)
        else:
            logger.warning("bge.local_model_not_found path=%s", model_path)
    return _BGE_ONNX_SESSION


def _get_bge_tokenizer():
    global _BGE_TOKENIZER
    if _BGE_TOKENIZER is None:
        from tokenizers import Tokenizer
        tokenizer_path = os.path.join(_BGE_MODEL_DIR, "tokenizer.json")
        if os.path.exists(tokenizer_path):
            _BGE_TOKENIZER = Tokenizer.from_file(tokenizer_path)
            _BGE_TOKENIZER.enable_truncation(max_length=512)
            _BGE_TOKENIZER.enable_padding(length=512)
        else:
            logger.warning("bge.local_tokenizer_not_found path=%s", tokenizer_path)
    return _BGE_TOKENIZER


# ---------------------------------------------------------------------------
# embed_fast_local — routes by ENVIRONMENT
# ---------------------------------------------------------------------------

def embed_fast_local(text: str) -> list[float]:
    """Generate a 384-dim embedding.

    prod  → boto3 invoke bge-embedding-lambda
    dev   → local ONNX
    test  → deterministic fallback (no network, no file deps)
    """
    environment = settings.ENVIRONMENT
    t0 = time.perf_counter()

    if environment == "test":
        vec = _deterministic_vector(text, 384)
        logger.debug("bge.mode=deterministic_fallback latency_ms=%.2f", (time.perf_counter() - t0) * 1000)
        return vec

    if environment == "prod":
        vec = _call_bge_lambda(text)
        logger.info("bge.mode=lambda latency_ms=%.2f", (time.perf_counter() - t0) * 1000)
        return vec

    # dev — local ONNX
    vec = _run_onnx(text)
    logger.info("bge.mode=local_onnx latency_ms=%.2f", (time.perf_counter() - t0) * 1000)
    return vec


def _call_bge_lambda(text: str) -> list[float]:
    """Invoke the deployed BGE Lambda and return the 384-dim embedding."""
    try:
        from aws.infra import get_client
        client = get_client("lambda")
        response = client.invoke(
            FunctionName=_BGE_LAMBDA_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps({"text": text}),
        )
        payload = json.loads(response["Payload"].read())
        if "error" in payload:
            raise RuntimeError(f"BGE Lambda error: {payload['error']}")
        return payload["embedding"]
    except Exception as e:
        logger.warning("bge.lambda_failed error=%s — falling back to local ONNX", e)
        return _run_onnx(text)


def _call_bge_lambda_batch(texts: list[str]) -> list[list[float]]:
    """Invoke the deployed BGE Lambda with a batch of texts."""
    try:
        from aws.infra import get_client
        client = get_client("lambda")
        response = client.invoke(
            FunctionName=_BGE_LAMBDA_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps({"texts": texts}),
        )
        payload = json.loads(response["Payload"].read())
        if "error" in payload:
            raise RuntimeError(f"BGE Lambda batch error: {payload['error']}")
        return payload["embeddings"]
    except Exception as e:
        logger.warning("bge.lambda_batch_failed error=%s — falling back to local ONNX", e)
        return [_run_onnx(t) for t in texts]


def _run_onnx(text: str) -> list[float]:
    """Local ONNX inference for dev mode."""
    session = _get_bge_session()
    tokenizer = _get_bge_tokenizer()

    if session is None or tokenizer is None:
        logger.warning("bge.local_onnx_unavailable falling back to deterministic")
        return _deterministic_vector(text, 384)

    encoded = tokenizer.encode(text)
    input_ids = np.array([encoded.ids], dtype=np.int64)
    attention_mask = np.array([encoded.attention_mask], dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

    outputs = session.run(
        None,
        {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        },
    )
    token_embeddings = outputs[0]
    mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
    summed = np.sum(token_embeddings * mask_expanded, axis=1)
    counted = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
    pooled = (summed / counted).flatten()

    norm = np.linalg.norm(pooled)
    if norm > 0:
        pooled = pooled / norm
    return pooled.tolist()


def _run_onnx_batch(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    """High-throughput batch ONNX inference using tokenizer.encode_batch."""
    session = _get_bge_session()
    tokenizer = _get_bge_tokenizer()

    if session is None or tokenizer is None or not texts:
        return [_deterministic_vector(t, 384) for t in texts]

    results = []
    for i in range(0, len(texts), batch_size):
        sub_texts = texts[i : i + batch_size]
        encoded_batch = tokenizer.encode_batch(sub_texts)

        input_ids = np.array([e.ids for e in encoded_batch], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded_batch], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

        outputs = session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )
        token_embeddings = outputs[0]
        mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
        summed = np.sum(token_embeddings * mask_expanded, axis=1)
        counted = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        pooled = summed / counted

        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms[norms == 0] = 1e-9
        normalized = (pooled / norms).tolist()
        results.extend(normalized)

    return results


def embed_fast_batch(texts: list[str]) -> list[list[float]]:
    """Batch fast embeddings — routes by ENVIRONMENT."""
    environment = settings.ENVIRONMENT
    t0 = time.perf_counter()

    if environment == "test":
        result = [_deterministic_vector(t, 384) for t in texts]
    elif environment == "prod":
        result = _call_bge_lambda_batch(texts)
    else:
        result = _run_onnx_batch(texts)

    logger.info(
        "bge.batch_embed count=%d env=%s latency_ms=%.2f",
        len(texts), environment, (time.perf_counter() - t0) * 1000,
    )
    return result


def _deterministic_vector(text: str, dim: int) -> list[float]:
    """SHA-256-seeded pseudo-embedding for test/CI fallback."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    vector = [(seed[i % len(seed)] / 127.5) - 1.0 for i in range(dim)]
    norm = sum(x * x for x in vector) ** 0.5
    if norm > 0:
        vector = [x / norm for x in vector]
    return vector


def embed_chunks_dual(chunks: list[Chunk], provider: str | None = None) -> list[Chunk]:
    """Populate both embedding columns for all chunks at ingestion time.

    - embedding_fast: BGE-small (384-dim). Prod=Lambda, dev=ONNX, test=deterministic.
    - embedding_full: Titan V2 API (1024-dim).
    """
    texts = [c.text for c in chunks]

    t0 = time.perf_counter()
    fast_embeddings = embed_fast_batch(texts)
    logger.info("embed_service.fast_done count=%d latency_ms=%.2f", len(texts), (time.perf_counter() - t0) * 1000)

    from providers.embedding_provider import embed_batch as api_embed_batch
    t1 = time.perf_counter()
    full_embeddings = api_embed_batch(texts, provider=provider)
    logger.info("embed_service.full_done count=%d latency_ms=%.2f", len(texts), (time.perf_counter() - t1) * 1000)

    # Hard integrity check before returning
    for i, (f_emb, full_emb) in enumerate(zip(fast_embeddings, full_embeddings)):
        if f_emb is None or len(f_emb) != 384:
            raise ValueError(f"Chunk {chunks[i].id} has invalid embedding_fast (expected 384-dim, got {len(f_emb) if f_emb else None})")
        if full_emb is None or len(full_emb) != 1024:
            raise ValueError(f"Chunk {chunks[i].id} has invalid embedding_full (expected 1024-dim, got {len(full_emb) if full_emb else None})")
        if all(x == 0.0 for x in f_emb):
            raise ValueError(f"Chunk {chunks[i].id} has all-zero embedding_fast")
        if all(x == 0.0 for x in full_emb):
            raise ValueError(f"Chunk {chunks[i].id} has all-zero embedding_full")
        if all(x == 0.0 for x in full_emb[384:]):
            raise ValueError(f"Chunk {chunks[i].id} has zero-padded embedding_full (dimensions 384:1024 are all zero)")

    return [
        replace(chunk, embedding_fast=emb_fast, embedding_full=emb_full)
        for chunk, emb_fast, emb_full in zip(chunks, fast_embeddings, full_embeddings)
    ]
