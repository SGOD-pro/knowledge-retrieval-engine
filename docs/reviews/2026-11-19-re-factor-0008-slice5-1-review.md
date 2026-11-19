# Review, re-factor (0008 Slice 5.1), 2026-11-19

**Reviewed by**: claude-opus-4-6-thinking / gemini-3.8-flash-high (author on gemini)
**Scope**: 14 files, branch vs base (3c7142bc)
**Verdict**: Approve

## Summary
Slice 5.1 implements bounded LAYA escalation with lexical expansion, typed contracts, budget gating, concurrency admission, a delivery gate, and a comprehensive 25 test offline regression suite (all passing). All blocker and major issues identified during the initial review pass have been cleanly resolved: the `settle_spend` argument mismatch and double settlement on invalid usage were eliminated by relying on the concurrency controller's spend callback and adding `record_invalid_usage`, and `missing_dimension` is now dynamically derived from failed stage and error context. Concurrency lifecycle cleanup, Pydantic `PrivateAttr` lock handling, and direct attribute access in `query_service.py` were also added.

## Strengths
- The contract layer (`laya.py`) is exceptionally robust: frozen models, strict `extra="forbid"`, discriminator agreement validation, boolean confidence rejection, and normalized bounding box checks form a strong defensive boundary against malformed model outputs.
- The concurrency controller (`laya_concurrency.py`) with bounded semaphore, capacity retention on caller timeout, and late completion discarding with spend reconciliation cleanly solves difficult race conditions under load.
- The fail closed delivery gate (`langgraph_pipeline.py`) correctly re-checks dynamic tombstones before delivery, blocks free-form text when a verifier is absent or unconfigured, and enforces single synthesis call limits under ADR-004.
- Test coverage is comprehensive across 25 unit tests and 20 live verification checks with zero paid model calls or benchmarks.

## Test coverage
Tests are `configured` and 25 offline tests pass in `backend/tests/test_laya_escalation_slice5_1.py`, along with 20 live checks in `verify_0008_live.py` and 125 integrated pipeline regression tests. Coverage includes contracts (AC-2), policy fallback (AC-3), budget gating (AC-4), concurrency admission (AC-4), usage validation and invalid usage fallback (AC-4), tombstone isolation (AC-5), delivery gate (AC-6), stale refusal clearing and no progress refusal (AC-7), and offline execution (AC-8).
