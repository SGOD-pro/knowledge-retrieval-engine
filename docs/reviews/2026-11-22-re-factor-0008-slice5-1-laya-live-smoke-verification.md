# Verification Record: Bounded Live LAYA Latency Smoke

**Author**: Senior Software Engineer
**Date**: 2026-11-22
**Branch**: `re-factor`
**Inspected HEAD**: `88a45d155b4e4ea9bb8686335ea737dccb6c3170`
**Working Tree Status**: Clean

## Summary

This record captures the sanitized results of running one single live bounded latency smoke check against the active Cloudflare hosted LAYA System One endpoint (`https://missed-catalog-tub-renewal.trycloudflare.com/v1/systemone`). Exactly five HTTP transmissions were executed across the two direct endpoint probes and three compiled LangGraph pipeline probes. Zero automatic reruns were executed, and all non LAYA model providers remained mocked.

Overall verdict: **FAIL (FUNCTIONAL DEFECT)** (exit code 2).
While all five transmissions satisfied the 2500 ms escalation phase deadline (timing passed), the live LAYA model classified the turnover retrieval gap queries as route `none` rather than `lexical_expansion`. Consequently, lexical expansion did not execute in the compiled pipeline, producing clean refusals instead of delivery.

## Preflight Confirmation

* **Tunnel and Endpoint Status**: Reachable and active. Preflight connection check returned HTTP 404 at the host root, confirming the Cloudflare tunnel was forwarding traffic to the backend service.
* **Credentials Configuration**: Verified as configured and non empty. Exact credentials were preserved private and redacted from all logs and records.
* **Transmission Ceiling**: Enforced at five requests before dispatch. Exactly five HTTP requests were sent.

## Probe Execution Evidence

Command executed:
```bash
cd backend && ENVIRONMENT=test MODEL_PROVIDER=test uv run python scripts/probe_laya_latency.py --live
```

Exit code: 2

### Observable Timing Breakdown

* First request average client roundtrip: 748.9 ms
* Subsequent requests average client roundtrip: 709.7 ms
* Average reported server model latency: 54.6 ms
* Average subsequent unaccounted elapsed time: 655.1 ms
* Phase deadline limit: 2500.0 ms hard ceiling
* Phase deadline status: All five requests completed well within the 2500.0 ms deadline

### Detailed Scenario Results

#### Section 1: Direct Endpoint Decision Service

* **Request 1: Direct Endpoint (First Request, Lexical Intent)**
  * Query: "What was the turnover for fiscal year 2026?"
  * Gap: "Initial fidelity check failed coverage: Missing turnover coverage"
  * Target: `direct_endpoint`
  * Expected route: `lexical_expansion`
  * Actual observed route: `none` (model: `laya-rl-agent`, answer confidence: 0.9354, choice confidence: 0.45)
  * Route accepted by code: Yes (`none` is an allowed route in Slice 5.1 code validation)
  * Expansion executed: No (direct decision service only)
  * Fallback: No (provider returned valid JSON)
  * Caller timing (client roundtrip): 1033.91 ms
  * Server reported latency: 54.94 ms
  * Unaccounted time: 979.0 ms
  * Caller timeout: No
  * Final decision status: `approved`
  * Conformance check: FAILED (observed route `none` did not match expected `lexical_expansion`)

* **Request 2: Direct Endpoint (Subsequent Request, default client behavior)**
  * Query: "What was the turnover for fiscal year 2026?"
  * Gap: "Initial fidelity check failed coverage: Missing turnover coverage"
  * Target: `direct_endpoint`
  * Expected route: `lexical_expansion`
  * Actual observed route: `none` (model: `laya-rl-agent`, answer confidence: 0.9354, choice confidence: 0.45)
  * Route accepted by code: Yes
  * Expansion executed: No
  * Fallback: No
  * Caller timing (client roundtrip): 1007.57 ms
  * Server reported latency: 54.31 ms
  * Unaccounted time: 953.3 ms
  * Caller timeout: No
  * Final decision status: `approved`
  * Conformance check: FAILED (observed route `none` did not match expected `lexical_expansion`)

#### Section 2: Compiled LangGraph Pipeline End to End

* **Request 3: Compiled Pipeline (First Pipeline Request, Lexical Expansion Intent)**
  * Query: "What was the turnover for fiscal year 2026?"
  * Target: `compiled_pipeline`
  * Expected route: `lexical_expansion`
  * Actual observed route: `none`
  * Route accepted by code: Yes
  * Expansion executed: No (suppressed because model decided route `none`)
  * Fallback: No
  * Caller timing (LayaCall roundtrip): 463.90 ms
  * Pipeline total time: 505.37 ms
  * Caller timeout: No
  * Synthesis calls: 0
  * Final pipeline outcome: `REFUSAL` (answer: NOT_FOUND)
  * Conformance check: FAILED (expected `lexical_expansion` and `DELIVERED`, observed `none` and `REFUSAL`)

* **Request 4: Compiled Pipeline (Subsequent Pipeline Request, Lexical Expansion Intent)**
  * Query: "What was the turnover for fiscal year 2026?"
  * Target: `compiled_pipeline`
  * Expected route: `lexical_expansion`
  * Actual observed route: `none`
  * Route accepted by code: Yes
  * Expansion executed: No
  * Fallback: No
  * Caller timing (LayaCall roundtrip): 568.39 ms
  * Pipeline total time: 582.55 ms
  * Caller timeout: No
  * Synthesis calls: 0
  * Final pipeline outcome: `REFUSAL` (answer: NOT_FOUND)
  * Conformance check: FAILED (expected `lexical_expansion` and `DELIVERED`, observed `none` and `REFUSAL`)

* **Request 5: Compiled Pipeline (Subsequent Pipeline Request, Route None Refusal Intent)**
  * Query: "Explain the philosophy of business in general"
  * Target: `compiled_pipeline`
  * Expected route: `none`
  * Actual observed route: `none`
  * Route accepted by code: Yes
  * Expansion executed: No
  * Fallback: No
  * Caller timing (LayaCall roundtrip): 553.08 ms
  * Pipeline total time: 574.13 ms
  * Caller timeout: No
  * Synthesis calls: 0
  * Final pipeline outcome: `REFUSAL` (answer: NOT_FOUND)
  * Conformance check: PASSED (observed route `none` and clean refusal matched expected behavior)

## Findings and Next Steps

1. **Latency Ceiling Passed**: The Cloudflare tunnel roundtrip latency (463 ms to 1034 ms) easily satisfied the 2500 ms SLA. No phase overruns occurred.
2. **Provider Route Selection Defect**: The live LAYA model policy (`laya-rl-agent`) selected route `none` (with confidence 0.9354) for the missing turnover retrieval gap instead of `lexical_expansion`. Because the model chose `none`, the engine correctly refrained from executing lexical expansion and issued a clean refusal. The failure is an external model decision misalignment, not a client side timing or serialization error.
3. **Statistical Sample Limitation**: Five samples provide smoke verification and sanity checks only. They do not demonstrate long term operational reliability, tail latency percentiles (such as p95 or p99), or model classification accuracy across varied domains.
