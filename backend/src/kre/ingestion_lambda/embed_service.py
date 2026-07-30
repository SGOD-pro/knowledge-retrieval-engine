"""Ingestion-time embedding service.

Populates BOTH embedding columns at ingestion time for every chunk:
- embedding_fast (384-dim): Local BGE-small-en-v1.5 via ONNX runtime.
- embedding_full (1024-dim): API provider (Titan V2 prod / Nemotron dev).

This ensures both fast-path and full-path queries have pre-computed
vectors to search against. Neither column is left null after ingestion.
"""

import logging
from dataclasses import replace

from kre.shared.models import Chunk
from kre.shared.providers.embedding_provider import embed_fast_batch, embed_text as api_embed_text

logger = logging.getLogger(__name__)


def embed_chunks_dual(chunks: list[Chunk], provider: str = "dev") -> list[Chunk]:
    """Populate both embedding columns for a list of chunks at ingestion time.

    - embedding_fast: generated locally via BGE-small ONNX (384-dim).
    - embedding_full: generated via API provider (1024-dim).

    Returns new Chunk instances with both embeddings populated.
    """
    texts = [c.text for c in chunks]

    # Fast embeddings — always local ONNX, zero network calls
    fast_embeddings = embed_fast_batch(texts)

    # Full embeddings — always API provider
    full_embeddings = [api_embed_text(t, provider=provider) for t in texts]

    result = []
    for chunk, emb_fast, emb_full in zip(chunks, fast_embeddings, full_embeddings):
        result.append(
            replace(chunk, embedding_fast=emb_fast, embedding_full=emb_full)
        )
    return result
