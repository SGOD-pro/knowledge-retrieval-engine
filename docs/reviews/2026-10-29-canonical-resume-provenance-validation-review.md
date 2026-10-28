# Review, re-factor, 2026-10-29

**Reviewed by**: Gemini (Staff Engineer review)
**Scope**: 4 files, uncommitted
**Verdict**: Approve with nits

## Summary
This change establishes strict model provenance validation for canonical Qdrant points during ingestion and resume preflight. It replaces hardcoded model identifiers with runtime provenance captured from embedding providers, rejecting deterministic fallback vectors from landing or passing verification. The implementation is clean, fail closed, and supported by thorough regression tests across preflight inspection, timeout read back, and post write verification.

## Minor
### 🟡 Inconsistent test environment fallback gating in embed_batch, `backend/src/providers/embedding_provider.py:165`
**Problem**: In `backend/src/providers/embedding_provider.py`, `embed_batch` checks `if provider == "test" or (provider is None and is_test_env):` while `embed_batch_with_provenance` checks `is_fallback = provider == "test" or is_test_env` and `embed_text` checks `if provider == "test" or is_test_env:`. When a caller passes an explicit provider such as `provider="prod"` during test execution, `embed_batch` does not take the synchronous fallback branch. Instead, it creates a `ThreadPoolExecutor` and dispatches workers to call `embed_text`, which then immediately returns deterministic vectors.
**Why it matters**: Spinning up worker threads to compute deterministic fallback vectors adds unnecessary concurrency overhead in test runs and leaves fallback gating logic inconsistent across the module.
**Suggested fix**: Align the branch condition in `embed_batch` with `embed_text` by checking `if provider == "test" or is_test_env:` directly.

## Nits
- ⚪ `backend/tests/test_resume_qdrant_preflight.py:428`, Remove superfluous consecutive blank lines preceding `test_verify_qdrant_post_write_missing_point_fails`.
- ⚪ `backend/scripts/resume_canonical_qdrant_points.py:644`, Payload construction omits `fast_model` and `full_model` keys entirely if metadata values are absent. Storing explicit fallback identifiers or unknown tags would keep the point payload schema deterministic.

## Strengths
- Runtime model provenance is tracked directly from producing functions rather than inferred from vector dimensions or hardcoded in upsert payloads.
- Fail closed verification prevents deterministic fallback embeddings from silently contaminating the canonical Qdrant collection during batch upserts and timeout recovery read back.
- Excellent regression test suite addition in `backend/tests/test_resume_qdrant_preflight.py` validating corrupted model provenance, fallback rejection, and post write assertions.

## Test coverage
The backend test suite was executed in full (552 items, 549 passed, 3 skipped, 0 failed). The 8 new regression tests in `backend/tests/test_resume_qdrant_preflight.py` exercise each provenance validation branch: incompatible fast model tag, incompatible full model tag, deterministic fallback tag in preflight inspection, dual embedding metadata recording, test mode rejection as Titan, post write verification failure, and timeout recovery rejection.
