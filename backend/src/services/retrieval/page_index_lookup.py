"""Isolated, scoped PageIndex structural lookup service.

Enforces:
- Hard client deadlines across DynamoDB and S3 reads (default 50ms).
- Explicit SDK retry disabling (zero retries).
- Explicit admission and queue bounding with immediate load shedding.
- Worker capacity retention until underlying storage work actually completes.
- Clean distinction between caller timeout and storage cancellation.
- Immediate workspace and source tombstone enforcement (zero leakage, zero errors).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import threading
import time
from typing import Any, Callable

from config import settings
from src.db.page_index_repo import PageIndexRepository
from src.schemas.contracts.page_index import (
    ElementLocation,
    PageIndexDetail,
    PageIndexSummary,
    StructuralLookupResult,
)

logger = logging.getLogger(__name__)

# Query target extraction pattern
_TARGET_QUERY_PATTERN = re.compile(
    r"\b(table|figure|exhibit|box|chart)\s+([a-z0-9.-]+)\b",
    re.IGNORECASE,
)


class WorkerResultWrapper:
    """Explicit wrapper for worker tasks returning custom execution statuses."""

    def __init__(self, result: Any, status: str = "completed"):
        self.result = result
        self.status = status


class PageIndexConcurrencyController:
    """Admission gate and capacity tracker for PageIndex storage lookups.

    Enforces explicit admission queue bounds, retaining worker capacity
    until the underlying storage thread completes, even when callers time out
    or cancel their coroutines.
    """

    def __init__(self, max_concurrency: int = 4, max_queue: int = 8) -> None:
        self.max_concurrency = max_concurrency
        self.max_queue = max_queue
        self._active_workers = 0
        self._queued_callers = 0
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        self._sync_semaphore = threading.Semaphore(max_concurrency)
        self._thread_lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix="page_index",
        )

    @property
    def active_count(self) -> int:
        with self._thread_lock:
            return self._active_workers

    @property
    def queued_count(self) -> int:
        with self._thread_lock:
            return self._queued_callers

    def _release_worker_slot(self) -> None:
        with self._thread_lock:
            self._active_workers = max(0, self._active_workers - 1)
        self._semaphore.release()

    def _release_worker_slot_sync(self) -> None:
        with self._thread_lock:
            self._active_workers = max(0, self._active_workers - 1)
        self._sync_semaphore.release()

    async def run_storage_work(
        self,
        func: Callable[..., Any],
        *args: Any,
        deadline_s: float = 0.050,
    ) -> tuple[Any, str]:
        """Execute blocking storage work on thread pool with retained capacity and deadlines.

        Returns (result, status) where status is one of:
          - "completed"
          - "caller_timeout"
          - "caller_cancelled"
          - "admission_rejected"
          - "storage_failed"
        """
        # 1. Bounded admission check
        with self._thread_lock:
            if self._queued_callers >= self.max_queue:
                logger.warning(
                    "page_index.admission_rejected queued=%d max=%d",
                    self._queued_callers,
                    self.max_queue,
                )
                return None, "admission_rejected"
            self._queued_callers += 1

        t0 = time.perf_counter()

        # 2. Acquire concurrency slot within deadline
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=deadline_s)
        except asyncio.TimeoutError:
            with self._thread_lock:
                self._queued_callers -= 1
            logger.warning(
                "page_index.timeout_ms=%.1f fallback=baseline stage=queue_admission",
                deadline_s * 1000.0,
            )
            return None, "caller_timeout"
        except asyncio.CancelledError:
            with self._thread_lock:
                self._queued_callers -= 1
            return None, "caller_cancelled"

        with self._thread_lock:
            self._queued_callers -= 1
            self._active_workers += 1

        # Calculate remaining time for storage execution
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
                logger.warning("page_index.storage_error: %s", exc)
            finally:
                # Capacity is retained until the thread actually finishes
                try:
                    loop.call_soon_threadsafe(self._release_worker_slot)
                    loop.call_soon_threadsafe(storage_done.set)
                except RuntimeError:
                    # Handle process shutdown gracefully when event loop is closed
                    self._release_worker_slot()

        self._executor.submit(_worker_task)

        # 3. Wait for storage task completion within remaining deadline
        try:
            await asyncio.wait_for(storage_done.wait(), timeout=remaining_s)
            res = outcome["result"]
            if isinstance(res, WorkerResultWrapper):
                return res.result, res.status
            return res, outcome["status"]
        except asyncio.TimeoutError:
            logger.warning(
                "page_index.timeout_ms=%.1f fallback=baseline stage=storage_execution",
                deadline_s * 1000.0,
            )
            # Worker slot will be released by _worker_task when the thread finishes
            return None, "caller_timeout"
        except asyncio.CancelledError:
            logger.debug("page_index.caller_cancelled stage=storage_execution")
            return None, "caller_cancelled"

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
                    "page_index.admission_rejected queued=%d max=%d",
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
                "page_index.timeout_ms=%.1f fallback=baseline stage=queue_admission",
                deadline_s * 1000.0,
            )
            return None, "caller_timeout"

        with self._thread_lock:
            self._queued_callers -= 1
            self._active_workers += 1

        # Calculate remaining time for storage execution
        elapsed = time.perf_counter() - t0
        remaining_s = max(0.001, deadline_s - elapsed)

        outcome: dict[str, Any] = {"result": None, "status": "running", "error": None}
        work_done = threading.Event()

        def _worker_task() -> None:
            try:
                outcome["result"] = func(*args, remaining_s)
                outcome["status"] = "completed"
            except Exception as exc:
                outcome["error"] = exc
                outcome["status"] = "storage_failed"
                logger.warning("page_index.storage_error: %s", exc)
            finally:
                # Capacity remains retained until thread actually completes
                self._release_worker_slot_sync()
                work_done.set()

        self._executor.submit(_worker_task)

        # 3. Wait for storage task completion within remaining deadline
        finished = work_done.wait(timeout=remaining_s)
        if finished:
            res = outcome["result"]
            if isinstance(res, WorkerResultWrapper):
                return res.result, res.status
            return res, outcome["status"]
        else:
            logger.warning(
                "page_index.timeout_ms=%.1f fallback=baseline stage=storage_execution",
                deadline_s * 1000.0,
            )
            # Worker slot remains retained until _worker_task finishes
            return None, "caller_timeout"


# Global process concurrency controller
_PAGE_INDEX_CONTROLLER: PageIndexConcurrencyController | None = None


def get_page_index_controller() -> PageIndexConcurrencyController:
    global _PAGE_INDEX_CONTROLLER
    if _PAGE_INDEX_CONTROLLER is None:
        concurrency = getattr(settings, "PAGEINDEX_MAX_CONCURRENCY", 4)
        queue_size = getattr(settings, "PAGEINDEX_MAX_QUEUE", 8)
        _PAGE_INDEX_CONTROLLER = PageIndexConcurrencyController(
            max_concurrency=concurrency, max_queue=queue_size
        )
    return _PAGE_INDEX_CONTROLLER


class PageIndexLookupService:
    """Isolated PageIndex structural target lookup."""

    def __init__(
        self,
        repository: PageIndexRepository | None = None,
        controller: PageIndexConcurrencyController | None = None,
    ) -> None:
        self.repo = repository or PageIndexRepository()
        self.controller = controller or get_page_index_controller()

    def resolve_target_from_manifest(
        self,
        summary: PageIndexSummary,
        target_label: str,
        remaining_deadline_s: float | None = None,
    ) -> StructuralLookupResult:
        """Resolve target label against summary and detail without model calls."""
        norm_target = target_label.strip().lower()

        # Handle missing layout state
        if summary.layout_status == "missing_layout":
            return StructuralLookupResult(
                target_label=target_label,
                state="missing_layout",
                candidate_pages=(),
                matching_locations=(),
            )

        # Check inline definitions first
        matching_defs: list[ElementLocation] = list(summary.inline_definitions.get(norm_target, ()))
        cross_ref_pages: list[int] = list(summary.inline_cross_references.get(norm_target, ()))

        # If summary has S3 overflow, load detail to inspect definitions and cross-references
        if summary.has_s3_overflow and summary.s3_overflow_key:
            detail = self.repo.get_detail(summary, remaining_deadline_s=remaining_deadline_s)
            if detail is not None:
                if norm_target in detail.definitions:
                    matching_defs = list(detail.definitions[norm_target])
                if norm_target in detail.cross_references:
                    cross_ref_pages = [cr.page_number for cr in detail.cross_references[norm_target]]

        if matching_defs:
            cand_pages = tuple(sorted(list({loc.page_number for loc in matching_defs})))
            state = "unique" if len(cand_pages) == 1 else "ambiguous"
            return StructuralLookupResult(
                target_label=target_label,
                state=state,
                candidate_pages=cand_pages,
                matching_locations=tuple(matching_defs),
                has_cross_references_only=False,
                cross_reference_pages=tuple(sorted(list(set(cross_ref_pages)))),
            )

        # Check if only cross-references exist
        if cross_ref_pages:
            return StructuralLookupResult(
                target_label=target_label,
                state="none",
                candidate_pages=(),
                matching_locations=(),
                has_cross_references_only=True,
                cross_reference_pages=tuple(sorted(list(set(cross_ref_pages)))),
            )

        return StructuralLookupResult(
            target_label=target_label,
            state="none",
            candidate_pages=(),
            matching_locations=(),
        )

    def _sync_lookup_target(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        target_label: str,
        chunk_repo: Any | None,
        remaining_deadline_s: float,
    ) -> WorkerResultWrapper:
        """Perform isolated tombstone checking, manifest/detail reading, and candidate chunk fetching."""
        t_start = time.perf_counter()

        # 1. Bounded tombstone check inside the worker (fail-closed)
        try:
            if self.repo.boundary_repo.has_tombstone(workspace_id, "workspace", workspace_id):
                logger.debug("page_index.tombstone_workspace_blocked ws=%s", workspace_id)
                return WorkerResultWrapper(None, "tombstoned")
            if self.repo.boundary_repo.has_tombstone(workspace_id, "source", document_id):
                logger.debug("page_index.tombstone_source_blocked doc=%s", document_id)
                return WorkerResultWrapper(None, "tombstoned")
        except Exception as exc:
            logger.warning(
                "page_index.tombstone_check_failed ws=%s doc=%s error=%s (fail-closed)",
                workspace_id,
                document_id,
                exc,
            )
            return WorkerResultWrapper(None, "tombstoned")

        # 2. Read manifest (DynamoDB or SQLite)
        summary = self.repo.get_summary(workspace_id, document_id, document_version)
        if summary is None:
            return WorkerResultWrapper(None, "completed")

        elapsed = time.perf_counter() - t_start
        s3_budget = max(0.001, remaining_deadline_s - elapsed)

        # 3. Resolve target from manifest (including S3 overflow if needed)
        res = self.resolve_target_from_manifest(
            summary=summary,
            target_label=target_label,
            remaining_deadline_s=s3_budget,
        )
        if res is None:
            return WorkerResultWrapper(None, "completed")

        # 4. Fetch candidate chunks inside the worker if chunk_repo is provided and pages matched
        if chunk_repo is not None and (res.candidate_pages or res.cross_reference_pages):
            pages_to_fetch = res.candidate_pages if res.candidate_pages else res.cross_reference_pages
            doc_chunks = chunk_repo.get_all_chunks(
                document_ids=[document_id],
                workspace_id=workspace_id,
                page_numbers=pages_to_fetch,
            )
            res = res.model_copy(update={"candidate_chunks": tuple(doc_chunks)})

        return WorkerResultWrapper(res, "completed")

    def _sync_read_manifest_and_resolve(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        target_label: str,
        remaining_deadline_s: float,
    ) -> StructuralLookupResult | None:
        """Synchronous storage reading executed in the controller thread pool."""
        outcome = self._sync_lookup_target(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            target_label=target_label,
            chunk_repo=None,
            remaining_deadline_s=remaining_deadline_s,
        )
        return outcome.result if isinstance(outcome, WorkerResultWrapper) else outcome

    async def lookup_target(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        target_label: str,
        deadline_ms: float | None = None,
        chunk_repo: Any | None = None,
    ) -> tuple[StructuralLookupResult | None, str]:
        """Perform deadline-enforced, concurrency-bounded isolated lookup.

        Returns (result, execution_status).
        """
        total_deadline_s = (deadline_ms or getattr(settings, "PAGEINDEX_TIMEOUT_MS", 50)) / 1000.0

        result, status = await self.controller.run_storage_work(
            self._sync_lookup_target,
            workspace_id,
            document_id,
            document_version,
            target_label,
            chunk_repo,
            deadline_s=total_deadline_s,
        )
        return result, status

    def lookup_target_sync(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        target_label: str,
        deadline_ms: float | None = None,
        chunk_repo: Any | None = None,
    ) -> tuple[StructuralLookupResult | None, str]:
        """Perform synchronous deadline-enforced, concurrency-bounded isolated lookup.

        Returns (result, execution_status).
        """
        total_deadline_s = (deadline_ms or getattr(settings, "PAGEINDEX_TIMEOUT_MS", 50)) / 1000.0

        result, status = self.controller.run_storage_work_sync(
            self._sync_lookup_target,
            workspace_id,
            document_id,
            document_version,
            target_label,
            chunk_repo,
            deadline_s=total_deadline_s,
        )
        return result, status

    def extract_target_labels(self, query: str) -> list[str]:
        """Extract candidate structural target labels from query text."""
        matches = _TARGET_QUERY_PATTERN.findall(query)
        labels: list[str] = []
        for prefix, num in matches:
            clean_num = num.strip().rstrip(".:-").lower()
            labels.append(f"{prefix.lower()} {clean_num}")
        return list(dict.fromkeys(labels))
