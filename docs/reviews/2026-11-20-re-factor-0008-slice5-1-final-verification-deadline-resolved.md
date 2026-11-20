# Review, re-factor, 2026-11-20 (Superseding Review: Final Verification Deadline Propagation)

**Reviewed by**: claude-sonnet-4-6 (author on gemini)
**Scope**: 3 files, commit to be published on branch `re-factor`
**Verdict**: Approve

## Summary

This superseding review confirms resolution of the remaining Spec 0008 Slice 5.1 gap regarding final verification deadline propagation through the compiled application path under default production timing.

Historical reviews are preserved. This disposition verifies that pipeline state derives and persists one absolute monotonic escalation deadline (`_escalation_deadline_t` and `escalation_deadline_t`) derived from the minimum of remaining query time, the 2500ms phase ceiling, and any tighter caller limit. The exact same deadline is passed through decision, expansion, post processing (reranking, context compression, secondary fidelity), answer synthesis, and final answer semantic verification in `delivery_gate`.

Under default production timing (where callers omit `caller_deadline_ms` or `phase_deadline_ms`), the 2500ms escalation phase ceiling is strictly enforced. The delivery gate receives only the remaining allowance from the escalation phase and never falls back to the 15 second query budget after escalation has run. Expired phase allowances cause zero verifier dispatch, while slow verifiers retain worker capacity and cannot publish late text.

## Addressed Findings and Verifications

### 1. Monotonic Escalation Deadline Propagation
* In [langgraph_pipeline.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L1330), `run_laya_escalation` derives `escalation_deadline_t = t0 + (phase_deadline_ms / 1000.0)` where `phase_deadline_ms` is the minimum of remaining query time (`15000.0 - elapsed_query_ms`), the 2500ms phase ceiling, and any valid caller override.
* Both `_escalation_deadline_t` and `escalation_deadline_t` are persisted into pipeline state across all early returns, stage updates, and final node returns.
* In [langgraph_pipeline.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L1989), `delivery_gate` checks `state.get("_escalation_deadline_t") or state.get("escalation_deadline_t")`. When escalation has executed, `rem_s_verify = max(0.0, float(esc_deadline_t) - time.perf_counter())`.
* The delivery gate never restarts a duration or falls back to the 15 second query budget after escalation has run.

### 2. Zero Verifier Dispatch on Expired Phase
* When earlier escalation stages consume the 2500ms phase ceiling (e.g. answer synthesis consumes 2.5s), remaining phase time at `delivery_gate` is below the 50ms execution threshold.
* The gate immediately logs `delivery_gate.verifier_deadline_insufficient` and returns a structured refusal (`final_answer="NOT_FOUND"`, `retrieval_incomplete=True`).
* The semantic verifier is never called, verifying exactly zero verifier dispatch (`mock_verifier.verify.call_count == 0`).

### 3. Capacity Retention and Late Text Suppression Under Default Timing
* When earlier escalation stages consume most of the 2500ms phase allowance (e.g. answer synthesis consumes 2.1s), the semantic verifier receives only the remaining phase allowance (~0.4s).
* When the verifier takes longer than the remaining phase allowance, `ctrl.run_provider_call` triggers a caller timeout after ~0.4s (well before the 15 second query budget or worker timeout).
* Worker capacity is retained (`ctrl.active_count == 1`) while the verifier runs in the background.
* The pipeline immediately returns `final_answer="NOT_FOUND"` and `retrieval_incomplete=True`, ensuring late verified text cannot reach the caller.
* Upon worker completion, capacity is cleanly released (`ctrl.active_count == 0`).

### 4. Regression Suite Reconciliation
* Two compiled application path regressions added in [test_laya_escalation_slice5_1.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_laya_escalation_slice5_1.py):
  - `test_compiled_pipeline_default_timing_expired_phase_zero_verifier_dispatch`
  - `test_compiled_pipeline_default_timing_slow_verification_retains_capacity_and_suppresses_late_text`
* Slice 5.1 regression suite increased from 40 to 42 tests, passing in 13.01s.
* Integrated regression suite across 7 files increased from 150 to 152 tests, passing in 28.01s.
* Live runtime verification across 20 endpoints passed with zero failures.

## Test Evidence

```bash
# 1. Focused Slice 5.1 regression suite
cd backend && uv run pytest tests/test_laya_escalation_slice5_1.py -v
# Result: 42 passed in 13.01s

# 2. Integrated pipeline and boundary regression suites
cd backend && uv run pytest tests/test_laya_escalation_slice5_1.py tests/test_contracts_and_identity.py tests/test_workspace_boundary_isolation.py tests/test_phase2.py tests/test_phase3.py tests/test_deterministic_pipeline_wiring.py tests/test_mandatory_audit_guards.py -q
# Result: 152 passed, 1 warning in 28.01s

# 3. Live runtime verification
cd backend && uv run python ../verify_0008_live.py
# Result: 20 passed, 0 failed
```

Zero paid provider calls made. All provider invocations, embeddings, and reranking use local deterministic doubles.
