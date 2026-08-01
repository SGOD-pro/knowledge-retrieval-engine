"""Ingestion-time embedding service.

Populates BOTH embedding columns at ingestion time for every chunk:
- embedding_fast (384-dim): Local BGE-small-en-v1.5 via ONNX runtime.
- embedding_full (1024-dim): API provider (Titan V2 prod / Nemotron dev).

This ensures both fast-path and full-path queries have pre-computed
vectors to search against. Neither column is left null after ingestion.
"""

import hashlib
import logging
import os
from dataclasses import replace
from pathlib import Path

import numpy as np
import onnxruntime as ort

from kre.models import Chunk
from kre.providers.embedding_provider import embed_text as api_embed_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Local BGE-small ONNX inference for embedding_fast (384-dim)
# ---------------------------------------------------------------------------

_BGE_ONNX_SESSION: ort.InferenceSession | None = None
_BGE_TOKENIZER = None

# BGE-small weights path — configurable via env var for Lambda packaging
_BGE_MODEL_DIR = os.environ.get(
    "BGE_SMALL_MODEL_DIR",
    str(Path(__file__).resolve().parent.parent / "models" / "bge-small-en-v1.5"),
)


def _get_bge_session() -> ort.InferenceSession:
    """Lazy-load the ONNX session for BGE-small."""
    global _BGE_ONNX_SESSION
    if _BGE_ONNX_SESSION is None:
        model_path = os.path.join(_BGE_MODEL_DIR, "model.onnx")
        if os.path.exists(model_path):
            _BGE_ONNX_SESSION = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"]
            )
        else:
            logger.warning(
                "BGE-small ONNX model not found at %s; fast embeddings will use deterministic fallback.",
                model_path,
            )
    return _BGE_ONNX_SESSION


def _get_bge_tokenizer():
    """Lazy-load the tokenizer for BGE-small using the `tokenizers` library.

    Rule 27: No `transformers` or `sentence-transformers` imports.
    Uses HuggingFace `tokenizers` library directly.
    """
    global _BGE_TOKENIZER
    if _BGE_TOKENIZER is None:
        from tokenizers import Tokenizer

        tokenizer_path = os.path.join(_BGE_MODEL_DIR, "tokenizer.json")
        if os.path.exists(tokenizer_path):
            _BGE_TOKENIZER = Tokenizer.from_file(tokenizer_path)
            _BGE_TOKENIZER.enable_truncation(max_length=512)
            _BGE_TOKENIZER.enable_padding(length=512)
        else:
            logger.warning(
                "BGE-small tokenizer not found at %s; fast embeddings will use deterministic fallback.",
                tokenizer_path,
            )
    return _BGE_TOKENIZER


def embed_fast_local(text: str) -> list[float]:
    """Generate a 384-dim embedding using local BGE-small ONNX inference.

    Falls back to a deterministic pseudo-embedding if the ONNX model
    or tokenizer files are not present (test/CI environments).
    """
    session = _get_bge_session()
    tokenizer = _get_bge_tokenizer()

    if session is not None and tokenizer is not None:
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
        # Mean pooling over token embeddings (output shape: [1, seq_len, 384])
        token_embeddings = outputs[0]
        mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
        summed = np.sum(token_embeddings * mask_expanded, axis=1)
        counted = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        pooled = (summed / counted).flatten()

        # L2 normalize
        norm = np.linalg.norm(pooled)
        if norm > 0:
            pooled = pooled / norm

        return pooled.tolist()

    # Raise error if model files are missing instead of falling back silently
    raise RuntimeError(f"BGE-small ONNX model or tokenizer missing at expected path: {_BGE_MODEL_DIR}")


def embed_fast_batch(texts: list[str]) -> list[list[float]]:
    """Batch wrapper for local BGE-small embedding."""
    return [embed_fast_local(t) for t in texts]


def _deterministic_vector(text: str, dim: int) -> list[float]:
    """SHA-256-seeded pseudo-embedding for test/CI fallback."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    vector = [(seed[i % len(seed)] / 127.5) - 1.0 for i in range(dim)]
    norm = sum(x * x for x in vector) ** 0.5
    if norm > 0:
        vector = [x / norm for x in vector]
    return vector


def embed_chunks_dual(chunks: list[Chunk], provider: str = "dev") -> list[Chunk]:
    """Populate both embedding columns for a list of chunks at ingestion time.

    - embedding_fast: generated locally via BGE-small ONNX (384-dim).
    - embedding_full: generated via API provider (1024-dim).

    Returns new Chunk instances with both embeddings populated.
    """
    texts = [c.text for c in chunks]

    # Fast embeddings — always local ONNX, zero network calls
    fast_embeddings = embed_fast_batch(texts)

    from kre.providers.embedding_provider import embed_batch as api_embed_batch

    # Full embeddings — always API provider
    full_embeddings = api_embed_batch(texts, provider=provider)

    result = []
    for chunk, emb_fast, emb_full in zip(chunks, fast_embeddings, full_embeddings):
        result.append(
            replace(chunk, embedding_fast=emb_fast, embedding_full=emb_full)
        )
    return result
