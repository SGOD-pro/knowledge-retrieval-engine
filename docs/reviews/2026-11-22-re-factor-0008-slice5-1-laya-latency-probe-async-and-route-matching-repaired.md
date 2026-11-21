# Review: LAYA Latency Probe Scenario Route Matching and Async Worker Isolation Repaired

**Author**: Senior Software Engineer
**Date**: 2026-11-22
**Branch**: `re-factor`
**Supersedes**: Probe measurement and verdict approval in [2026-11-21-re-factor-0008-slice5-1-laya-latency-probe-repaired.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-21-re-factor-0008-slice5-1-laya-latency-probe-repaired.md)

## Summary

This review supersedes the previous latency probe review by addressing three remaining probe harness findings. All repairs have been implemented and verified offline in [probe_laya_latency.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/probe_laya_latency.py) and [test_laya_latency_probe.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_laya_latency_probe.py).

## Key Repairs

### 1. Actual Observed Provider Route Scenario Matching and Missing Telemetry Rejection
Previously, missing route telemetry could be masked by defaulting logic, and route mismatch during none scenarios was not strictly rejected if the pipeline refused on its own.
The repaired verdict logic in `evaluate_probe_verdict` strictly enforces:
* Provider route telemetry presence: Any empty string, unknown, or None route telemetry fails validation with code 2.
* Scenario matching for lexical expansion: Observed provider route must explicitly equal `lexical_expansion`.
* Scenario matching for route none: Observed provider route must explicitly equal `none`. If a provider returns `lexical_expansion` during a route none scenario, the verdict fails with code 2 even if downstream pipeline synthesis refuses.

### 2. Caller Timeout Separation and Originating Probe Request Association
`HttpAttemptRecord` and `BoundedNetworkSpy` have been enhanced to isolate caller timeouts from subsequent worker completion:
* Attempts are explicitly tagged with `probe_id` via `spy.set_probe_id(probe_id)`.
* When a caller times out before HTTP roundtrip completes, `spy.record_caller_timeout(probe_id)` records `caller_timed_out = True` and timestamp without blocking for worker completion.
* When the background worker eventually completes, `attempt.ended_at` and `roundtrip_ms` are recorded, and `attempt.late_completed` is set to True automatically.
* The original caller decision status (`timeout`) is strictly preserved and not overwritten by late HTTP completion.

### 3. Event Controlled Asynchronous Worker Testing Without Manual Population
In both offline negative controls and dedicated unit tests, manual population of late completion fields was eliminated. Testing is driven by explicit threading events:
* `on_worker_start` event notification triggers when the worker begins dispatch.
* `pause_event` pauses the worker inside HTTP execution.
* The caller executes with active phase budget and times out naturally in 20 ms.
* A subsequent request runs immediately with separate probe identifier and completes successfully.
* Releasing `pause_event` allows the original worker to complete naturally, populating `ended_at`, `roundtrip_ms`, and `late_completed` automatically through real worker execution.

## Verification Evidence

```bash
# 1. Bounded latency probe offline dry run with all 8 negative controls
cd backend && uv run python scripts/probe_laya_latency.py
# Result: 5/5 probe requests executed, 8 negative controls verified, HARNESS PASS (exit code 0)

# 2. Dedicated latency probe regression test suite
cd backend && uv run pytest tests/test_laya_latency_probe.py -v
# Result: 12 passed in 5.98s

# 3. Full LAYA escalation and adapter regression suite
cd backend && uv run pytest tests/test_laya_endpoint_adapter.py tests/test_laya_escalation_slice5_1.py tests/test_laya_latency_probe.py -v
# Result: 84 passed, 1 skipped in 16.64s
```
