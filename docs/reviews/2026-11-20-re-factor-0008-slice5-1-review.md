# Review, re-factor, 2026-11-20

**Reviewed by**: claude-sonnet-4-20250514 (author on gemini)
**Scope**: 15 files, branch vs base (3c7142bc)
**Verdict**: Approve with nits

## Summary

This change implements Bounded LAYA Escalation and Query Expansion (Spec 0008 Slice 5.1) as a thin lexical vertical path through the existing LangGraph pipeline. The implementation adds typed frozen contracts, a budget gate with atomic pre reservation and strict token validation, a concurrency controller with capacity retention, a deterministic fallback policy, a decision service, an expansion executor, and a delivery gate. The code is well structured, follows the spec faithfully, and all 34 offline tests pass. The previous review's blocker (wrong keyword arguments in settle_spend) and majors (double settlement and hardcoded missing_dimension) have been resolved. The remaining findings are minor and cosmetic.

## Minor

### 🟡 Redundant `is_late` derivation in spend reconciliation callback, `backend/src/services/retrieval/laya_service.py:179`
**Problem**: The expression `status in ("late_completion", "timeout", "caller_timeout") or status != "completed"` is logically equivalent to just `status != "completed"`. The first disjunct is a strict subset of the second, making the enumeration dead logic.
**Why it matters**: A future maintainer may add a new status value and expect the enumerated list to control behavior separately from the catch all, leading to confusion about intent.
**Suggested fix**: Decide on the intended semantics. If the goal is "anything other than completed is late", simplify to `is_late = status != "completed"`. If only specific statuses should be late, remove the `or status != "completed"` clause.

### 🟡 Local BM25 expansion routed through LAYA provider concurrency controller, `backend/src/services/langgraph_pipeline.py:1544` (diff line ~384)
**Problem**: The `_do_expansion()` call wrapping `executor.execute(...)` is dispatched through `ctrl.run_provider_call()`. This occupies one of the 4 bounded semaphore slots designed for paid provider network calls, but the lexical expansion is a local CPU bound BM25 search that does not call any external provider.
**Why it matters**: Under concurrent load, local expansion work competes with actual LAYA inference calls for admission slots. With max_concurrency=4, this reduces effective provider throughput by consuming a slot for a purely local operation.
**Suggested fix**: Call `_do_expansion()` directly with its own lightweight timeout guard (for example `threading.Timer` or `concurrent.futures.ThreadPoolExecutor` with a separate short lived pool) instead of routing it through the LAYA provider controller. Reserve the provider controller exclusively for external network calls.

### 🟡 `sys.modules` manipulation at module import time, `backend/src/schemas/contracts/laya.py:16-18`
**Problem**: The module manipulates `sys.modules` during import to register itself under an alternate module name. This is a workaround for the dual import path issue (`schemas.contracts.laya` vs `src.schemas.contracts.laya`), but it couples the contracts module to the project's specific import path ambiguity.
**Why it matters**: This pattern silently creates module identity aliases that can cause subtle issues with `isinstance` checks, pickle, and debugger breakpoints. It also masks the underlying path configuration problem.
**Suggested fix**: Consider fixing the root cause by standardizing on a single import path across the project (via `pyproject.toml` package configuration or `sys.path` setup in the entry point). This is a broader project concern, not specific to this slice, so noting it here for future cleanup.

### 🟡 Missing test for `MAX_TOOL_ATTEMPTS` enforcement, test file
**Problem**: The budget ledger defines `MAX_TOOL_ATTEMPTS = 3` as a constant in `laya_gate.py`, and `record_attempt()` increments the counter, but no test verifies that the counter is checked against the ceiling. The spec (AC-4) says "429 throttling retries are charged as attempts" and "At most 1 retry is permitted for HTTP 429", but the code path in `_invoke_with_retry` does not check `ledger.tool_attempts >= MAX_TOOL_ATTEMPTS` before retrying.
**Why it matters**: If a future change adds a third retry path without a ceiling check, the budget contract is silently violated. The current code is safe because the retry logic structurally limits retries to at most 1 (the `if is_429:` block only retries once), but the budget ceiling is defined and tracked without being enforced by code.
**Suggested fix**: Either add an explicit guard `if ledger.tool_attempts >= MAX_TOOL_ATTEMPTS: return resp, status` before the retry in `_invoke_with_retry`, or document that the structural single retry limit makes the constant informational only.

## Nits

- ⚪ `backend/src/services/retrieval/laya_gate.py:66`, the `_lock` field uses `PrivateAttr(default_factory=threading.Lock)` on a non frozen Pydantic BaseModel. This works with `arbitrary_types_allowed=True` but is unusual. A plain `threading.Lock` attribute outside Pydantic (or a `__init__` override) would be more conventional.
- ⚪ `backend/src/services/retrieval/laya_concurrency.py:60-67`, the `active_count` and `queued_count` properties each acquire `self._lock` for a single int read. On CPython with the GIL, int reads are already atomic. The lock is harmless but adds overhead on every status check.
- ⚪ `backend/src/services/langgraph_pipeline.py` (diff line ~290), the hardcoded `missing_dimension = "exact_term"` default fallback at line 290 is correct for Slice 5.1 since only lexical expansion is active, but should carry an inline comment noting that Slice 5.2 must derive this from the actual gap analysis to support multi route routing.
- ⚪ `backend/src/services/retrieval/laya_executor.py:21`, mixed import paths: `from schemas.models import Chunk` (without `src.` prefix) alongside `from src.schemas.contracts.laya import ...` (with `src.` prefix). Consistent prefix usage would reduce confusion.
- ⚪ `backend/src/services/langgraph_pipeline.py` (diff line ~493), `ledger.synthesis_calls += 1` directly mutates the ledger field without going through the lock. While this is on a single request thread and not a concurrency concern in practice, it breaks the pattern established by `pre_reserve()` and `settle_spend()` which use `with self._lock:`.

## Strengths

- Excellent frozen contract layer with discriminator agreement validation, boolean confidence rejection, and normalized bounding box enforcement. The contracts match the spec exactly and provide strong schema level protection.
- Sophisticated concurrency controller with correct capacity retention on caller timeout, mutually exclusive spend settlement paths, and clean late completion discarding. The threading logic is careful and well tested.
- Thorough test suite (34 tests, 1939 lines) covering contracts, budget mechanics, concurrency edge cases, version isolation, tombstone enforcement, delivery gate blocking, and compiled pipeline integration with realistic mock doubles. Test scenarios cover both positive and negative control cases comprehensively.
- The delivery gate implementation faithfully enforces the ADR-004 invariant that free form prose is never delivered without passing verification, with clean fallback to deterministic excerpts or refusal.
- The `build_pinned_snapshot_context` changes in `knowledge_graph_repo.py` correctly relax the KG manifest requirement while tightening the boundary_repo contract (raising instead of silently swallowing missing dependencies).

## Test coverage

Tests are configured and comprehensive. 34 tests pass covering all 8 acceptance criteria. Contract validation, budget pre reservation and settlement, concurrency admission and load shedding, caller timeout with capacity retention, 429 retry charging, happy path expansion, empty context entry, unavailable route rejection, version isolation, tombstone halting, delivery gate blocking, single synthesis enforcement, stale refusal clearing, no progress suppression, invalid usage fallback, missing dimension derivation, production snapshot resolution, snapshot failure halting, slow retrieval deadline retention, and slow reranking deadline retention are all tested.

No significant coverage gaps were identified. One minor gap: the `MAX_TOOL_ATTEMPTS` ceiling is tracked but not enforced or tested (see Minor finding above).
