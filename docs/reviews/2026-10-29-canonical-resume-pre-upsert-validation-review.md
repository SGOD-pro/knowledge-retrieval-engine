# Review, re-factor, 2026-10-29

**Reviewed by**: Gemini (Staff Software Engineer review)
**Scope**: 3 files, uncommitted
**Verdict**: Approve with nits

## Summary
This change introduces pre upsert validation and provider availability preflight checks for canonical Qdrant resume operations. It ensures that invalid vector dimensions, mismatched identifiers, fallback embeddings, and mixed model batches are rejected before any points are upserted to the vector store. The implementation is robust and backed by comprehensive regression tests across preflight checks, provider availability, and batch validation logic.

## Minor
### 🟡 Unused expected_point_map parameter in validate_batch_before_upsert, `backend/scripts/resume_canonical_qdrant_points.py:497`
**Problem**: In `validate_batch_before_upsert`, the parameter `expected_point_map` is declared in the signature and passed by callers, but never referenced in the function body. The test `test_validate_batch_before_upsert_identity_mismatch_rejected` sets up a dictionary mapping an invalid ID to a chunk, but the validation failure actually triggers because the point ID does not match the deterministic UUID5 hash of the chunk rather than checking membership in `expected_point_map`.
**Why it matters**: Accepting an unused parameter creates the impression that batch points are being checked against corpus wide point mappings. It also causes unit tests to pass for reasons different from what the author intended.
**Suggested fix**: Either verify that each point ID and chunk match an entry in `expected_point_map`, or remove `expected_point_map` from the function signature and callers.

### 🟡 Test framework mock introspection in production embedding provider, `backend/src/providers/embedding_provider.py:195`
**Problem**: In `embed_batch_with_provenance`, production code imports `unittest.mock.Mock` and branches at runtime using `isinstance(embed_batch, Mock)` and `isinstance(embed_text, Mock)` to return a synthetic `"mock"` model provenance string.
**Why it matters**: Production provider logic should not inspect test framework doubles or alter execution flow based on test mocks. This tightly couples production code to unit testing machinery, fails to intercept non Mock callables or fixtures, and necessitates special casing for `"mock"` strings in downstream validation logic.
**Suggested fix**: Remove `unittest.mock` imports and test double introspection from `embed_batch_with_provenance`. Unit tests requiring mock embeddings should patch `embed_batch_with_provenance` directly or use an explicit test provider.

### 🟡 Divergent fallback gating and duplicated worker pools in batch embedding, `backend/src/providers/embedding_provider.py:176`
**Problem**: The function `embed_batch` checks `if provider == "test" or (provider is None and is_test_env):` whereas `embed_batch_with_provenance` checks `if provider == "test" or is_test_env:`. Both functions construct independent `ThreadPoolExecutor` pools with identical worker counts and duplicate batch mapping logic.
**Why it matters**: Explicit `provider="prod"` parameters dispatched during test execution follow different code paths across the two functions. This creates unnecessary concurrency overhead and maintenance duplication.
**Suggested fix**: Refactor `embed_batch` to delegate directly to `embed_batch_with_provenance`, returning only the vector list.

## Nits
- ⚪ `backend/scripts/resume_canonical_qdrant_points.py:844`, Point counters `upserted_count` and `completed_batches` are not updated when all points land during a timeout and are recovered via read back, causing summary progress logs to under report landed points.
- ⚪ `backend/scripts/resume_canonical_qdrant_points.py:609`, Bedrock provider availability preflight confirms AWS STS caller identity and model configuration, but does not verify regional Bedrock API accessibility or model entitlement.
- ⚪ `backend/src/providers/embedding_provider.py:230`, The check `if len(vectors) != len(texts):` in `embed_batch_with_provenance` is unreachable because `executor.map` returns an output for every input item or raises an exception.

## Strengths
- The pre upsert validation gate enforces strict fail closed semantics, preventing corrupted vectors, fallback tags, or mismatched identities from reaching Qdrant.
- Provider availability preflight prevents starting paid resume runs when AWS dependencies or environment settings are invalid.
- Comprehensive regression tests in `backend/tests/test_resume_qdrant_preflight.py` exercise all failure branches, including dimension mismatches, fallback tags, missing model provenance, and provider failures.

## Test coverage
The entire backend test suite passed (563 passed, 3 skipped, 0 failed). The 13 new unit tests in `backend/tests/test_resume_qdrant_preflight.py` cover pre upsert batch validation, dimension verification, identity checks, provider availability checks, and execution model tracking.
