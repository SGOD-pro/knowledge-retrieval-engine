# Review, re-factor, 2026-11-20 (Cloudflare LAYA Adapter Verified)

**Reviewed by**: claude-sonnet-4-6 (author on gemini)
**Scope**: 8 files, commit to be published on branch `re-factor`
**Verdict**: Approve

## Summary

This review confirms verification of the Cloudflare hosted LAYA endpoint adapter for Spec 0008 Slice 5.1.

Historical reviews are preserved. This disposition verifies that LAYA routing decisions can be executed either against the Cloudflare hosted LAYA System One endpoint or against local test doubles with explicit provider identity and zero Bedrock fallback.

The adapter enforces exact System One request and response compatibility (`state.document`, `questions.route`, `answers.route`), strict numeric confidence validation, authoritative input preflight bounding (strictly 1000 input tokens or payload byte cap), shared absolute deadline propagation, concurrency controller capacity retention, and late spend settlement.

Telemetry explicitly distinguishes whether LAYA was configured, dispatched, whether a valid provider response was returned, whether the proposed route was accepted by code validation, whether expansion executed, and whether deterministic fallback was used. In particular, `laya_invoked` is set only upon actual provider dispatch and is never hardcoded true before dispatch.

## Key Verifications

### 1. System One Contract Compatibility and Zero Bedrock Fallback
* In [laya_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/laya_service.py), `_format_endpoint_payload` maps `LayaEscalationRequest` into `/v1/systemone` format with `state.document` and `questions.route`.
* `_parse_endpoint_response` parses choice and confidence from `answers.route`. Missing, non numeric, or boolean confidence is rejected.
* When `endpoint_url` is configured, `self.client` is explicitly set to `None`, guaranteeing zero Bedrock fallback if the endpoint is unavailable.
* Endpoint connection errors and HTTP 502/503 errors trigger `provider_unavailable` telemetry and deterministic Section 35 fallback.

### 2. Authoritative Input Bounding Before HTTP Dispatch
* `_format_endpoint_payload` dynamically constrains `state.document` so the complete serialized JSON payload remains strictly below 1000 bytes (defensible token upper bound).
* Authoritative token preflight enforces the 1000 token cap before any HTTP dispatch occurs.

### 3. Shared Absolute Deadline and Capacity Retention
* A simulated slow 4.4 second network call exceeding the 2500ms phase allowance was tested offline.
* The caller times out promptly at the phase deadline without hanging for 4.4 seconds.
* The concurrency controller retains the worker slot while the background call runs.
* Upon late completion, late results are discarded and spend is reconciled cleanly into the budget ledger.

### 4. Telemetry Distinctions
* Telemetry in `retrieval_candidates` records:
  - `laya_configured`: True when LAYA is enabled in configuration
  - `laya_dispatched`: True only when an HTTP request was dispatched
  - `laya_invoked`: True only when an HTTP request was dispatched
  - `laya_valid_response`: True when the endpoint returned valid JSON with valid route and confidence
  - `laya_provider_success`: True when valid provider response was received
  - `laya_route_accepted`: True only when the proposed route is approved in Slice 5.1 (lexical_expansion or none)
  - `laya_expansion_executed`: True when novel chunks were retrieved and added to evidence
  - `laya_fallback`: True when deterministic fallback was used instead of an accepted route
  - `laya_provider`: "endpoint" when using the HTTP endpoint, "bedrock" when using Bedrock

### 5. Performance and Latency Readiness Finding
* Live smoke check against the running Cloudflare tunnel confirmed System One response compatibility (`choice: "none"`, `confidence: 0.476`, `answer_confidence: 0.7413`, model `"laya-rl-agent"`).
* The Cloudflare trycloudflare tunnel roundtrip measured at 4409.25ms (server latency was 50.86ms).
* Because 4409ms exceeds KRE's 2500ms escalation phase deadline ceiling, this constitutes a known latency readiness blocker for benchmark evaluation until the server is co-located or deployed behind a stable, low latency tunnel.

## Test Evidence

```bash
# 1. Cloudflare LAYA endpoint adapter test suite (offline)
cd backend && uv run pytest tests/test_laya_endpoint_adapter.py -v
# Result: 15 passed, 1 skipped in 3.19s

# 2. Slice 5.1 regression suite
cd backend && uv run pytest tests/test_laya_escalation_slice5_1.py -v
# Result: 42 passed in 12.74s

# 3. Combined LAYA test suites
cd backend && uv run pytest tests/test_laya_endpoint_adapter.py tests/test_laya_escalation_slice5_1.py
# Result: 57 passed, 1 skipped in 13.81s
```
