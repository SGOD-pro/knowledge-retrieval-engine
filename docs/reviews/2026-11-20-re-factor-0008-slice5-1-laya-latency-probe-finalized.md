# Report: Bounded LAYA Latency Readiness Probe Finalized

**Author**: Senior Software Engineer
**Date**: 2026-11-20
**Branch**: `re-factor`
**Target Commit**: HEAD on `re-factor`

## Overview

This report documents the finalized diagnostic tooling for assessing Cloudflare LAYA endpoint latency under Spec 0008 Slice 5.1. The probe script [probe_laya_latency.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/probe_laya_latency.py) has been finalized, validated offline, committed, and published.

## Implementation Details

### 1. Dry Run Labeling and Honest Latency Reporting
Dry run executions use simulated offline doubles and print the explicit verdict:
`HARNESS PASS; LIVE LATENCY READINESS UNKNOWN`
The probe never claims or infers network readiness from simulated offline timings. Live readiness can only be determined through explicit opt-in execution against a running endpoint.

### 2. Request Sequence Separation and Connection Semantics
The probe distinguishes between:
* First request: Subject to initial socket creation, DNS resolution, and TLS handshake.
* Subsequent requests: Executed using standard client behavior.
* Production connection behavior: Production `LayaDecisionService` issues independent HTTP POST calls using `requests.post`. Persistent HTTP connection reuse is not verified across calls, and production connection semantics are preserved without artificial pooling.

### 3. Unaccounted Elapsed Time Attribution
Client roundtrip minus reported server execution time is strictly labeled `unaccounted elapsed time` (`unaccounted_elapsed_ms`). The tooling avoids attributing this delta specifically to Cloudflare, TLS handshakes, or DNS lookups without network packet captures and telemetry evidence.

### 4. Hard Pre Dispatch Request Ceiling
A pre dispatch validation check in `BoundedNetworkSpy` guarantees that at most five HTTP requests are transmitted in any probe run, including retries. Any invocation attempting a sixth request is blocked with a runtime exception before network dispatch.

### 5. Preserved Phase Deadlines and Failure Handling
The probe enforces the strict 2500.0 ms escalation phase deadline across all requests. It records client roundtrip, server reported latency, unaccounted elapsed time, dispatch status, valid response parsing, proposed route, accepted route, deterministic fallback status, and final delivery outcomes.

### 6. Synthetic Isolation and Zero Credential Leakage
* The probe runs against an in memory synthetic document repository with zero access to disk corpus or ground truth files.
* Synthesis, embedding, reranking, and verification use local offline doubles to prevent paid model invocation.
* Authentication credentials are read from environment variables or command arguments, and credential masking outputs `[CONFIGURED: REDACTED]` or `[NOT CONFIGURED]`, printing zero key fragments.

## Test Verification

```bash
# 1. Default offline preparation (dry run)
cd backend && uv run python scripts/probe_laya_latency.py
# Result: 5/5 requests executed, HARNESS PASS; LIVE LATENCY READINESS UNKNOWN

# 2. LAYA endpoint adapter unit test suite
cd backend && uv run pytest tests/test_laya_endpoint_adapter.py -v
# Result: 26 passed, 1 skipped

# 3. Slice 5.1 escalation regression suite
cd backend && uv run pytest tests/test_laya_escalation_slice5_1.py -v
# Result: 42 passed
```

## Opt In Live Execution

To measure live network latency against the running LAYA service, execute:

```bash
cd backend && uv run python scripts/probe_laya_latency.py --live
```

### Statistical Reliability Notice
Five requests provide operational boundary validation and detect catastrophic latency blockages (such as recurring timeouts exceeding 2500 ms). Five samples do not provide statistical reliability or high percentile (p95/p99) guarantees.
