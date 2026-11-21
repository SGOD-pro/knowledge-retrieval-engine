# Review: LAYA Lexical Smoke Scenario Repaired and Diagnostic Follow-up

**Author**: Senior Software Engineer
**Date**: 2026-11-22
**Branch**: `re-factor`
**Inspected HEAD**: `e06cd46cb7891a2a3cbb2a6a53360e02fbdbcb75`
**Supersedes**: Diagnosis in [2026-11-22-re-factor-0008-slice5-1-laya-live-smoke-verification.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-22-re-factor-0008-slice5-1-laya-live-smoke-verification.md)

## Summary

This review resolves the harness defect identified during live smoke testing where baseline discovery exposed the answer chunk in serialized context before escalation occurred. It captures the actual serialized payload produced by the compiled pipeline offline, isolates baseline evidence, and adds explicit positive assertions for lexical retrieval and grounded delivery.

## Separation of Reproduced Harness Defect from Live Model Behavior

### 1. Reproduced Harness Defect (Confirmed Evidence)
In the original probe harness configuration:
* The query in compiled pipeline requests 3 and 4 asked directly for turnover: `"What was the turnover for fiscal year 2026?"`.
* The synthetic document in [probe_laya_latency.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/probe_laya_latency.py) contained both the background overview chunk and the novel turnover chunk.
* During baseline discovery before escalation, BM25 keyword matching and vector search matched `"turnover"` and retrieved `chk_probe_turnover` into baseline candidate chunks.
* The escalation serializer summarized baseline candidates into `state.document`, placing `"Context: The fiscal year 2026 turnover was officially confirmed as $450 million."` directly into the serialized prompt dispatched to System One.
* Consequently, the harness asked System One to resolve a missing turnover gap while simultaneously providing the exact turnover answer in the baseline context.
* When downstream executor expansion was tested, `chk_probe_turnover` was already present in `baseline_chunks`, which caused `LayaExpansionExecutor` to treat it as non novel and return `no_progress = True`.

### 2. Hypotheses About Live Model Behavior (Distinct from Harness Defect)
In the live smoke test where requests 1 through 4 returned route `none`:
* For compiled pipeline requests 3 and 4, the model observed the answer in the baseline context and correctly selected route `none` because no further retrieval expansion was needed.
* For direct requests 1 and 2 where context was limited to background metrics, the live model still selected route `none` with high confidence. The question criterion for route `none` stated `"No expansion needed or evidence fundamentally missing"`. It is hypothesized that the live model interpreted the lack of baseline turnover mentions as fundamental evidence absence rather than a retrieval gap requiring expansion.
* This hypothesis remains unproven without live model evaluation, but the harness contamination defect is conclusively confirmed and repaired.

## Key Repairs

### 1. Actual Serialized Payload Capture Offline
* `HttpAttemptRecord` now contains `request_payload: dict[str, Any]` to record the exact dictionary dispatched over HTTP.
* `BoundedNetworkSpy.spy_post` captures `kwargs.get("json")` directly into the attempt record on every dispatch.
* The compiled pipeline payload is captured directly from real pipeline execution offline with zero manual reconstruction.

### 2. Baseline Evidence Isolation in Lexical Scenario
* Baseline discovery query is configured to request corporate background: `"What was the corporate background for 2026?"`.
* `_setup_synthetic_repository` separates corporate background text (`chk_probe_baseline`) from novel turnover text (`chk_probe_turnover`).
* `_mock_rerank` exposes only background evidence during baseline discovery when the query does not ask for turnover.
* The answer chunk `chk_probe_turnover` remains in the in memory repository and active snapshot context, ready for targeted retrieval during LAYA expansion.

### 3. Explicit Pre Dispatch and Post Expansion Assertions
Before provider dispatch in requests 3 and 4, the harness strictly asserts:
* `chk_probe_turnover` is absent from baseline candidates in `retrieval_candidates["reranked"]`.
* `chk_probe_turnover` is absent from the serialized context in `att.request_payload["state"]["document"]`.
* `"$450 million"` and `"450 million"` are absent from the serialized context.

After mocked lexical approval, the harness strictly asserts:
* `chk_probe_turnover` is retrieved by `LayaExpansionExecutor` as a novel chunk in `retrieval_candidates["laya_novel_chunks"]`.
* `laya_expansion_executed` is True.
* Grounded delivery succeeds with `delivery_outcome == "DELIVERED"`.
* The final synthesis answer contains `"$450 million"`.

### 4. Preservation of Existing Invariants
* Negative control for route none and clean refusal in request 5 is preserved.
* The five transmission maximum ceiling is strictly enforced.
* Per attempt measurement tracking and late completion accounting are fully maintained.
* Zero credentials logged or printed, and zero live paid calls executed.

## Verification Evidence

```bash
# 1. Latency readiness probe offline dry run with all negative controls and new assertions
cd backend && uv run python scripts/probe_laya_latency.py
# Result: 5/5 requests executed, 8 negative controls verified, HARNESS PASS (exit code 0)

# 2. Dedicated probe regression suite including payload capture and lexical isolation tests
cd backend && uv run pytest tests/test_laya_latency_probe.py -v
# Result: 13 passed in 6.02s

# 3. Full LAYA suite (contracts, endpoint adapter, and latency probe)
cd backend && uv run pytest tests/test_laya_escalation_slice5_1.py tests/test_laya_endpoint_adapter.py tests/test_laya_latency_probe.py -v
# Result: 86 passed, 1 skipped in 16.56s
```
