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

import concurrent.futures
import contextvars
import json
import logging
import math
import os
import random
import threading
import time
from typing import Any
import boto3
import botocore.config
import requests

from config import settings
from schemas.models import RerankerMode

logger = logging.getLogger(__name__)

# OpenRouter Reranker endpoint
_OPENROUTER_RERANK_URL = "https://openrouter.ai/api/v1/rerank"

# Rate limiter / token bucket state
_rl_lock = threading.Lock()
_last_request_time = 0.0
REQUEST_INTERVAL_SECONDS = 0.2  # Max 5 requests per second

_reranker_counter = {"reranker_calls": 0}
_current_reranker_mode: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_reranker_mode", default=RerankerMode.BYPASSED.value
)
_current_reranker_telemetry: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "current_reranker_telemetry", default={}
)


def reset_reranker_counter() -> None:
    _reranker_counter["reranker_calls"] = 0


def get_reranker_counter() -> dict:
    return dict(_reranker_counter)


def get_last_reranker_mode() -> str:
    return _current_reranker_mode.get()


def set_last_reranker_mode(mode: str) -> None:
    _current_reranker_mode.set(mode)


def get_last_reranker_telemetry() -> dict[str, Any]:
    return dict(_current_reranker_telemetry.get())


def set_last_reranker_telemetry(data: dict[str, Any]) -> None:
    _current_reranker_telemetry.set(data)

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


_bedrock_client: Any = None
_bedrock_client_lock = threading.Lock()


def reset_bedrock_rerank_client() -> None:
    """Clear the cached Bedrock reranker client (for isolated tests and credential refresh)."""
    global _bedrock_client
    with _bedrock_client_lock:
        _bedrock_client = None


def _resolve_aws_profile() -> str | None:
    """Align local profile routing with aws.infra while honoring explicit AWS_PROFILE."""
    explicit = os.environ.get("AWS_PROFILE") or getattr(settings, "AWS_PROFILE", None)
    if explicit:
        return explicit
    env = getattr(settings, "ENVIRONMENT", "dev")
    if env != "prod":
        return "aws"
    return None


def _get_bedrock_rerank_client() -> Any:
    """Return dedicated, reused Bedrock Agent Runtime client targeting us-west-2 with total_max_attempts=1."""
    global _bedrock_client
    with _bedrock_client_lock:
        if _bedrock_client is None:
            region = getattr(settings, "BEDROCK_RERANKER_REGION", "us-west-2")
            cfg = botocore.config.Config(
                retries={"total_max_attempts": 1, "mode": "standard"},
                connect_timeout=1.0,
                read_timeout=1.5,
            )
            profile_name = _resolve_aws_profile()
            try:
                if profile_name:
                    session = boto3.Session(profile_name=profile_name)
                else:
                    session = boto3.Session()
                _bedrock_client = session.client("bedrock-agent-runtime", region_name=region, config=cfg)
            except Exception:
                # Preserve normal default credential chain if local profile lookup is unavailable
                if os.environ.get("AWS_PROFILE") or getattr(settings, "AWS_PROFILE", None):
                    raise
                _bedrock_client = boto3.Session().client(
                    "bedrock-agent-runtime", region_name=region, config=cfg
                )
        return _bedrock_client


def verify_bedrock_reranker_preflight() -> dict[str, Any]:
    """Fail fast in paired-test preflight if retries != 1, region mismatches, or credentials are absent."""
    client = _get_bedrock_rerank_client()
    expected_region = getattr(settings, "BEDROCK_RERANKER_REGION", "us-west-2")
    actual_region = getattr(getattr(client, "meta", None), "region_name", None)
    if actual_region != expected_region:
        raise RuntimeError(
            f"Bedrock reranker preflight failed: region mismatch ({actual_region} != {expected_region})"
        )
    retries_cfg = getattr(getattr(getattr(client, "meta", None), "config", None), "retries", None) or {}
    if retries_cfg.get("total_max_attempts") != 1:
        raise RuntimeError(
            f"Bedrock reranker preflight failed: expected total_max_attempts=1, got {retries_cfg}"
        )
    creds = client._get_credentials() if hasattr(client, "_get_credentials") else None
    if creds is None or not getattr(creds, "access_key", None):
        raise RuntimeError(
            "Bedrock reranker preflight failed: missing AWS credentials for Bedrock Agent Runtime"
        )
    return {
        "provider": "bedrock",
        "model": getattr(settings, "BEDROCK_RERANKER_MODEL", "amazon.rerank-v1:0"),
        "region": actual_region,
        "total_max_attempts": retries_cfg["total_max_attempts"],
        "credentials_present": True,
    }


