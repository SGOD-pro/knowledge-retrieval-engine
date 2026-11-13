# Review, re-factor, 2026-11-13

**Reviewed by**: Staff Software Engineer
**Scope**: OKF Slice 3.1 Publication Authority and Concurrent Commit Ordering Resolution
**Verdict**: Approve

## Summary

This review records the resolution and verification of publication authority, concurrent commit ordering, and entity pagination bounds in Open Knowledge Framework (OKF) Slice 3.1. It proves that concurrent unfenced CSV publishers enforce optimistic concurrency against base publication identifiers so stale staged attempts cannot replace committed publications, that fenced manifest publication atomically validates budget ledger existence, current fence token, and valid unexpired lease ownership, that budget reconciliation preserves active worker leases through manifest publication, and that DynamoDB entity resolution pagination enforces a strict 5 page safety bound failing closed to protect retrieval deadlines across Memory, SQLite, and DynamoDB Local.

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
* Safe staging of manifests with over 100 stored items and atomic visibility commit across InMemory, SQLite, and DynamoDB Local.
* Assertion level publication isolation returning only current ready publication facts upon same version republication.
* Distinct publication identity generation per attempt decoupled from the stable document version idempotency key.
* Entity resolution lookup query limit protection against stale records and legacy records lacking publication identifiers.

## New Test Evidence and Verifications

This change resolves publication authority and concurrent commit ordering across [`backend/src/schemas/contracts/okf.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/okf.py), [`backend/src/db/okf_repo.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), [`backend/src/ingestion/okf_builder.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py), [`backend/tests/test_okf_slice3_1.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_okf_slice3_1.py), and related components:

1. **Safe concurrent publication ordering and optimistic commit fencing**:
   - In [`backend/src/schemas/contracts/okf.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/okf.py), `OkfManifest` includes `base_publication_id: str | None = Field(default=None, max_length=128)`.
   - In [`OkfBuilder._extract_csv`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py), builder captures `base_pub_id = existing_man.publication_id if existing_man else None` prior to staging, assigns it to `manifest.base_publication_id`, and passes it to `save_manifest(..., base_publication_id=base_pub_id)`.
   - In [`OkfRepositoryBase`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), [`InMemoryOkfRepository`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), [`SQLiteOkfRepository`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), and [`DynamoDBOkfRepository`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), unfenced manifest writes enforce optimistic concurrency against `base_publication_id`. In DynamoDB, the condition requires `attribute_not_exists(PK)` when creating a fresh manifest, or `attribute_exists(PK) AND publication_id = :expected_base_pub` when replacing an existing publication.
   - In `test_concurrent_csv_publishers_paused_after_staging_cannot_replace_committed_dynamodb_local` and `test_concurrent_csv_publishers_paused_after_staging_cannot_replace_committed_sqlite_separate_connections`, Publisher A stages against the initial document state and pauses before commit. Publisher B commits a ready publication. When Publisher A resumes, its manifest commit is atomically rejected because the base publication changed. Publisher A fails open per ADR 001 with failed status in memory, and Publisher B remains authoritative and visible in retrieval.

2. **Fenced publication authority verification at write time**:
   - In [`DynamoDBOkfRepository.save_manifest`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), fenced writes enforce atomic condition check on the budget ledger item: `attribute_exists(PK) AND #ft = :expected_fence AND #lexp >= :now_epoch` plus `#lown = :wid` when worker identifier is provided.
   - Missing ledgers, expired leases, wrong worker owners, and superseded fence tokens are strictly rejected across InMemory, SQLite, and DynamoDB Local.
   - In `test_fenced_publication_authority_at_write_missing_expired_wrong_owner_stale_fence_dynamodb_local`, missing ledger, expired lease, incorrect lease owner, stale fence token, and fence advancement immediately prior to the transaction execution are rejected with RuntimeError.
   - In `test_fenced_publication_authority_at_write_sqlite_and_memory`, the same authority checks are validated across SQLite and InMemory implementations.

3. **Lease retention across budget reconciliation**:
   - In [`DynamoDBOkfRepository.reconcile_extraction_budget`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), [`SQLiteOkfRepository.reconcile_extraction_budget`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), and [`InMemoryOkfRepository.reconcile_extraction_budget`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), completed calls and spent tokens are recorded while retaining active lease ownership and unexpired lease expiration (`lease_owner_id` and `lease_expires_at`).
   - In `test_reconciliation_preserves_lease_and_allows_legitimate_publication`, workers reserve a budget, reconcile token spend, retain their authorized lease, and commit ready manifests and assertions successfully across DynamoDB Local, SQLite, and InMemory.

4. **Bounded entity resolution pagination failing closed**:
   - In [`DynamoDBOkfRepository.resolve_entities`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), GSI1 entity pagination is bounded to a maximum of 5 pages (`max_pages = 5`).
   - If 5 query pages are evaluated and additional records remain without satisfying the lookup limit, `resolve_entities` logs a warning and raises `StorageUnavailableError`.
   - In `test_dynamodb_stale_entity_pagination_safety_bound_fails_closed`, an entity with 60 stale records triggers the 5 page limit, raises `StorageUnavailableError`, and causes `OkfRetriever.lookup_sync` to fail closed with `execution_status='storage_failed'` and zero assertions returned, protecting retrieval deadlines.

5. **Test execution summary**:
   - `tests/test_okf_slice3_1.py`: 52 passed in 10.01s
   - `tests/test_okf_slice1.py`: 19 passed in 0.52s
   - `tests/test_okf_ablation_slice2.py`: 11 passed in 0.65s
   - `tests/test_okf_correctness_regressions.py`: 29 passed in 2.21s
   - Combined OKF test suite: 111 passed in 13.39s
   - Zero test failures, zero skips treated as passes.

6. **Remaining limitations**:
   - DynamoDB Local container was used for local AWS SDK expression verification. Live AWS cloud endpoints remain unverified in CI without live AWS credentials.
   - Deletion of inert DynamoDB publication records remains deferred to preserve reader safety.

7. **Specification status**:
   - Specification 0003 remains Proposed and deferred.
   - Specification 0007 remains Proposed and unimplemented.
