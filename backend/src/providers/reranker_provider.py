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


# Circuit breaker state
_circuit_breaker_tripped = False
_circuit_breaker_reset_time = 0.0
_circuit_breaker_lock = threading.Lock()


def _is_circuit_open() -> bool:
    global _circuit_breaker_tripped, _circuit_breaker_reset_time
    with _circuit_breaker_lock:
        if _circuit_breaker_tripped:
            if time.time() > _circuit_breaker_reset_time:
                _circuit_breaker_tripped = False
                return False
            return True
        return False


def _trip_circuit(cooldown_seconds: float = 3600.0):
    global _circuit_breaker_tripped, _circuit_breaker_reset_time
    with _circuit_breaker_lock:
        _circuit_breaker_tripped = True
        _circuit_breaker_reset_time = time.time() + cooldown_seconds
        logger.warning(
            "reranker.circuit_breaker_tripped for %.0fs — routing directly to lexical fallback",
            cooldown_seconds,
        )


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
    """
    if _is_circuit_open():
        raise RuntimeError("OpenRouter circuit breaker is OPEN (quota exhausted)")

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
    max_retries = 2
    base_backoff = 0.5

    for attempt in range(max_retries):
        _wait_for_rate_limit()
        try:
            response = session.post(
                _OPENROUTER_RERANK_URL,
                headers=headers,
                json=payload,
                timeout=10,
            )

            # Check for rate limit / daily quota exhaustion
            if response.status_code == 429:
                resp_text = response.text.lower()
                if "free-models-per-day" in resp_text or "daily" in resp_text:
                    # Daily quota exhausted — trip circuit breaker immediately, do NOT retry
                    _trip_circuit(cooldown_seconds=3600.0)
                    raise RuntimeError("OpenRouter daily free-model limit exhausted (429)")

                if attempt == max_retries - 1:
                    response.raise_for_status()
                sleep_time = (base_backoff * (2**attempt)) + random.uniform(0, 0.2)
                logger.warning(
                    "OpenRouter reranker returned 429. Retrying in %.2fs (attempt %d/%d)",
                    sleep_time,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(sleep_time)
                continue

            if response.status_code >= 500:
                if attempt == max_retries - 1:
                    response.raise_for_status()
                time.sleep(base_backoff * (2**attempt))
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
            time.sleep(base_backoff * (2**attempt))
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                logger.error("OpenRouter request failed after %d attempts: %s", max_retries, e)
                raise
            time.sleep(base_backoff * (2**attempt))

    raise RuntimeError("OpenRouter reranker failed (max retries exceeded)")


def _term_coverage_fallback(query: str, documents: list[str]) -> list[float]:
    """Deterministic BM25Plus lexical scoring fallback over candidate documents.

    Used when OpenRouter is unavailable. Computes BM25Plus relevance over
    the candidate document pool with clean regex word tokenization, preserving
    rare keyword matches (e.g. variable codes, model names, specific years).
    """
    import re
    from rank_bm25 import BM25Plus

    if not documents:
        return []

    q_tokens = re.findall(r"\w+", query.lower())
    if not q_tokens:
        return [0.0] * len(documents)

    doc_corpus = [re.findall(r"\w+", doc.lower()) for doc in documents]
    try:
        bm25_local = BM25Plus(doc_corpus)
        raw_scores = bm25_local.get_scores(q_tokens)
        max_s = max(raw_scores) if len(raw_scores) > 0 else 0.0
        if max_s > 0:
            # Normalize to [0.05, 0.95] range
            scores = [round(float(s) / max_s * 0.9 + 0.05, 4) for s in raw_scores]
        else:
            scores = [0.1] * len(documents)
        return scores
    except Exception as e:
        logger.warning("term_coverage_fallback BM25Plus failed: %s — using token overlap", e)

    # Fallback to token overlap if BM25Plus errors
    q_set = set(q_tokens)
    scores = []
    for doc_toks in doc_corpus:
        doc_set = set(doc_toks)
        overlap = len(q_set & doc_set) / max(1, len(q_set))
        scores.append(round(overlap, 4))
    return scores


def rerank_documents(
    query: str, documents: list[str], provider: str | None = None
) -> list[float]:
    """Score a list of document strings against a query.

    Primary:  OpenRouter Rerank API (nvidia/llama-nemotron-rerank-vl-1b-v2:free)
    Fallback: BM25Plus lexical scoring (deterministic, zero API call)

    Returns a list of relevance scores (floats) aligned with the input
    documents list. Higher = more relevant.
    """
    if not documents:
        return []

    t0 = time.perf_counter()
    if not _is_circuit_open():
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
                "reranker.openrouter.failed latency_ms=%.2f error=%s — falling back to lexical BM25Plus",
                latency_ms,
                str(e),
            )

    # Deterministic fallback (zero API call, immediate p50 < 2ms)
    logger.info("reranker.mode=lexical_bm25_fallback")
    return _term_coverage_fallback(query, documents)
