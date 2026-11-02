# Review, re-factor, 2026-11-03

**Reviewed by**: Staff Software Engineer
**Scope**: OKF Slice 3.1 Production Publication Identity and Retrieval Isolation Resolution
**Verdict**: Approve

## Summary

This review records the resolution and verification of publication identity generation and entity resolution retrieval isolation in Open Knowledge Framework (OKF) Slice 3.1. It proves that real CSV and PDF builder pipelines generate distinct publication identities for every extraction attempt separate from the stable document version idempotency key, that staged payload writes never write into keys used by existing ready publications, that failure during subsequent staging attempts preserves previous ready publications intact and readable, and that stale entities never consume bounded entity resolution limits before active publication filtering across Memory, SQLite, and DynamoDB Local.

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

## New Test Evidence and Verifications

This change resolves production publication identity generation and entity lookup isolation across [`backend/src/schemas/contracts/okf.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/okf.py), [`backend/src/db/okf_repo.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), [`backend/src/db/database.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/database.py), [`backend/src/ingestion/okf_builder.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py), [`backend/src/services/retrieval/okf_retriever.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/okf_retriever.py), and [`backend/tests/test_okf_slice3_1.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_okf_slice3_1.py):

1. **Distinct publication identity generation per attempt**:
   - In [`backend/src/schemas/contracts/okf.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/okf.py#L66), `generate_publication_id() -> str` generates a distinct publication identity (`pub_{uuid4().hex[:16]}`) for each attempt.
   - In [`OkfBuilder.extract_and_persist`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1015), each extraction execution derives a fresh `attempt_pub_id` and propagates it to `_extract_csv` and `_extract_pdf`.
   - The stable document version idempotency key (`idempotency_key = compute_idempotency_key(ws, doc, ver)`) remains decoupled from the physical publication identity, ensuring that retries and force extractions never overwrite keys of existing publications during staging.
   - In `test_builder_two_attempts_distinct_publication_ids_and_retrieval`, two builder extraction calls for the same document version with zero manually assigned publication IDs produce identical idempotency keys but distinct publication IDs. Retrieval queries isolate exclusively to attempt 2 facts across InMemory, SQLite, and DynamoDB Local.

2. **Staging protection and failure fail open behavior**:
   - Because staging keys include `#PUB#{pub_id}`, payload items for attempt 2 are staged into isolated keys.
   - In [`OkfBuilder._extract_csv`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1211), storage failures during manifest saving fail open per ADR 001, returning a failed status manifest in memory rather than falsely reporting readiness.
   - In `test_large_csv_staging_failure_leaves_first_publication_intact`, an existing ready publication containing 105 columns remains completely readable and authoritative after a subsequent large CSV attempt fails during payload staging across SQLite and DynamoDB Local.

3. **Entity resolution query limit protection**:
   - In [`OkfRepositoryBase`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L95), [`InMemoryOkfRepository`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L330), [`SQLiteOkfRepository`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L1065), and [`DynamoDBOkfRepository`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L2005), `resolve_entities` accepts `active_document_versions` and `active_publications`.
   - In DynamoDB, `resolve_entities` paginates via `ExclusiveStartKey`, evaluating and skipping stale entities (older versions, superseded publications, and legacy records without publication identifiers) before incrementing matched results toward the bounded `limit`.
   - In SQLite, cursor evaluation iterates over GSI1 matches, filtering stale entities before counting toward `limit`.
   - In `test_stale_entity_records_exceeding_limit_do_not_hide_current_entity`, 15 stale entity records (exceeding the lookup limit of 10), including 5 older version records, 5 superseded publication records, and 5 legacy records without publication IDs, do not hide the active entity. Entity resolution resolves the current entity and retrieval returns active assertions across InMemory, SQLite, and DynamoDB Local.

4. **Concurrent publishers, boundary tombstones, and isolation**:
   - In `test_concurrent_publishers_tombstones_and_isolation`, Worker 2 supersedes Worker 1's fence token, Worker 2 publishes successfully, and Worker 1's attempt to save with the stale fence is rejected with RuntimeError.
   - Immediate boundary tombstones suppress both entity resolution and assertion retrieval.
   - Cross workspace and cross version lookups remain completely isolated across InMemory, SQLite, and DynamoDB Local.

5. **Test execution summary**:
   - `tests/test_okf_slice3_1.py`: 46 passed in 8.29s
   - `tests/test_okf_slice1.py`: 19 passed in 0.52s
   - `tests/test_okf_ablation_slice2.py`: 11 passed in 0.65s
   - `tests/test_okf_correctness_regressions.py`: 29 passed in 2.21s
   - Combined OKF suite: 105 passed in 11.67s

6. **Remaining limitations**:
   - DynamoDB Local container was used for real AWS SDK expression verification. Live AWS cloud endpoints remain unverified in CI without live AWS credentials.
   - Cleanup of inert DynamoDB publication records remains deferred to keep reader paths safe and low latency.

7. **Specification status**:
   - Specification 0003 remains Proposed and deferred.
   - Specification 0007 remains Proposed and unimplemented.
