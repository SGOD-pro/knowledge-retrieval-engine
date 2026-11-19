# Review, re-factor, 2026-11-20

**Reviewed by**: claude-sonnet-4-6 (author on gemini)
**Scope**: 15 files, branch vs base (3c7142bc)
**Verdict**: Approve with nits

## Summary

This update evaluates the implementation and the resolutions to previous findings for Spec 0008 Slice 5.1 covering Bounded LAYA Escalation and Query Expansion. The MAX_TOOL_ATTEMPTS ceiling enforcement and its corresponding test are verified, the is_late status derivation in the spend callback is simplified, synthesis call counting is made thread safe under the ledger lock, and module imports in the executor are normalized. All 35 slice tests and 145 integrated regression tests pass cleanly. The branch is ready for merge with minor non blocking observations noted for Slice 5.2.

## Minor

### 🟡 Local BM25 expansion routed through provider concurrency controller, `backend/src/services/langgraph_pipeline.py:1531`
**Problem**: The executor call for local BM25 expansion is dispatched through ctrl.run_provider_call. This consumes one of the 4 admission semaphore slots designed for external paid provider calls, even though lexical expansion is a local CPU operation.
**Why it matters**: Under concurrent load, local expansion work can temporarily occupy provider slots intended for model inference calls.
**Suggested fix**: In Slice 5.2, call the expansion executor directly with an isolated thread pool or deadline guard rather than sharing the provider concurrency controller.

### 🟡 Module aliasing at import time, `backend/src/schemas/contracts/laya.py:16-18`
Resolved in commit `6cd3ebc` by standardizing all imports across test suites and services to use `schemas.contracts.laya` with fallback, eliminating `sys.modules` manipulation entirely.

## Nits

- ⚪ `backend/src/services/retrieval/laya_gate.py:66`, the `_lock` attribute uses `PrivateAttr(default_factory=threading.Lock)` on a Pydantic model where a regular class initialization attribute would be cleaner.
- ⚪ `backend/src/services/retrieval/laya_concurrency.py:47-55`, reading integer counters under the lock in property accessors resolved in commit `7fa305c` by returning attributes directly without locking.

## Strengths

- Robust resolution of previous findings including MAX_TOOL_ATTEMPTS ceiling enforcement, thread safe synthesis call tracking, simplified spend reconciliation, and normalized imports.
- Bounded concurrency controller with capacity retention and clean discarding of late completions preserves system stability.
- Authoritative delivery gate enforces ADR 004 by blocking unverified free form prose and falling back to verified deterministic facts.
- Comprehensive test coverage with 35 dedicated unit tests and 145 integrated tests verifying all acceptance criteria without paid provider invocations.

## Test coverage

Test signal is configured and comprehensive. All 35 unit tests in test_laya_escalation_slice5_1.py pass in under 5 seconds, and 145 integrated tests across boundary isolation, deterministic pipelines, and contracts pass without errors. Test coverage includes positive and negative controls for contract validation, budget pre reservation, 429 retry limits, tool attempt ceilings, caller timeouts with retained capacity, tombstone enforcement, and single synthesis limits.
