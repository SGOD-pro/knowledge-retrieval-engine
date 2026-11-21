# Review: LAYA Lexical Probe Stage Boundary Repaired and Assertions Confined

**Author**: Senior Software Engineer
**Date**: 2026-11-22
**Branch**: `re-factor`
**Inspected HEAD**: `d2a1d7a5de90f26e2b0799b7b5e495cb09d0d5d3`
**Supersedes**: Diagnosis in [2026-11-22-re-factor-0008-slice5-1-laya-lexical-scenario-repaired.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-22-re-factor-0008-slice5-1-laya-lexical-scenario-repaired.md)

## Summary

This review resolves the lexical probe fixture defects identified in Spec 0008 Slice 5.1. It restores the turnover question across all requests, establishes an explicit stage boundary in the synthetic harness repository, verifies pre dispatch cleanliness inside the network spy, inspects evidence survival through synthesis and verification inputs, and confines lexical success assertions to offline controls so live responses reach structured verdicts.

## Key Repairs

### 1. Consistent Restoration of Turnover Question
* Restored the query across all lexical requests to `"What was the turnover for fiscal year 2026?"`.
* Kept query, missing requirement (`"Initial fidelity check failed coverage: Missing turnover coverage"`), expected evidence (`chk_probe_turnover`), and expected answer (`"$450 million"`) consistent across direct endpoint and compiled pipeline requests.
* Preserved the route none refusal scenario on request 5 with query `"Unexpandable general concept with no evidence in existence"`.

### 2. Explicit Harness Stage Boundary Isolation
* Replaced keyword based reranker filtering with `StageBoundedRepository`, an explicit stage bounded harness repository.
* During the baseline discovery stage, the repository exposes only the corporate background chunk `chk_probe_baseline`. The novel chunk `chk_probe_turnover` is hidden from chunk lookups, vector searches, and in memory chunk registries.
* Calling `transition_to_expansion()` advances the repository to the expansion stage, reveals both baseline and novel chunks, and invalidates the chunk cache so the real expansion executor discovers the novel chunk.
* Subclassed singleton handling with an explicit `__new__` override so test runs across multiple test files never inherit stale repository instances.
* Kept real reranking and compression paths active with relevance sorting rather than destructive keyword removal.

### 3. Pre Dispatch Verification and Baseline Candidate Capture
* Inside `BoundedNetworkSpy.spy_post`, verified before any HTTP transmission that the serialized document context contains no answer text (`"$450 million"` or `"450 million"`) and no novel chunk ID (`chk_probe_turnover`).
* Captured actual baseline candidate IDs (`['chk_probe_baseline']`) directly from the stage boundary into `HttpAttemptRecord.baseline_candidate_ids`.
* Triggered stage boundary expansion immediately before mock or network response delivery so downstream pipeline execution can access novel evidence.

### 4. Post Expansion Evidence Survival Verification
* Added Positive Control 9 in offline controls asserting that novel evidence survives post expansion reranking and compression.
* Inspected inputs to `call_llm` to confirm that the synthesis call receives the turnover query and compressed context containing both `chk_probe_turnover` and `"$450 million"`.
* Inspected inputs to generative verifier `MockOfflineVerifier.verify` to confirm that the verifier receives the synthesized answer, compressed context with `"$450 million"`, and citations with `chk_probe_turnover`.
* Confirmed that the final pipeline response retains `chk_probe_turnover` in `citations` and contains `"$450 million"` in `answer`.

### 5. Live Scenario Failures Recorded into Structured Verdict
* Removed bare inline delivery assertions from the main probe loop in requests 3 and 4.
* Live responses returning none, timeout, or provider unavailable record into `rec_3` and `rec_4` without aborting execution.
* The evaluation engine `evaluate_probe_verdict` evaluates these records into structured verdicts with clear failure reasons, preserving test reporting integrity during live executions.

## Verification Evidence

```bash
# 1. Latency readiness probe offline dry run with all 9 controls and restored turnover question
cd backend && uv run python scripts/probe_laya_latency.py
# Result: 5/5 requests executed, 9 controls verified (8 negative + 1 positive), HARNESS PASS (exit code 0)

# 2. Dedicated probe regression suite including stage boundary, pre dispatch spy, and evidence survival tests
cd backend && uv run pytest tests/test_laya_latency_probe.py -v
# Result: 17 passed in 5.98s

# 3. Full LAYA suite (contracts, endpoint adapter, and latency probe)
cd backend && uv run pytest tests/test_laya_escalation_slice5_1.py tests/test_laya_endpoint_adapter.py tests/test_laya_latency_probe.py -v
# Result: 90 passed, 1 skipped in 17.58s
```
