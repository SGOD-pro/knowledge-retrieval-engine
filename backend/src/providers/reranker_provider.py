"""Reranker provider — API-based document reranking.

Model Provider Matrix:
  - Primary:  OpenRouter Rerank API (nvidia/llama-nemotron-rerank-vl-1b-v2:free)
  - Fallback: BM25Plus lexical scoring (deterministic, no API call)

NVIDIA NIM direct and Bedrock Cohere are fully removed (NIM returned 410 Gone).
This is the accepted architectural decision recorded in BOUNDARIES.md.

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

_reranker_counter = {"reranker_calls": 0}


def reset_reranker_counter() -> None:
    _reranker_counter["reranker_calls"] = 0


def get_reranker_counter() -> dict:
    return dict(_reranker_counter)

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
_circuit_breaker_half_open_interval = 300.0  # Probe request allowed every 5 minutes
_circuit_breaker_last_probe = 0.0
_circuit_breaker_lock = threading.Lock()


def _is_circuit_open() -> bool:
    global _circuit_breaker_tripped, _circuit_breaker_reset_time, _circuit_breaker_last_probe
    with _circuit_breaker_lock:
        if _circuit_breaker_tripped:
            now = time.time()
            if now > _circuit_breaker_reset_time:
                _circuit_breaker_tripped = False
                logger.info("reranker.circuit_breaker_reset — full cooldown elapsed")
                return False
            # Half-open: allow one probe request every 5 minutes
            if now - _circuit_breaker_last_probe >= _circuit_breaker_half_open_interval:
                _circuit_breaker_last_probe = now
                logger.info("reranker.circuit_breaker_half_open — allowing probe request")
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
            try:
                from services.telemetry import record_reranker
                record_reranker()
            except Exception:
                pass
            _reranker_counter["reranker_calls"] += 1
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
    from services.retrieval.bm25_retriever import _tokenize

    if not documents:
        return []

    q_tokens = _tokenize(query)
    if not q_tokens:
        return [0.0] * len(documents)

    doc_corpus = [_tokenize(doc) for doc in documents]
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

    # Phrase and entity matching bonus: boosts documents with exact multi-word phrases and key entities
    clean_words = [w for w in re.findall(r"\b[a-z0-9]+\b", query.lower()) if len(w) > 1]
    phrase_stopwords = {
        "what", "is", "are", "was", "were", "the", "in", "of", "to", "for", "from",
        "by", "with", "and", "or", "a", "an", "at", "as", "on", "that", "this",
        "which", "how", "much", "many", "round", "two", "decimals", "calculate",
        "compute", "find", "does", "did", "their", "there", "show", "shows",
        "came", "into", "been", "being", "have", "has", "had", "will", "would",
    }
    phrases: list[tuple[str, float]] = []
    for i in range(len(clean_words) - 1):
        w1, w2 = clean_words[i], clean_words[i + 1]
        if w1 not in phrase_stopwords and w2 not in phrase_stopwords:
            phrases.append((f"{w1} {w2}", 0.10))
        if i + 2 < len(clean_words):
            w3 = clean_words[i + 2]
            non_stop = sum(1 for w in (w1, w2, w3) if w not in phrase_stopwords)
            if non_stop >= 2:
                phrases.append((f"{w1} {w2} {w3}", 0.15))

    # Entity tokens: capitalized words in query indicating named entities or proper nouns
    entity_tokens = [w.lower() for w in re.findall(r"\b[A-Z][a-z0-9_]{2,}\b", query) if w.lower() not in phrase_stopwords]

    if phrases or entity_tokens:
        boosted_scores = []
        for doc, score in zip(documents, scores):
            d_lower = doc.lower()
            bonus = min(0.35, sum(weight for phrase, weight in phrases if phrase in d_lower))
            if entity_tokens:
                ent_hits = sum(1 for ent in entity_tokens if ent in d_lower)
                bonus += min(0.20, ent_hits * 0.10)
            boosted_scores.append(round(min(1.0, score + bonus), 4))
        scores = boosted_scores

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
            with _circuit_breaker_lock:
                global _circuit_breaker_tripped
                if _circuit_breaker_tripped:
                    _circuit_breaker_tripped = False
                    logger.info("reranker.circuit_breaker_closed — probe succeeded, service recovered")
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
