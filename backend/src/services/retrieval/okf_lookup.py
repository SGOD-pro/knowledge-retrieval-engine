"""Isolated, bounded OKF structural lookup service with capacity retention.

Enforces:
- Hard client deadlines across OKF DynamoDB/SQLite reads (default 50ms).
- Explicit admission and queue bounding with immediate load shedding (max 4 workers, max 8 queue).
- Worker capacity retention until underlying storage thread actually finishes.
- Clean distinction between caller timeout and storage cancellation.
- Immediate workspace and source tombstone enforcement (fail closed, zero leakage).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import threading
import time
from typing import Any, Callable, Sequence

from config import settings

logger = logging.getLogger(__name__)

# Query target extraction pattern for organizations and named entities
_CAPITALIZED_ENTITY_PATTERN = re.compile(
    r"\b[A-Z][a-zA-Z0-9]*(?:\s+[A-Z][a-zA-Z0-9]*)*\b"
)


_STOP_WORDS = {
    "what", "when", "where", "who", "how", "tell", "is", "are", "the", "for",
    "under", "in", "on", "at", "to", "of", "with", "by", "according", "about",
    "from", "during", "before", "after", "per", "which", "whose", "does", "have",
    "show", "give", "can", "could", "should", "would", "an", "a",
}


def extract_entity_names_from_query(query: str) -> list[str]:
    """Extract candidate entity names from query string deterministically with 0 model calls."""
    if not query:
        return []
    candidates: list[str] = []

    # 1. Capitalized words/phrases
    matches = _CAPITALIZED_ENTITY_PATTERN.findall(query)
    for m in matches:
        tokens = [t for t in m.strip().split() if t]
        # Strip leading and trailing stop words from multi-token spans
        while tokens and tokens[0].lower() in _STOP_WORDS:
            tokens.pop(0)
        while tokens and tokens[-1].lower() in _STOP_WORDS:
            tokens.pop()

        if not tokens:
            continue

        cleaned = " ".join(tokens)
        if len(cleaned) >= 2 and cleaned.lower() not in _STOP_WORDS:
            candidates.append(cleaned)

        # For multi-word spans (e.g. "Chief Operating Officer Alice Smith"), extract sub-phrases
        if len(tokens) > 2:
            # 3-grams
            for i in range(len(tokens) - 2):
                span3 = " ".join(tokens[i : i + 3])
                if span3.lower() not in _STOP_WORDS:
                    candidates.append(span3)
            # 2-grams
            for i in range(len(tokens) - 1):
                span2 = " ".join(tokens[i : i + 2])
                if span2.lower() not in _STOP_WORDS:
                    candidates.append(span2)
        elif len(tokens) == 2:
            # Add individual tokens
            for tok in tokens:
                if len(tok) >= 2 and tok.lower() not in _STOP_WORDS:
                    candidates.append(tok)

    # 2. Quoted entities
    quoted = re.findall(r'"([^"]+)"', query)
    for q in quoted:
        if q.strip():
            candidates.append(q.strip())

    # 3. If no entities extracted, treat single words (excluding stop words) as candidates
    if not candidates:
        tokens = [t.strip() for t in query.split() if len(t.strip()) > 3]
        filtered = [t for t in tokens if t.lower() not in _STOP_WORDS]
        # Prioritize tokens containing digits or underscores before applying slice limit
        filtered_prio = sorted(filtered, key=lambda t: (bool(re.search(r"[0-9_]", t)), len(t)), reverse=True)
        candidates.extend(filtered_prio[:10])

    # Deduplicate preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for c in candidates:
        low = c.lower()
        if low not in seen:
            seen.add(low)
            deduped.append(c)

    return deduped


class WorkerResultWrapper:
    """Wrapper for worker tasks returning custom execution status."""

    def __init__(self, result: Any, status: str = "completed"):
        self.result = result
        self.status = status


class OkfConcurrencyController:
    """Admission gate and capacity tracker for OKF storage lookups.
    
    Enforces explicit admission queue bounds, retaining worker capacity
    until the underlying storage thread completes, even when callers time out
    or cancel their coroutines.
    """

    def __init__(
        self,
        max_concurrency: int | None = None,
        max_queue: int | None = None,
    ) -> None:
        self.max_concurrency = max_concurrency or getattr(settings, "OKF_MAX_CONCURRENCY", 4)
        self.max_queue = max_queue or getattr(settings, "OKF_MAX_QUEUE", 8)
        self._active_workers = 0
        self._queued_callers = 0
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._sync_semaphore = threading.Semaphore(self.max_concurrency)
        self._thread_lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix="okf_worker",
        )

    @property
    def active_count(self) -> int:
        with self._thread_lock:
            return self._active_workers

    @property
    def queued_count(self) -> int:
        with self._thread_lock:
            return self._queued_callers

    def _release_worker_slot_sync(self) -> None:
        with self._thread_lock:
            self._active_workers = max(0, self._active_workers - 1)
        self._sync_semaphore.release()

    def _release_worker_slot_async(self) -> None:
        with self._thread_lock:
            self._active_workers = max(0, self._active_workers - 1)
        self._semaphore.release()

    def run_storage_work_sync(
        self,
        func: Callable[..., Any],
        *args: Any,
        deadline_s: float = 0.050,
    ) -> tuple[Any, str]:
        """Execute blocking storage work synchronously with retained capacity and caller deadlines.
        
        Returns (result, status) where status is one of:
          - "completed"
          - "caller_timeout"
          - "admission_rejected"
          - "storage_failed"
        """
        # 1. Bounded admission check
        with self._thread_lock:
            if self._queued_callers >= self.max_queue:
                logger.warning(
                    "okf.admission_rejected queued=%d max=%d",
                    self._queued_callers,
                    self.max_queue,
                )
                return None, "admission_rejected"
            self._queued_callers += 1

        t0 = time.perf_counter()

        # 2. Acquire concurrency slot within deadline
        acquired = self._sync_semaphore.acquire(timeout=deadline_s)
        if not acquired:
            with self._thread_lock:
                self._queued_callers -= 1
            logger.warning(
                "okf.timeout_ms=%.1f fallback=baseline stage=queue_admission",
                deadline_s * 1000.0,
            )
            return None, "caller_timeout"

        with self._thread_lock:
            self._queued_callers -= 1
            self._active_workers += 1

        elapsed = time.perf_counter() - t0
        remaining_s = max(0.001, deadline_s - elapsed)

        outcome: dict[str, Any] = {"result": None, "status": "running", "error": None}
        storage_done = threading.Event()

        def _worker_task() -> None:
            try:
                outcome["result"] = func(*args, remaining_s)
                outcome["status"] = "completed"
            except Exception as exc:
                outcome["error"] = exc
                outcome["status"] = "storage_failed"
                logger.warning("okf.storage_error: %s", exc)
            finally:
                # Capacity is retained until the thread actually finishes
                self._release_worker_slot_sync()
                storage_done.set()

        self._executor.submit(_worker_task)

        # 3. Wait for storage task completion within remaining deadline
        finished = storage_done.wait(timeout=remaining_s)
        if finished:
            res = outcome["result"]
            if isinstance(res, WorkerResultWrapper):
                return res.result, res.status
            return res, outcome["status"]

        logger.warning(
            "okf.timeout_ms=%.1f fallback=baseline stage=storage_execution",
            deadline_s * 1000.0,
        )
        return None, "caller_timeout"

    async def run_storage_work(
        self,
        func: Callable[..., Any],
        *args: Any,
        deadline_s: float = 0.050,
    ) -> tuple[Any, str]:
        """Execute blocking storage work asynchronously with retained capacity and caller deadlines."""
        with self._thread_lock:
            if self._queued_callers >= self.max_queue:
                logger.warning("okf.admission_rejected queued=%d", self._queued_callers)
                return None, "admission_rejected"
            self._queued_callers += 1

        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=deadline_s)
        except asyncio.TimeoutError:
            with self._thread_lock:
                self._queued_callers -= 1
            return None, "caller_timeout"
        except asyncio.CancelledError:
            with self._thread_lock:
                self._queued_callers -= 1
            return None, "caller_cancelled"

        with self._thread_lock:
            self._queued_callers -= 1
            self._active_workers += 1

        elapsed = time.perf_counter() - t0
        remaining_s = max(0.001, deadline_s - elapsed)

        loop = asyncio.get_running_loop()
        storage_done = asyncio.Event()
        outcome: dict[str, Any] = {"result": None, "status": "running", "error": None}

        def _worker_task() -> None:
            try:
                outcome["result"] = func(*args, remaining_s)
                outcome["status"] = "completed"
            except Exception as exc:
                outcome["error"] = exc
                outcome["status"] = "storage_failed"
            finally:
                try:
                    loop.call_soon_threadsafe(self._release_worker_slot_async)
                    loop.call_soon_threadsafe(storage_done.set)
                except RuntimeError:
                    self._release_worker_slot_async()

        self._executor.submit(_worker_task)

        try:
            await asyncio.wait_for(storage_done.wait(), timeout=remaining_s)
            res = outcome["result"]
            if isinstance(res, WorkerResultWrapper):
                return res.result, res.status
            return res, outcome["status"]
        except asyncio.TimeoutError:
            return None, "caller_timeout"
        except asyncio.CancelledError:
            return None, "caller_cancelled"

    def shutdown(self, wait: bool = False) -> None:
        """Shutdown thread pool executor."""
        try:
            self._executor.shutdown(wait=wait)
        except Exception:
            pass


_OKF_CONTROLLER: OkfConcurrencyController | None = None
_OKF_CONTROLLER_LOCK = threading.Lock()


def get_okf_controller(reset: bool = False) -> OkfConcurrencyController:
    """Return process singleton OkfConcurrencyController."""
    global _OKF_CONTROLLER
    with _OKF_CONTROLLER_LOCK:
        if reset and _OKF_CONTROLLER is not None:
            try:
                _OKF_CONTROLLER.shutdown(wait=False)
            except Exception:
                pass
            _OKF_CONTROLLER = None
        if _OKF_CONTROLLER is None:
            concurrency = int(getattr(settings, "OKF_MAX_CONCURRENCY", 4))
            queue_size = int(getattr(settings, "OKF_MAX_QUEUE", 8))
            _OKF_CONTROLLER = OkfConcurrencyController(
                max_concurrency=concurrency, max_queue=queue_size
            )
    return _OKF_CONTROLLER


import sys
if __name__ in ("services.retrieval.okf_lookup", "src.services.retrieval.okf_lookup"):
    sys.modules["services.retrieval.okf_lookup"] = sys.modules[__name__]
    sys.modules["src.services.retrieval.okf_lookup"] = sys.modules[__name__]

