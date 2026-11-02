# Review, re-factor, 2026-11-03

**Reviewed by**: Staff Software Engineer
**Scope**: OKF Slice 3.1 Large Publication and Same Version Republication Resolution
**Verdict**: Approve

## Summary

This review records the resolution and verification of the two confirmed publication defects in Open Knowledge Framework (OKF) Slice 3.1: large publication handling in DynamoDB and same version republication isolation in retrieval. It proves that large publications stage safely without exposing partial writes, that concurrent lease changes cancel unauthorized commits, and that stale records from prior publications never hide current assertions or consume bounded query limits.

## Previously Reported Results

Prior rounds established and verified:
* Bounded Bedrock extraction with maximum 2 calls, 4000 input tokens, and 1200 output tokens per document version.
* Atomic call and token ceiling enforcement across absent ledger and subsequent reservation attempts.
* Separate prompt and output ceilings preserved across restarts without integer division heuristics.
* Authoritative request token bounding via Bedrock Runtime `count_tokens` failing closed before dispatch.
* Deterministic publication race fencing preventing stale workers from exposing assertions after validation.
* DynamoDB Local container integration and multi process SQLite WAL execution.
* Authoritative readiness definition in `has_ready_okf` bound to exact document manifests using strongly consistent base table reads.
* Atomic rejection of unfenced writes attempting to overwrite active worker leases or published ready manifests.

## New Test Evidence and Verifications

This change resolves both confirmed defects across [`backend/src/schemas/contracts/okf.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/okf.py), [`backend/src/db/okf_repo.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), [`backend/src/services/retrieval/okf_retriever.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/okf_retriever.py), and [`backend/tests/test_okf_slice3_1.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_okf_slice3_1.py):

1. **Large DynamoDB publications staged safely**:
   - In [`DynamoDBOkfRepository.save_manifest`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L1780), publications containing 100 or more items (such as wide CSV tables with 105 columns yielding 107 stored records) stage payload items first into distinct publication scoped keys (`PUB#{pub_id}`) via `batch_writer`.
   - Visibility is switched atomically via a two item transaction executing `[condition_check, manifest_put]`.
   - The condition check validates that no active Bedrock extraction lease is held by another worker (`attribute_not_exists(lease_expires_at) OR lease_expires_at < :now_epoch`), while the manifest put action enforces status protections atomically.
   - If a batch write fails, or if a worker crashes before the transaction executes, the manifest is never committed or exposed as ready.
   - In `test_wide_csv_with_more_than_100_stored_items_staged_and_committed`, a wide CSV document with 105 assertions and 107 total items stages and commits successfully across InMemory, SQLite, and DynamoDB Local.
   - In `test_crash_or_batch_failure_before_publication_commit`, a simulated batch failure during payload staging leaves the database with zero ready state (`has_ready_okf` returns False, manifest returns None).
   - In `test_two_concurrent_publishers_and_lease_change_during_staging`, Worker A stages payload items, but Worker B acquires an active Bedrock extraction lease before Worker A commits. Worker A's transaction cancels with `TransactionCanceledException`, Worker A's manifest is never written, and Worker B's active lease remains intact on the budget ledger.
   - In `test_existing_ready_publication_remains_readable_during_failed_staging`, an existing ready publication remains readable and authoritative when a subsequent publication fails during staging across InMemory, SQLite, and DynamoDB Local.

2. **Same version republication isolation in retrieval**:
   - In [`OkfEntityRecord`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/okf.py#L65), [`OkfPropertyAssertion`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/okf.py#L82), and [`OkfManifest`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/okf.py#L106), a `publication_id` attribute tracks the unique publication token.
   - In [`DynamoDBOkfRepository`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L1820) and [`SQLiteOkfRepository`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L1005), assertion sort keys embed publication identifiers: `SK = ENTITY#{entity_id}#ASSERTION#PUB#{pub_id}#{assertion_id}`.
   - In [`OkfRetriever.lookup`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/okf_retriever.py#L215), retrieval reads the authoritative ready manifest, captures its active `publication_id`, filters candidate entities by active publication, and queries assertions with `publication_id=pub_id`.
   - In `get_assertions_for_entity`, queries use prefix `ENTITY#{entity_id}#ASSERTION#PUB#{publication_id}#`. Stale assertions from older publications reside under different prefixes and are completely skipped by DynamoDB range scans and SQLite queries.
   - In `test_same_version_republication_stale_records_exceeding_query_limit`, Publication A is committed with 25 assertions, followed by Publication B with 5 assertions for the exact same document version. Querying through the actual OKF retrieval pipeline returns exclusively Publication B assertions and zero Publication A assertions. Stale A records do not consume the bounded query limit or hide B facts across InMemory, SQLite, and DynamoDB Local.

3. **Deferred DynamoDB cleanup**:
   - Physical deletion of older publication items in DynamoDB is explicitly deferred.
   - Because deleting records across different partition and sort keys in DynamoDB is not atomic and could race with active readers, old records remain inert in the table.
   - They cannot match prefix queries scoped to newer publication tokens, preserving reader safety without publication path delays.

4. **Test execution summary**:
   - `tests/test_okf_slice3_1.py`: 42 passed in 10.42s
   - `tests/test_okf_slice1.py`: 19 passed in 0.52s
   - `tests/test_okf_ablation_slice2.py`: 11 passed in 0.65s
   - `tests/test_okf_correctness_regressions.py`: 29 passed in 2.21s
   - Combined OKF suite: 101 passed in 14.02s
   - CSV vertical slice: 5 passed in 11.37s

5. **Remaining limitations**:
   - Verification of DynamoDB conditional writes and transactions was executed against DynamoDB Local running in Docker. Live AWS DynamoDB and Bedrock runtime endpoints remain unverified in CI without live AWS credentials.
   - Asynchronous cleanup of inert DynamoDB publication records is deferred to future maintenance tasks.

6. **Specification status**:
   - Specification 0003 remains Proposed and deferred.
   - Specification 0007 remains Proposed and unimplemented.
