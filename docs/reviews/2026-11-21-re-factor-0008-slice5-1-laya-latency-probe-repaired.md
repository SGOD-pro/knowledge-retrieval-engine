# Review: LAYA Latency Probe Measurement and Verdict Logic Repaired

**Author**: Senior Software Engineer
**Date**: 2026-11-21
**Branch**: `re-factor`
**Supersedes**: Probe measurement and verdict approval in [2026-11-20-re-factor-0008-slice5-1-laya-latency-probe-finalized.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-20-re-factor-0008-slice5-1-laya-latency-probe-finalized.md)

## Summary

This review supersedes the previous latency probe approval where measurement isolation and verdict strictness were identified as gaps. All measurement, transmission, and verdict evaluation logic in [probe_laya_latency.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/probe_laya_latency.py) has been repaired and verified offline.

## Key Repairs

### 1. Per Attempt Records and Measurement Isolation
Previously, client roundtrip and server latency were stored in shared instance variables on `BoundedNetworkSpy`. A timeout or exception could permit subsequent requests to inherit stale timings from a previous successful request.
The repaired implementation introduces `HttpAttemptRecord`. Every HTTP transmission allocates its own attempt record storing start time, end time, roundtrip duration, server latency, status code, error, redirect flag, and late completion status.
Caller timeouts and exceptions record their own elapsed duration and set server latency to None. Late completions from background workers update their original attempt record without altering current or subsequent attempts.

### 2. Strict Verdict Evaluation Logic
Previously, delivery of answer text from deterministic fallback could be counted as successful delivery. Fallback delivery does not prove provider readiness.
The repaired verdict evaluator (`evaluate_probe_verdict`) enforces:
* Lexical queries: Must observe an accepted lexical route (`route_accepted`), executed lexical expansion (`expansion_executed`), zero fallback (`fallback == False`), and delivered outcome.
* Route none queries: Must observe an accepted none route (`route_accepted`), zero expansion (`expansion_executed == False`), zero fallback (`fallback == False`), and refusal outcome.
* Phase deadline protection: Every request must complete within the 2500.0 ms escalation phase limit. Overruns are classified as latency blockers.

### 3. Transmission Ceiling and Redirect Rejection
`BoundedNetworkSpy` enforces a strict ceiling of at most five HTTP transmissions across all attempts and retries. Redirects are rejected before untracked transmissions occur. Any 3xx redirect status code raises `RedirectRejectedError`, records the attempt, and causes the probe verdict to fail.

### 4. Explicit Assertions for Offline Harness Pass
The offline dry run mode no longer issues an unverified pass verdict. It executes end to end pipeline checks followed by five explicit negative controls:
* Control 1: Stale timeout metrics are rejected, verifying zero measurement inheritance.
* Control 2: Fallback only delivery is rejected, verifying that fallback cannot fake provider readiness.
* Control 3: Phase expiry over 2500ms is caught as a latency blocker.
* Control 4: HTTP redirects are caught and rejected.
* Control 5: Sixth transmission attempts are blocked by pre dispatch validation.
Only when all five negative controls pass does the probe output `HARNESS PASS; LIVE LATENCY READINESS UNKNOWN`.

## Test Evidence

```bash
# 1. Dedicated latency probe test suite (10 unit and regression tests)
cd backend && uv run pytest tests/test_laya_latency_probe.py -v
# Result: 10 passed in 3.89s

# 2. Bounded latency probe offline dry run
cd backend && uv run python scripts/probe_laya_latency.py
# Result: 5/5 requests executed, 5 negative controls verified, HARNESS PASS

# 3. LAYA endpoint adapter unit test suite
cd backend && uv run pytest tests/test_laya_endpoint_adapter.py -v
# Result: 26 passed, 1 skipped

# 4. Slice 5.1 escalation regression suite
cd backend && uv run pytest tests/test_laya_escalation_slice5_1.py -v
# Result: 42 passed
```
