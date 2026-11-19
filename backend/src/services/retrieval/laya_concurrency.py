"""Concurrency controller and bounded admission queue for LAYA provider calls.

Enforces Slice 5.1 constraints:
- Global process admission queue capping active concurrent LAYA escalation workers at 4.
- Bounded wait queue capping waiting callers at 8.
- Immediate load shedding on queue saturation (admission_rejected).
- Capacity retention on caller timeout: semaphore worker slot is retained until
  the underlying provider call finishes.
- Late result suppression: late completions are discarded and do not mutate query results.
- Late spend reconciliation: billable tokens and latency are reconciled into the spend ledger
  even after caller cancellation or timeout.
- Deadline and capacity protection for both inference and count_tokens operations.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class LayaProviderConcurrencyController:
    """Bounded concurrency and admission queue for LAYA model operations."""

    def __init__(
        self,
        max_concurrency: int = 4,
        max_queue: int = 8,
    ) -> None:
        self.max_concurrency = max_concurrency
        self.max_queue = max_queue
        self._active_workers = 0
        self._queued_callers = 0
        self._semaphore = threading.Semaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix="laya_provider_worker",
        )

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active_workers

    @property
    def queued_count(self) -> int:
        with self._lock:
            return self._queued_callers

    def run_provider_call(
        self,
        func: Callable[..., Any],
        *args: Any,
        timeout_s: float = 2.5,
        on_spend_reconcile: Callable[[dict[str, Any], float, str], None] | None = None,
        **kwargs: Any,
    ) -> tuple[Any, str]:
        """Execute blocking provider call with capacity retention and caller timeout enforcement.

        Returns (result, status) where status is one of:
        - "completed": underlying call returned successfully within deadline
        - "caller_timeout": caller deadline expired while waiting for slot or execution
        - "admission_rejected": wait queue is saturated (immediate load shedding)
        - "call_failed": underlying call raised an unhandled exception

        If caller deadline expires before completion, the underlying thread and semaphore
        remain retained until the background task finishes. Late completion results are discarded
        so they do not mutate finished query results, but on_spend_reconcile is called with
        actual billable token usage and latency.
        """
        if timeout_s <= 0.0:
            return None, "caller_timeout"

        with self._lock:
            if self._queued_callers >= self.max_queue:
                logger.warning(
                    "laya.admission_rejected queued=%d max=%d",
                    self._queued_callers,
                    self.max_queue,
                )
                return None, "admission_rejected"
            self._queued_callers += 1

        t0 = time.perf_counter()
        acquired = self._semaphore.acquire(timeout=timeout_s)
        if not acquired:
            with self._lock:
                self._queued_callers -= 1
            logger.warning("laya.caller_timeout stage=queue_admission timeout_s=%.2f", timeout_s)
            return None, "caller_timeout"

        with self._lock:
            self._queued_callers -= 1
            self._active_workers += 1

        elapsed = time.perf_counter() - t0
        remaining_s = max(0.001, timeout_s - elapsed)

        outcome: dict[str, Any] = {
            "result": None,
            "error": None,
            "status": "running",
            "usage": None,
            "latency_ms": 0.0,
        }
        call_done = threading.Event()
        caller_timed_out = threading.Event()

        def _worker_task() -> None:
            w_t0 = time.perf_counter()
            try:
                res = func(*args, **kwargs)
                outcome["result"] = res
                outcome["status"] = "completed"
                # Extract usage dict if present in response
                if isinstance(res, dict) and "usage" in res:
                    outcome["usage"] = res.get("usage")
            except Exception as exc:
                outcome["error"] = exc
                outcome["status"] = "call_failed"
            finally:
                w_latency_ms = (time.perf_counter() - w_t0) * 1000.0
                outcome["latency_ms"] = w_latency_ms

                # If caller already timed out, reconcile spend while discarding result
                is_late = caller_timed_out.is_set()
                if is_late and on_spend_reconcile is not None:
                    try:
                        on_spend_reconcile(outcome.get("usage") or {}, w_latency_ms, outcome["status"])
                    except Exception as e:
                        logger.warning("laya.late_spend_reconcile_failed: %s", e)

                with self._lock:
                    self._active_workers = max(0, self._active_workers - 1)
                self._semaphore.release()
                call_done.set()

        self._executor.submit(_worker_task)

        finished = call_done.wait(timeout=remaining_s)
        if finished:
            if on_spend_reconcile is not None and outcome.get("usage"):
                try:
                    on_spend_reconcile(outcome["usage"], outcome["latency_ms"], outcome["status"])
                except Exception as e:
                    logger.warning("laya.spend_reconcile_failed: %s", e)

            if outcome["error"] is not None:
                return outcome["error"], outcome["status"]
            return outcome["result"], outcome["status"]

        # Caller timeout: signal caller_timed_out so worker task reconciles spend upon late finish
        caller_timed_out.set()
        logger.warning(
            "laya.caller_timeout stage=call_execution timeout_s=%.2f worker_slot_retained=True",
            timeout_s,
        )
        return None, "caller_timeout"


_SHARED_LAYA_CONTROLLER: LayaProviderConcurrencyController | None = None
_CONTROLLER_LOCK = threading.Lock()


def get_laya_concurrency_controller(
    reset: bool = False,
    max_concurrency: int = 4,
    max_queue: int = 8,
) -> LayaProviderConcurrencyController:
    """Return process singleton LayaProviderConcurrencyController."""
    global _SHARED_LAYA_CONTROLLER
    with _CONTROLLER_LOCK:
        if reset or _SHARED_LAYA_CONTROLLER is None:
            _SHARED_LAYA_CONTROLLER = LayaProviderConcurrencyController(
                max_concurrency=max_concurrency,
                max_queue=max_queue,
            )
        return _SHARED_LAYA_CONTROLLER
