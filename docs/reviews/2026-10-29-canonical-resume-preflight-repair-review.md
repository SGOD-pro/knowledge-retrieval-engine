# Review, re-factor, 2026-10-29

**Reviewed by**: Senior Code Reviewer
**Scope**: 2 files, uncommitted changes on branch `re-factor`
**Verdict**: Approve with nits

## Summary

This review assesses the uncommitted changes in [`backend/scripts/resume_canonical_qdrant_points.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py) and the newly added regression test suite [`backend/tests/test_resume_qdrant_preflight.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_resume_qdrant_preflight.py). The change replaces fragile direct SQLite path checks with durable boundary repository lookups, adds strict preflight provenance validation for DynamoDB chunk items, distinguishes between verified and unresolved model provenance in Qdrant points, and introduces robust post write verification. The implementation is clean, runs cleanly against the live services in dry run mode, and is accompanied by sixteen passing offline unit tests.

## Minor

### 🟡 Missing test coverage for error branches in preflight and post write verification, `backend/tests/test_resume_qdrant_preflight.py:128`
**Problem**: The new test suite in [`backend/tests/test_resume_qdrant_preflight.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_resume_qdrant_preflight.py) provides valuable unit test coverage for happy paths and primary failure modes, but omits several error handling branches. Specifically, [`verify_dynamodb_chunk_records`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L190) has an unexercised branch for unexpected chunk records, [`inspect_existing_qdrant_points`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L241) has an unexercised branch for unexpected point identifiers, and [`verify_qdrant_post_write`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L422) has unexercised failure branches for unexpected points, mismatched payload attributes, and invalid vector dimensions.
**Why it matters**: In critical resumption scripts, error paths that are not exercised in regression tests can silently fail or produce unexpected exceptions when corrupted data or unexpected point records appear in store.
**Suggested fix**: Add targeted test cases in [`backend/tests/test_resume_qdrant_preflight.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_resume_qdrant_preflight.py) to assert failure behavior when unexpected chunk records are returned from DynamoDB, when unexpected point records are returned from Qdrant scroll calls, and when post write verification detects unexpected point IDs, mismatched payload attributes, or corrupted vector dimensions.

### 🟡 Post write verification does not verify model provenance fields when unresolved provenance is disallowed, `backend/scripts/resume_canonical_qdrant_points.py:427`
**Problem**: When executed without `--allow-unresolved-provenance` (the default setting), the resume execution embeds missing and unverified points with explicit `fast_model` and `full_model` metadata in the upsert payload. However, [`verify_qdrant_post_write`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L427) only validates point identity, document ID, workspace ID, original ID, and vector dimensions (384 and 1024), without checking that the resulting points in the collection possess verified model provenance attributes.
**Why it matters**: If execution writes points or leaves existing records in place, post write verification will declare success even if records in the collection continue to lack verified model provenance metadata.
**Suggested fix**: Extend [`verify_qdrant_post_write`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L368) to accept a parameter indicating whether verified model provenance is required, and assert that `fast_model` and `full_model` are present in all verified records when unresolved provenance is disallowed.

### 🟡 Coercion of missing chunk ID attribute to string literal in DynamoDB verification, `backend/scripts/resume_canonical_qdrant_points.py:160`
**Problem**: In [`verify_dynamodb_chunk_records`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L123), line 160 executes `cid = str(item.get("id"))`. If an item returned from DynamoDB lacks an `id` attribute, Python `None` is converted to the string literal `"None"`, which is then stored in `found_chunk_ids` and reported in error logs.
**Why it matters**: Converting `None` to `"None"` produces confusing error messages such as `Chunk provenance mismatch for None` and can mask missing attribute schemas in stored DynamoDB items.
**Suggested fix**: Read `cid = item.get("id")` and explicitly raise a `RuntimeError` if `cid` is missing or empty before proceeding with provenance validation.

## Nits

* ⚪ [`backend/scripts/resume_canonical_qdrant_points.py:38`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L38), remove unused import [`Sequence`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L38) from the `typing` module.
* ⚪ [`backend/scripts/resume_canonical_qdrant_points.py:582`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L582), remove dead variable `upserted_count` which is incremented during batch iteration but never read or logged after the loop.

## Strengths

* Robust fail closed preflight design: The script resolves the durable boundary repository via [`get_workspace_boundary_repo`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L90) and validates source file SHA-256 prefixes against authoritative [`DocumentSnapshot`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/boundary.py#L92) records before attempting any store modifications.
* Strict provenance verification in DynamoDB: [`verify_dynamodb_chunk_records`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L123) performs paginated queries across all expected chunk IDs and validates workspace, document, and format provenance across every individual item.
* Principled model provenance handling: [`inspect_existing_qdrant_points`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L205) adheres to architecture guidelines by refusing to infer embedding model provenance from vector dimensions alone, accurately categorizing existing points as unresolved unless explicit model metadata exists in the payload.
* Resilient batch execution with read back recovery: Upserts use bounded batches with synchronous acknowledgement and exponential backoff. In the event of a timeout, [`retrieve_landed_point_ids`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L323) reads back committed points and verifies vector geometry and payload integrity before continuing.
* Clean offline regression test suite: The sixteen tests in [`backend/tests/test_resume_qdrant_preflight.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_resume_qdrant_preflight.py) run completely offline in under two seconds without mutating any shared state or making remote provider calls.

## Test coverage

* Test runner configuration: Configured with pytest in [`backend`](file:///home/swyra/projects/knowledge-retrieval-engine/backend).
* Test results: All 16 unit tests in [`backend/tests/test_resume_qdrant_preflight.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_resume_qdrant_preflight.py) pass.
* Coverage evaluation: Tests thoroughly cover version mismatch detection, missing snapshot errors, boundary repository resolution failures, DynamoDB read errors, missing chunk records, provenance mismatches, Qdrant unresolved vs verified provenance statuses, corrupted payload fields, corrupted vector dimensions, missing point detection, timeout read back validation, and post write verification success and missing point failures.
* Untested logic: Specific error branches for unexpected chunk records in DynamoDB, unexpected point IDs in Qdrant scroll calls, post write unexpected points, post write payload mismatches, post write vector dimension errors, and post write polling retry loops when `wait=False` remain without dedicated test cases as noted in Minor findings.