class BedrockRerankController:
    """Bounded concurrency and caller budget controller for Bedrock Agent Runtime rerank.

    Enforces:
      - 1 in-flight request at a time
      - 1.5s default caller budget
      - Capacity retention: caller timeout does not release in-flight slot while task runs
      - Scoring contract: rejects duplicate, missing, out-of-range, and NaN indices
      - Remote candidate preservation: numberOfResults == len(documents)
    """

    def __init__(self, max_concurrency: int = 1, caller_budget_s: float = 1.5):
        self.max_concurrency = max_concurrency
        self.caller_budget_s = caller_budget_s
        self._semaphore = threading.Semaphore(max_concurrency)
        self._active_workers = 0
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix="bedrock_rerank_worker",
        )

    @property
    def active_workers(self) -> int:
        with self._lock:
            return self._active_workers

    def is_slot_free(self) -> bool:
        with self._lock:
            return self._active_workers < self.max_concurrency

    def run_bedrock_rerank(
        self,
        query: str,
        documents: list[str],
        timeout_s: float | None = None,
    ) -> tuple[list[float] | None, str | None, float]:
        """Execute Bedrock rerank with capacity retention and caller timeout enforcement.

        Returns (scores, fallback_reason, latency_ms).
        """
        if not documents:
            return [], None, 0.0

        budget_s = timeout_s if timeout_s is not None else self.caller_budget_s
        t0 = time.perf_counter()

        acquired = self._semaphore.acquire(timeout=budget_s)
        if not acquired:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            logger.warning(
                "reranker.bedrock.concurrency_limit_timeout budget_s=%.2f",
                budget_s,
            )
            return None, "concurrency_limit_timeout", elapsed_ms

        with self._lock:
            self._active_workers += 1

        elapsed = time.perf_counter() - t0
        remaining_s = max(0.001, budget_s - elapsed)

        outcome: dict[str, Any] = {
            "scores": None,
            "error": None,
            "status": "running",
        }
        call_done = threading.Event()

        def _worker_task() -> None:
            try:
                client = _get_bedrock_rerank_client()
                model = getattr(settings, "BEDROCK_RERANKER_MODEL", "amazon.rerank-v1:0")
                region = getattr(settings, "BEDROCK_RERANKER_REGION", "us-west-2")
                model_arn = f"arn:aws:bedrock:{region}::foundation-model/{model}"

                sources = [
                    {
                        "type": "INLINE",
                        "inlineDocumentSource": {
                            "type": "TEXT",
                            "textDocument": {"text": doc},
                        },
                    }
                    for doc in documents
                ]

                # Preserve candidate identity and prevent remote top-k truncation
                payload = {
                    "queries": [{"type": "TEXT", "textQuery": {"text": query}}],
                    "sources": sources,
                    "rerankingConfiguration": {
                        "type": "BEDROCK_RERANKING_MODEL",
                        "bedrockRerankingConfiguration": {
                            "numberOfResults": len(documents),
                            "modelConfiguration": {"modelArn": model_arn},
                        },
                    },
                }

                try:
                    from services.telemetry import record_reranker
                    record_reranker()
                except Exception:
                    pass
                _reranker_counter["reranker_calls"] += 1

                resp = client.rerank(**payload)

                raw_results = resp.get("results")
                if raw_results is None or not isinstance(raw_results, list):
                    raise ValueError("Bedrock response missing 'results' list")

                n = len(documents)
                if len(raw_results) != n:
                    raise ValueError(f"Bedrock returned {len(raw_results)} results, expected {n} (missing indices)")

                scores = [0.0] * n
                seen_indices = set()
                for item in raw_results:
                    if not isinstance(item, dict):
                        raise ValueError("Malformed item in Bedrock results list")
                    idx = item.get("index")
                    if not isinstance(idx, int):
                        raise ValueError(f"Malformed non-integer index: {idx}")
                    if idx < 0 or idx >= n:
                        raise ValueError(f"Index {idx} out of range [0, {n - 1}]")
                    if idx in seen_indices:
                        raise ValueError(f"Duplicate index {idx} in Bedrock results")
                    raw_score = item.get("relevanceScore")
                    if raw_score is None or not isinstance(raw_score, (int, float)) or math.isnan(raw_score):
                        raise ValueError(f"Malformed relevanceScore for index {idx}: {raw_score}")

                    scores[idx] = float(raw_score)
                    seen_indices.add(idx)

                if len(seen_indices) != n:
                    raise ValueError(f"Missing indices in Bedrock results: {set(range(n)) - seen_indices}")

                outcome["scores"] = scores
                outcome["status"] = "completed"

            except Exception as exc:
                outcome["error"] = exc
                outcome["status"] = "failed"
            finally:
                with self._lock:
                    self._active_workers = max(0, self._active_workers - 1)
                self._semaphore.release()
                call_done.set()

        self._executor.submit(_worker_task)

        finished = call_done.wait(timeout=remaining_s)
        total_ms = (time.perf_counter() - t0) * 1000.0

        if not finished:
            logger.warning(
                "reranker.bedrock.caller_timeout budget_s=%.2f worker_slot_retained=True",
                budget_s,
            )
            return None, "caller_timeout", total_ms

        if outcome["status"] == "completed":
            return outcome["scores"], None, total_ms

        err = outcome["error"]
        err_str = str(err)
        err_code = ""
        if hasattr(err, "response") and isinstance(err.response, dict):
            err_code = err.response.get("Error", {}).get("Code", "")
        if "throttl" in err_str.lower() or "throttl" in err_code.lower() or "rate" in err_str.lower():
            reason = f"throttled: {err_str}"
        elif "duplicate" in err_str.lower():
            reason = f"duplicate_index: {err_str}"
        elif "missing" in err_str.lower():
            reason = f"missing_indices: {err_str}"
        elif "out of range" in err_str.lower():
            reason = f"out_of_range_index: {err_str}"
        else:
            reason = f"error: {err_str}"

        logger.warning("reranker.bedrock.failed reason=%s", reason)
        return None, reason, total_ms


