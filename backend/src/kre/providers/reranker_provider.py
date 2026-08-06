"""Reranker provider — API-based document reranking.

Model Provider Matrix (ARCHITECTURE.md rev 6):
  - Prod: nvidia/llama-nemotron-rerank-1b-v2 (NVIDIA NIM)
  - Dev:  Same as Prod

Rule 6: Reranker runs before compression. Always.
Rule 28: All reranker calls route through this module.
"""

import json
import logging
import os
import time
import math
import random
import threading
import requests

from kre.providers.provider_client import get_active_provider

logger = logging.getLogger(__name__)

# Basic token bucket / rate limiter state
_rl_lock = threading.Lock()
_last_request_time = 0.0
REQUEST_INTERVAL_SECONDS = 0.5  # Max 2 requests per second globally

def _wait_for_rate_limit():
    global _last_request_time
    with _rl_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < REQUEST_INTERVAL_SECONDS:
            time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)
        _last_request_time = time.monotonic()

def _sigmoid(x: float) -> float:
    try:
        if x >= 0:
            z = math.exp(-x)
            return 1 / (1 + z)
        else:
            z = math.exp(x)
            return z / (1 + z)
    except OverflowError:
        return 1.0 if x > 0 else 0.0

def nvidia_nim_reranker(query: str, documents: list[str]) -> list[float]:
    invoke_url = "https://ai.api.nvidia.com/v1/retrieval/nvidia/llama-nemotron-rerank-1b-v2/reranking"
    api_key = os.environ.get("NVIDIA_API_KEY", "")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    
    payload = {
        "model": "nvidia/llama-nemotron-rerank-1b-v2",
        "query": {"text": query},
        "passages": [{"text": doc} for doc in documents]
    }
    
    session = requests.Session()
    max_retries = 5
    base_backoff = 1.0
    
    for attempt in range(max_retries):
        _wait_for_rate_limit()
        try:
            response = session.post(invoke_url, headers=headers, json=payload, timeout=15)
            
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == max_retries - 1:
                    response.raise_for_status()
                # Exponential backoff with jitter
                sleep_time = (base_backoff * (2 ** attempt)) + random.uniform(0, 1)
                logger.warning(f"Reranker API returned {response.status_code}. Retrying in {sleep_time:.2f}s (Attempt {attempt+1}/{max_retries})")
                time.sleep(sleep_time)
                continue
                
            response.raise_for_status()
            
            response_body = response.json()
            rankings = response_body.get("rankings", [])
            
            scores = [0.0] * len(documents)
            for r in rankings:
                idx = r.get("index")
                if idx is not None and idx < len(documents):
                    logit = r.get("logit", 0.0)
                    scores[idx] = _sigmoid(logit)
                    
            return scores
            
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                logger.error(f"Reranker failed after {max_retries} attempts: {str(e)}")
                raise
            sleep_time = (base_backoff * (2 ** attempt)) + random.uniform(0, 1)
            logger.warning(f"Reranker request exception: {str(e)}. Retrying in {sleep_time:.2f}s")
            time.sleep(sleep_time)
    
    raise RuntimeError("Reranker failed (max retries exceeded)")

def rerank_documents(query: str, documents: list[str], provider: str | None = None) -> list[float]:
    """Score a list of document strings against a query using NVIDIA NIM reranker.

    Returns a list of relevance scores (floats) aligned with the input documents list.
    """
    if not documents:
        return []
        
    try:
        return nvidia_nim_reranker(query, documents)
    except Exception as e:
        logger.error("Reranker failed: %s", str(e))

    # Deterministic fallback — Jaccard word overlap scoring
    scores = []
    query_words = set(query.lower().split())
    if not query_words:
        return [0.0] * len(documents)

    for doc in documents:
        doc_words = set(doc.lower().split())
        if not doc_words:
            scores.append(0.0)
            continue
        intersection = len(query_words.intersection(doc_words))
        union = len(query_words.union(doc_words))
        scores.append(float(intersection) / float(union))

    return scores
