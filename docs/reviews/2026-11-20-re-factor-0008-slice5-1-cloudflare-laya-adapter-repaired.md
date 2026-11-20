# Review, re-factor, 2026-11-20 (Cloudflare LAYA Adapter Validation and Request Semantics Repaired)

**Reviewed by**: claude-sonnet-4-6 (author on gemini)
**Scope**: 5 files, commit to be published on branch `re-factor`
**Verdict**: Approve

## Summary

This review supersedes the previous adapter verification review by confirming the resolution of three critical defects in the Cloudflare LAYA endpoint adapter:

1. **Raw Provider Usage Validation**: Eliminated client side usage smoothing (clamping, float casting, token defaulting). All provider usage dictionaries are preserved raw and validated against zero slack constraints. Missing fields, boolean values, fractional float counts, negative integers, and policy overruns (>1000 input tokens, >250 output tokens) are strictly rejected fail closed. Known spend is preserved in the budget ledger while recording the invalid usage event without double counting.
2. **Separate Field Budgeting and Preflight Trimming**: Mandatory request fields (user query, detected gap, missing dimension) are budgeted separately from optional context snippets. Optional snippets are trimmed first to ensure the total serialized payload strictly satisfies the <=980 byte target cap (<=1000 byte policy cap). If mandatory content alone cannot fit within the available document budget, escalation fails closed before HTTP dispatch with `rejected_by_code` rather than silently truncating or deleting its meaning. Multibyte UTF-8 characters and emojis are preserved safely without character slicing corruption.
3. **Domain Term Extraction and Operational Phrase Filtering**: Lexical expansion query parameters are derived exclusively from validated domain keywords extracted from the user query and detected gap. Generic operational error phrases (such as `initial`, `fidelity`, `check`, `failed`, `coverage`, `missing`, `error`, `runtime`, `traceback`) are strictly filtered out, preventing BM25 from searching operational logs instead of domain evidence.

## Key Verifications

### 1. Raw Usage Validation and Spend Preservation
* In [laya_gate.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/laya_gate.py), `validate_provider_usage` inspects raw token counts. It strictly rejects `bool` instances (which pass `isinstance(..., int)` in Python), `float` counts, negative values, and policy overruns.
* `LayaBudgetLedger` tracks `invalid_usage_calls` and updates settlement history without double counting spend.
* In [laya_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/laya_service.py), `_single_call` no longer clamps or defaults usage tokens, and `_spend_reconcile_cb` extracts known integer spend while leaving usage validation to `validate_provider_usage`.

### 2. Payload Field Budgeting and Fail Closed Preflight
* In [laya_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/laya_service.py), `_format_endpoint_payload` computes wrapper overhead dynamically.
* Mandatory content (`Query: ...\nGap detected: ...\nMissing dimension: ...`) is calculated first. If mandatory content exceeds the available document budget, it raises `PayloadBudgetExceededError`.
* In `decide()`, `PayloadBudgetExceededError` fails closed immediately with `self.last_dispatched = False`, `self.last_fallback = True`, and `execution_status = "rejected_by_code"`.
* Optional context snippets are trimmed to remaining bytes using `_trim_utf8_to_bytes`, which never truncates multibyte character boundaries.

### 3. Clean Domain Term Extraction
* In [laya_policy.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/laya_policy.py), `extract_domain_query_terms` prioritizes quoted terms and non operational domain keywords, filtering against `_GENERIC_OPERATIONAL_WORDS`.
* In [laya_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/laya_service.py), `_parse_endpoint_response` uses `extract_domain_query_terms` for lexical parameters.
* Both unit tests and compiled pipeline tests assert that executed BM25 queries contain domain keywords (such as `turnover`) and strictly exclude operational terms (`initial`, `fidelity`, `check`, `failed`, `coverage`).

## Test Evidence

```bash
# 1. Cloudflare LAYA endpoint adapter test suite (offline)
cd backend && uv run pytest tests/test_laya_endpoint_adapter.py -v
# Result: 26 passed, 1 skipped in 3.49s

# 2. Slice 5.1 regression suite
cd backend && uv run pytest tests/test_laya_escalation_slice5_1.py -v
# Result: 42 passed in 13.03s

# 3. Core contracts and workspace isolation suites
cd backend && uv run pytest tests/test_contracts_and_identity.py tests/test_workspace_boundary_isolation.py -v
# Result: 74 passed in 2.68s
```