_bedrock_rerank_controller = BedrockRerankController()


def reset_bedrock_rerank_controller() -> None:
    """Reset the Bedrock rerank controller worker count and semaphore state."""
    global _bedrock_rerank_controller
    with _bedrock_rerank_controller._lock:
        _bedrock_rerank_controller._active_workers = 0
    _bedrock_rerank_controller._semaphore = threading.Semaphore(
        _bedrock_rerank_controller.max_concurrency
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


def resolve_reranker_config(provider: str | None = None) -> dict[str, Any]:
    """Resolve active reranker provider, model, and region without mutating global settings."""
    if provider == "bedrock":
        active_provider = "bedrock"
    elif provider in ("lexical", "bm25", "bm25plus"):
        active_provider = "lexical"
    elif provider == "openrouter":
        active_provider = "openrouter"
    else:
        from providers.bedrock_models import is_bedrock_reranker_enabled

        if is_bedrock_reranker_enabled():
            active_provider = "bedrock"
        elif (
            os.environ.get("RERANKER_PROVIDER") in ("lexical", "bm25", "bm25plus")
            or getattr(settings, "RERANKER_PROVIDER", "") in ("lexical", "bm25", "bm25plus")
        ):
            active_provider = "lexical"
        else:
            active_provider = "openrouter"

    if active_provider == "bedrock":
        return {
            "provider": "bedrock",
            "model": getattr(settings, "BEDROCK_RERANKER_MODEL", "amazon.rerank-v1:0"),
            "region": getattr(settings, "BEDROCK_RERANKER_REGION", "us-west-2"),
        }
    if active_provider == "lexical":
        return {"provider": "lexical", "model": "bm25plus", "region": None}
    return {
        "provider": "openrouter",
        "model": getattr(
            settings,
            "OPENROUTER_RERANKER_MODEL",
            "nvidia/llama-nemotron-rerank-vl-1b-v2:free",
        ),
        "region": None,
    }


def get_bypassed_reranker_telemetry(
    provider: str | None = None,
    mode: str = RerankerMode.BYPASSED.value,
    candidate_count: int = 0,
) -> dict[str, Any]:
    """Return a fresh bypassed telemetry snapshot carrying configured provider metadata."""
    cfg = resolve_reranker_config(provider)
    return {
        "provider": cfg["provider"],
        "model": cfg["model"],
        "region": cfg["region"],
        "effective_mode": mode,
        "latency_ms": 0.0,
        "candidate_count": candidate_count,
        "fallback_reason": None,
    }


def rerank_documents(
    query: str,
    documents: list[str],
    provider: str | None = None,
    return_telemetry: bool = False,
) -> list[float] | tuple[list[float], dict[str, Any]]:
    """Score a list of document strings against a query.

    Model options:
      - Bedrock (opt-in): Amazon Rerank 1.0 (amazon.rerank-v1:0 in us-west-2)
      - OpenRouter: nvidia/llama-nemotron-rerank-vl-1b-v2:free
      - Lexical: BM25Plus deterministic scoring (fallback and default experiment mode)

    Returns relevance scores (or (scores, telemetry) when return_telemetry=True).
    """
    cfg = resolve_reranker_config(provider)
    if not documents:
        telemetry = get_bypassed_reranker_telemetry(
            provider=provider,
            mode=RerankerMode.BYPASSED.value,
            candidate_count=0,
        )
        set_last_reranker_mode(RerankerMode.BYPASSED.value)
        set_last_reranker_telemetry(dict(telemetry))
        return ([], telemetry) if return_telemetry else []

    active_provider = cfg["provider"]

    if active_provider == "bedrock":
        scores, fallback_reason, latency_ms = _bedrock_rerank_controller.run_bedrock_rerank(
            query, documents
        )
        if scores is not None:
            telemetry = {
                "provider": cfg["provider"],
                "model": cfg["model"],
                "region": cfg["region"],
                "effective_mode": RerankerMode.REMOTE_SUCCESS.value,
                "latency_ms": round(latency_ms, 2),
                "candidate_count": len(documents),
                "fallback_reason": None,
            }
            set_last_reranker_mode(RerankerMode.REMOTE_SUCCESS.value)
            set_last_reranker_telemetry(dict(telemetry))
            logger.info(
                "reranker.bedrock.latency_ms=%.2f candidate_count=%d avg_score=%.4f",
                latency_ms,
                len(documents),
                sum(scores) / max(1, len(scores)),
            )
            return (scores, telemetry) if return_telemetry else scores

        # Bedrock failed: fallback directly to BM25Plus, DO NOT chain to OpenRouter
        t_fb = time.perf_counter()
        fb_scores = _term_coverage_fallback(query, documents)
        total_ms = latency_ms + ((time.perf_counter() - t_fb) * 1000.0)
        fallback_mode = RerankerMode.CIRCUIT_OPEN_LEXICAL_FALLBACK.value
        telemetry = {
            "provider": cfg["provider"],
            "model": cfg["model"],
            "region": cfg["region"],
            "effective_mode": fallback_mode,
            "latency_ms": round(total_ms, 2),
            "candidate_count": len(documents),
            "fallback_reason": fallback_reason,
        }
        set_last_reranker_mode(fallback_mode)
        set_last_reranker_telemetry(dict(telemetry))
        logger.warning(
            "reranker.bedrock_fallback_to_lexical reason=%s latency_ms=%.2f candidate_count=%d",
            fallback_reason,
            total_ms,
            len(documents),
        )
        return (fb_scores, telemetry) if return_telemetry else fb_scores

    if active_provider == "lexical":
        t0 = time.perf_counter()
        scores = _term_coverage_fallback(query, documents)
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        # Intentional lexical mode by configuration, distinct from upstream error fallback
        fallback_mode = RerankerMode.CIRCUIT_OPEN_LEXICAL_FALLBACK.value
        telemetry = {
            "provider": "lexical",
            "model": "bm25plus",
            "region": None,
            "effective_mode": fallback_mode,
            "latency_ms": latency_ms,
            "candidate_count": len(documents),
            "fallback_reason": "lexical_by_configuration",
        }
        set_last_reranker_mode(fallback_mode)
        set_last_reranker_telemetry(dict(telemetry))
        return (scores, telemetry) if return_telemetry else scores

    # Legacy OpenRouter path
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
            telemetry = {
                "provider": cfg["provider"],
                "model": cfg["model"],
                "region": None,
                "effective_mode": RerankerMode.REMOTE_SUCCESS.value,
                "latency_ms": round(latency_ms, 2),
                "candidate_count": len(documents),
                "fallback_reason": None,
            }
            set_last_reranker_mode(RerankerMode.REMOTE_SUCCESS.value)
            set_last_reranker_telemetry(dict(telemetry))
            logger.info(
                "reranker.openrouter.latency_ms=%.2f doc_count=%d avg_score=%.4f",
                latency_ms,
                len(documents),
                sum(scores) / max(1, len(scores)),
            )
            return (scores, telemetry) if return_telemetry else scores
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            logger.warning(
                "reranker.openrouter.failed latency_ms=%.2f error=%s — falling back to lexical BM25Plus",
                latency_ms,
                str(e),
            )

    # Deterministic fallback (zero API call, immediate p50 < 2ms)
    scores = _term_coverage_fallback(query, documents)
    fallback_mode = RerankerMode.CIRCUIT_OPEN_LEXICAL_FALLBACK.value
    telemetry = {
        "provider": cfg["provider"],
        "model": cfg["model"],
        "region": None,
        "effective_mode": fallback_mode,
        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "candidate_count": len(documents),
        "fallback_reason": "openrouter_unavailable",
    }
    set_last_reranker_mode(fallback_mode)
    set_last_reranker_telemetry(dict(telemetry))
    logger.info("reranker.mode=%s", fallback_mode)
    return (scores, telemetry) if return_telemetry else scores
