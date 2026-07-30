"""Embedding provider — handles both local fast-path and API full-path embeddings.

Model Provider Matrix (ARCHITECTURE.md rev 5):
  - Fast path (Local): BGE-small-en-v1.5 via ONNX Runtime → 384-dim
  - Full path (Prod): amazon.titan-embed-text-v2:0 (Bedrock) → 1024-dim
  - Full path (Dev): nvidia/nemotron-3-embed-1b (OpenRouter) → 1024-dim

Rule 28: All API embedding calls route through this module.
Rule 19: Fast path uses local BGE-small (zero network calls).
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Sequence

import numpy as np
import onnxruntime as ort

from kre.shared.providers.provider_client import get_active_provider

logger = logging.getLogger(__name__)

# Model IDs per ARCHITECTURE.md Model Provider Matrix
_PROD_MODEL_ID = "amazon.titan-embed-text-v2:0"
_DEV_MODEL_ID = "nvidia/nemotron-3-embed-1b"

FULL_EMBEDDING_DIM = 1024
FAST_EMBEDDING_DIM = 384


# ---------------------------------------------------------------------------
# Local BGE-small ONNX inference for embedding_fast (384-dim)
# ---------------------------------------------------------------------------

_BGE_ONNX_SESSION: ort.InferenceSession | None = None
_BGE_TOKENIZER = None

# BGE-small weights path — configurable via env var for Lambda packaging
_BGE_MODEL_DIR = os.environ.get(
    "BGE_SMALL_MODEL_DIR",
    str(Path(__file__).resolve().parent.parent.parent / "models" / "bge-small-en-v1.5"),
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
        try:
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
        except ImportError:
            logger.warning("tokenizers library not found. Fast embeddings will use deterministic fallback.")
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

    # Deterministic fallback for environments without model files
    return _deterministic_vector(text, dim=FAST_EMBEDDING_DIM)


def embed_fast_batch(texts: Sequence[str]) -> list[list[float]]:
    """Batch wrapper for local BGE-small embedding."""
    return [embed_fast_local(t) for t in texts]


# ---------------------------------------------------------------------------
# API-based Inference for embedding_full (1024-dim)
# ---------------------------------------------------------------------------

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
            from kre.shared.config import get_boto3_client
            client = get_boto3_client("bedrock-runtime")
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


def embed_batch(texts: Sequence[str], provider: str | None = None) -> list[list[float]]:
    """Generate API embeddings for multiple texts."""
    return [embed_text(text, provider=provider) for text in texts]


def _deterministic_vector(text: str, dim: int) -> list[float]:
    """SHA-256-seeded pseudo-embedding for test/CI fallback."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    vector = [(seed[i % len(seed)] / 127.5) - 1.0 for i in range(dim)]
    norm = sum(x * x for x in vector) ** 0.5
    if norm > 0:
        vector = [x / norm for x in vector]
    return vector
