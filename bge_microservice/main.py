"""BGE-Small Embedding Lambda Handler

Pure AWS Lambda function — no FastAPI, no Mangum, no uvicorn.
Entry point: main.lambda_handler

Model files live in ./bge-onnx/ directory (model.onnx, tokenizer.json).

Event shapes:
    Single: {"text": "your query here"}
    Batch:  {"texts": ["query1", "query2", ...]}

Response shapes:
    Single: {"embedding": [0.123, ...], "dim": 384}
    Batch:  {"embeddings": [[...], [...]],  "dim": 384}

Batch uses ProcessPoolExecutor with MAX_WORKERS=6.
Each worker process loads its own ONNX session (sessions are not picklable).
"""

import json
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Paths — model files live in bge-onnx/ subfolder
# ---------------------------------------------------------------------------

_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.environ.get("BGE_MODEL_DIR", os.path.join(_DIR, "bge-onnx"))
MODEL_PATH = os.path.join(MODEL_DIR, "model.onnx")
TOKENIZER_PATH = os.path.join(MODEL_DIR, "tokenizer.json")
EMBEDDING_DIM = 384
MAX_WORKERS = 6

# Module-level cache — shared inside the SAME process (Lambda warm reuse)
_session = None
_tokenizer = None


# ---------------------------------------------------------------------------
# Model loading — lazy, process-local
# ---------------------------------------------------------------------------

def _load_model() -> None:
    """Load ONNX session and tokenizer once per process."""
    global _session, _tokenizer
    if _session is not None:
        return

    t0 = time.perf_counter()
    import onnxruntime as ort
    from tokenizers import Tokenizer

    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(
            f"model.onnx not found at {MODEL_PATH}. "
            "Place BGE-small ONNX weights in the bge-onnx/ directory."
        )

    _session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
    _tokenizer = Tokenizer.from_file(TOKENIZER_PATH)
    _tokenizer.enable_truncation(max_length=512)
    _tokenizer.enable_padding(length=512)

    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info("bge.model_loaded latency_ms=%.2f path=%s", latency_ms, MODEL_PATH)


# ---------------------------------------------------------------------------
# Core inference — single text → 384-dim L2-normalized vector
# ---------------------------------------------------------------------------

def _infer(text: str) -> list[float]:
    """Run ONNX inference for a single text. Caller must ensure model is loaded."""
    encoded = _tokenizer.encode(text)
    input_ids = np.array([encoded.ids], dtype=np.int64)
    attention_mask = np.array([encoded.attention_mask], dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

    outputs = _session.run(
        None,
        {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        },
    )

    # Mean pooling: outputs[0] shape = (1, seq_len, 384)
    token_embeddings = outputs[0]
    mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
    summed = np.sum(token_embeddings * mask_expanded, axis=1)
    counts = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
    pooled = summed / counts  # (1, 384)

    # L2 normalize
    norm = np.linalg.norm(pooled, axis=1, keepdims=True)
    normalized = (pooled / np.clip(norm, a_min=1e-9, a_max=None))[0]

    return normalized.tolist()


# ---------------------------------------------------------------------------
# Standalone worker for ProcessPoolExecutor
# Each spawned process calls this; it loads its own session on first call.
# Must be defined at module level for pickling.
# ---------------------------------------------------------------------------

def _worker_embed(text: str) -> list[float]:
    """Subprocess worker: load model (once per process) and run inference."""
    _load_model()
    return _infer(text)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context) -> dict:
    t0 = time.perf_counter()

    # ── Single embed ──────────────────────────────────────────────────────
    if "text" in event:
        text = event["text"]
        logger.info("bge.mode=single text_len=%d", len(text))
        _load_model()
        embedding = _infer(text)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.info("bge.latency_ms=%.2f bge.dim=%d", latency_ms, EMBEDDING_DIM)
        return {"embedding": embedding, "dim": EMBEDDING_DIM}

    # ── Batch embed ───────────────────────────────────────────────────────
    if "texts" in event:
        texts: list[str] = event["texts"]
        count = len(texts)
        logger.info("bge.mode=batch bge.batch_count=%d", count)

        if count == 0:
            return {"embeddings": [], "dim": EMBEDDING_DIM}

        if count == 1:
            _load_model()
            embeddings = [_infer(texts[0])]
        else:
            workers = min(MAX_WORKERS, count)
            embeddings: list[list[float]] = [None] * count  # type: ignore[assignment]

            with ProcessPoolExecutor(max_workers=workers) as executor:
                future_to_idx = {
                    executor.submit(_worker_embed, text): idx
                    for idx, text in enumerate(texts)
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    embeddings[idx] = future.result()

        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(
            "bge.batch_latency_ms=%.2f bge.batch_count=%d bge.workers=%d",
            latency_ms, count, min(MAX_WORKERS, count),
        )
        return {"embeddings": embeddings, "dim": EMBEDDING_DIM}

    # ── Unknown event ─────────────────────────────────────────────────────
    logger.error("bge.error=unknown_event event_keys=%s", list(event.keys()))
    return {"error": "Event must contain 'text' (single) or 'texts' (batch)"}
