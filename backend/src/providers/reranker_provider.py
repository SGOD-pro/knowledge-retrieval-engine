"""Reranker provider — API-based document reranking.

Model Provider Matrix:
  - Primary:  OpenRouter Rerank API (nvidia/llama-nemotron-rerank-vl-1b-v2:free)
  - Fallback: Term coverage / lexical scoring (deterministic, no API call)
  - Legacy:   Bedrock Cohere / NVIDIA NIM direct (deprecated)

Rule 6: Reranker runs before compression. Always.
Rule 27: No GPU packages (torch, transformers, faiss) — onnxruntime allowed only in BGE microservice.
Rule 28: All reranker calls route through this module.
"""

import json
import logging
import math
import os
import random
import threading
import time
import requests

from config import settings

logger = logging.getLogger(__name__)

# OpenRouter Reranker endpoint
_OPENROUTER_RERANK_URL = "https://openrouter.ai/api/v1/rerank"

# Rate limiter / token bucket state
_rl_lock = threading.Lock()
_last_request_time = 0.0
REQUEST_INTERVAL_SECONDS = 0.2  # Max 5 requests per second

_session: requests.Session | None = None
_session_lock = threading.Lock()


def _get_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=10,
                pool_maxsize=20,
                max_retries=0,
            )
            _session.mount("https://", adapter)
            _session.mount("http://", adapter)
        return _session


def _wait_for_rate_limit():
    global _last_request_time
    with _rl_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < REQUEST_INTERVAL_SECONDS:
            time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)
        _last_request_time = time.monotonic()


def _openrouter_reranker(query: str, documents: list[str]) -> list[float]:
    """Rerank documents using OpenRouter Rerank API.

    API endpoint: https://openrouter.ai/api/v1/rerank
    Payload:
      {
        "model": "nvidia/llama-nemotron-rerank-vl-1b-v2:free",
        "query": query,
        "documents": [{"text": doc}, ...],
        "top_n": len(documents)
      }
    Response:
      {
        "results": [
          {"index": 0, "relevance_score": 0.1539, ...},
          ...
        ]
      }
    """
    api_key = settings.OPENROUTER_API_KEY or os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is not configured")

    model = getattr(
        settings,
        "OPENROUTER_RERANKER_MODEL",
        "nvidia/llama-nemotron-rerank-vl-1b-v2:free",
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/SGOD-pro/knowledge-retrieval-engine",
        "X-OpenRouter-Title": "Knowledge Retrieval Engine",
    }

    payload = {
        "model": model,
        "query": query,
        "documents": [{"text": doc} for doc in documents],
        "top_n": len(documents),
    }

    session = _get_session()
    max_retries = 3
    base_backoff = 1.0

    for attempt in range(max_retries):
        _wait_for_rate_limit()
        try:
            response = session.post(
                _OPENROUTER_RERANK_URL,
                headers=headers,
                json=payload,
                timeout=15,
            )

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == max_retries - 1:
                    response.raise_for_status()
                sleep_time = (base_backoff * (2**attempt)) + random.uniform(0, 0.5)
                logger.warning(
                    "OpenRouter reranker returned %d. Retrying in %.2fs (attempt %d/%d)",
                    response.status_code,
                    sleep_time,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(sleep_time)
                continue

            # Fail fast on client errors (401, 403, 404, etc.)
            if 400 <= response.status_code < 500:
                logger.error(
                    "OpenRouter reranker client error %d: %s",
                    response.status_code,
                    response.text[:200],
                )
                response.raise_for_status()

            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])

            scores = [0.0] * len(documents)
            for r in results:
                idx = r.get("index")
                if idx is not None and 0 <= idx < len(documents):
                    scores[idx] = float(r.get("relevance_score", 0.0))

            return scores

        except requests.exceptions.HTTPError as e:
            if e.response is not None and 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                logger.error("OpenRouter permanent HTTP error %d: %s", e.response.status_code, e)
                raise
            if attempt == max_retries - 1:
                raise
            sleep_time = (base_backoff * (2**attempt)) + random.uniform(0, 0.5)
            time.sleep(sleep_time)
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                logger.error("OpenRouter request failed after %d attempts: %s", max_retries, e)
                raise
            sleep_time = (base_backoff * (2**attempt)) + random.uniform(0, 0.5)
            time.sleep(sleep_time)

    raise RuntimeError("OpenRouter reranker failed (max retries exceeded)")


def _term_coverage_fallback(query: str, documents: list[str]) -> list[float]:
    """Deterministic lexical query-term coverage fallback.
    
    Used when OpenRouter is unavailable. Computes fraction of meaningful
    query terms present in each document text.
    """
    _STOPWORDS = {
        "what", "is", "the", "a", "an", "in", "on", "at", "to", "for",
        "of", "and", "or", "with", "by", "from", "as", "are", "how",
        "why", "which", "who", "where", "when", "does", "do", "did",
    }
    raw_query_words = set(query.lower().split())
    query_words = (
        {w for w in raw_query_words if w not in _STOPWORDS and len(w) > 2}
        or raw_query_words
    )
    if not query_words:
        return [0.0] * len(documents)

    scores = []
    for doc in documents:
        doc_words = set(doc.lower().split())
        if not doc_words:
            scores.append(0.0)
            continue
        intersection = len(query_words.intersection(doc_words))
        coverage = float(intersection) / float(len(query_words))
        scores.append(coverage)
    return scores


def rerank_documents(
    query: str, documents: list[str], provider: str | None = None
) -> list[float]:
    """Score a list of document strings against a query.

    Primary:  OpenRouter Rerank API (nvidia/llama-nemotron-rerank-vl-1b-v2:free)
    Fallback: Lexical term coverage (deterministic, no API call)

    Returns a list of relevance scores (floats) aligned with the input
    documents list. Higher = more relevant.
    """
    if not documents:
        return []

    t0 = time.perf_counter()
    try:
        scores = _openrouter_reranker(query, documents)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(
            "reranker.openrouter.latency_ms=%.2f doc_count=%d avg_score=%.4f",
            latency_ms,
            len(documents),
            sum(scores) / max(1, len(scores)),
        )
        return scores
    except Exception as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.warning(
            "reranker.openrouter.failed latency_ms=%.2f error=%s — falling back to lexical coverage",
            latency_ms,
            str(e),
        )

    # Deterministic fallback
    logger.warning("reranker.mode=lexical_fallback reason=openrouter_failed")
    return _term_coverage_fallback(query, documents)
