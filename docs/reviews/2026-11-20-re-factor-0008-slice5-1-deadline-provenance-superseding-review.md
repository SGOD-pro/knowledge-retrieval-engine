# Review, re-factor, 2026-11-20 (Superseding Review: Deadline Enforcement and Provenance Resolution)

**Reviewed by**: claude-sonnet-4-6 (author on gemini)
**Scope**: 6 files, commit `5d05e5c709e99eb5962e26090c29ae022712dc26`
**Verdict**: Approve

## Summary

This superseding review confirms resolution of the remaining Spec 0008 Slice 5.1 gaps regarding absolute deadline enforcement across all post expansion stages and strict cross field provenance disagreement rejection.

Historical reviews are preserved. This disposition verifies that all post expansion work (compression, secondary fidelity, answer synthesis, and semantic verification) is bounded by the caller escalation deadline using `ctrl.run_provider_call`. Worker capacity is retained until completion, late results are suppressed from pipeline state, and spend is reconciled cleanly on `LayaBudgetLedger`. Furthermore, chunk provenance is validated across all populated attribute and metadata fields against authoritative pinned snapshot bindings, rejecting any disagreement or unpinned documents while preserving exact opaque version strings.

## Addressed Findings and Verifications

### 1. Absolute Escalation Deadline Enforcement Across All Stages
* **Compression**: Dispatched via `ctrl.run_provider_call` bounded by remaining escalation deadline. When the worker exceeds the deadline, the timeout returns clean None, worker capacity is retained, and late compressed text is suppressed.
* **Secondary Fidelity**: Dispatched via `ctrl.run_provider_call` bounded by remaining escalation deadline. When the check exceeds the deadline, execution fails closed and triggers a clean structured refusal.
* **Answer Synthesis**: Dispatched via `ctrl.run_provider_call` bounded by remaining escalation deadline with `on_spend_reconcile` callback. When synthesis exceeds the deadline, worker capacity is retained, the late answer is suppressed from delivery, and billable token spend is reconciled on the budget ledger.
* **Semantic Verification Delivery Gate**: Dispatched via `ctrl.run_provider_call` bounded by remaining escalation deadline. Timeouts fail closed to deterministic replacement excerpts or structured refusal without delivering unverified free form text.

### 2. Strict Provenance Validation and Cross Field Disagreement Rejection
* Function `validate_chunk_provenance` in [laya_executor.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/laya_executor.py#L32) collects populated workspace, document identity, and version values across top level attributes and the `metadata` dictionary.
* If any two populated values disagree (e.g. matching attribute with conflicting metadata), the chunk is rejected immediately.
* Chunks absent from pinned snapshot bindings or marked as tombstoned are rejected fail closed.
* Opaque version strings (e.g. `rev-2026.10-sha256:e3b0c442`) are preserved without coercion or relabeling.

### 3. Integrated Test Count Reconciliation
* The historical count of 131 tests reflected a partial suite invocation before wiring and audit guard suites were added (35 slice tests plus 96 baseline tests).
* The subsequent count of 145 tests reflected the full suite with 35 slice tests (35 slice plus 61 contracts, 22 boundary isolation, 14 phase 2, 8 phase 3, 7 deterministic wiring, and 4 mandatory audit guards, 35 + 110 = 145).
* The current count of 150 tests reflects 5 additional compiled application path regressions in [test_laya_escalation_slice5_1.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_laya_escalation_slice5_1.py), totaling 40 slice tests (40 + 110 = 150 passed).

## Test Coverage

* **Slice 5.1 Unit Tests**: 40 passed in 7.43s (`tests/test_laya_escalation_slice5_1.py`).
* **Live Runtime Verification**: 20 of 20 checks passed (`verify_0008_live.py`).
* **Integrated Regressions**: 150 passed, 1 warning in 15.48s (`test_laya_escalation_slice5_1.py`, `test_contracts_and_identity.py`, `test_workspace_boundary_isolation.py`, `test_phase2.py`, `test_phase3.py`, `test_deterministic_pipeline_wiring.py`, `test_mandatory_audit_guards.py`).

Zero paid provider calls made. All provider and reranking operations use local test doubles.
