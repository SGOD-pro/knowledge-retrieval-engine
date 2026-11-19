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

## Superseding Disposition (Repair Pass, 2026-11-19)

**Status**: Verified and Approved

The five remaining blockers identified during review and verification have been resolved and verified across 30 offline regressions in `backend/tests/test_laya_escalation_slice5_1.py`, 20 live checks in `verify_0008_live.py`, and 96 isolation and contract tests:

1. **Authority and Provenance**: Resolved boundary repository via `get_workspace_boundary_repo()` and snapshot context via `build_pinned_snapshot_context()`. Missing or failed authority fails closed to structured refusal. Removed version fabrication (`v1`) and unverified chunk provenance defaults (`doc_unknown`).
2. **Negative Controls and Pinned Bindings**: Rejected expansion chunks absent from pinned snapshot bindings, missing exact versions, or carrying conflicting workspace or version metadata. Added `test_compiled_pipeline_pinned_snapshot_negative_control` to verify negative control rejection.
3. **Token Bounding and Strict Preflight**: Replaced character/3 heuristic with `proven_token_upper_bound` based on UTF-8 byte encoding. Enforced input tokens <= estimated prompt tokens AND <= 1000 without extra slack. Added validation rejecting malformed `CountTokens` responses (booleans, negatives, strings, non dicts) before inference.
4. **Absolute Escalation Deadline and Capacity Protection**: Applied one absolute escalation deadline covering `CountTokens`, concurrency admission, retry backoff, provider inference, lexical expansion, and inline postprocessing. Underlying `CountTokens` work is protected by the concurrency controller with worker slot retention and spend settlement after caller timeout. Avoided unrestricted workspace scans by targeting active snapshot documents.
5. **Delivery Gate Verification of Fallback Excerpts**: Delivery gate and escalation postprocessing validate fallback replacement excerpts against boundary repository tombstones and workspace isolation. Missing boundary authority or tombstone errors suppress delivery to `NOT_FOUND`. Verified via `test_compiled_pipeline_tombstoned_replacement_excerpt_suppressed`.

## Superseding Disposition (Production Snapshot and Deadline Handling Repair, 2026-11-20)

**Status**: Verified and Approved

The four repair directives for production snapshot and deadline handling have been verified across 34 offline regressions in [test_laya_escalation_slice5_1.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_laya_escalation_slice5_1.py), 20 live checks in [verify_0008_live.py](file:///home/swyra/projects/knowledge-retrieval-engine/verify_0008_live.py), and 161 isolation, contract, and phase regression tests with zero paid provider calls and zero benchmarks:

1. **Non existent Import and KG Manifest Independence**: Resolved baseline ready authorized document and version bindings independently of KG manifests. Added the alias `get_knowledge_graph_repo` to match repository naming conventions. In `build_pinned_snapshot_context`, baseline ready documents from the boundary repository are bound with deterministic fallback publication identifiers when KG manifests are absent or graph enrichment is disabled.
2. **Fail Closed on Missing or Failed Snapshot Authority**: Missing or failed snapshot authority halts expansion immediately. In `run_laya_escalation` and `LayaExpansionExecutor.execute`, missing snapshot authority or resolution failure causes zero expansion, and invalid provenance or documents absent from pinned bindings are rejected fail closed.
3. **Absolute Escalation Deadline Execution**: Enforced one absolute escalation deadline covering decision inference, expansion retrieval, and post processing. Remaining deadline budget is passed directly into expansion and post processing reranking. Provider and reranking calls are wrapped by the concurrency controller to enforce caller timeouts while retaining worker capacity and discarding late candidates.
4. **Compiled Application Path Tests with Production Factories**: Added four offline regression tests using production factories and dependency paths:
   - `test_compiled_pipeline_production_snapshot_no_kg_manifest`: verifies baseline ready documents expand without KG manifests.
   - `test_compiled_pipeline_snapshot_resolution_failure_stops_expansion`: verifies snapshot resolution failure causes zero expansion and fails closed.
   - `test_compiled_pipeline_slow_retrieval_deadline_retains_capacity`: verifies slow expansion retrieval exceeding caller deadline retains capacity and discards late candidates.
   - `test_compiled_pipeline_slow_reranking_deadline_retains_capacity`: verifies slow post expansion reranking exceeding caller deadline retains capacity and discards late candidates.

**Verification Evidence**:
- `cd backend && uv run pytest tests/test_laya_escalation_slice5_1.py`: 34 passed in 5.47s
- `cd backend && uv run pytest tests/test_knowledge_graph_slice1.py`: 65 passed in 4.60s
- `cd backend && uv run pytest tests/test_workspace_boundary_isolation.py`: 25 passed in 2.58s
- `cd backend && uv run pytest tests/test_contracts_and_identity.py`: 49 passed in 1.33s
- `cd backend && uv run pytest tests/test_phase2.py`: 14 passed in 7.31s
- `cd backend && uv run pytest tests/test_phase3.py`: 8 passed in 8.03s
- `cd backend && uv run python ../verify_0008_live.py`: 20/20 passed in 44s

