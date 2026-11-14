# Review, re factor, November 14 2026

**Reviewed by**: Staff Software Engineer
**Scope**: OKF Publication Races Resolved, SQLite Atomic Ordering, Lease Clock Assumptions, and Ready Manifest Preservation
**Verdict**: Approve

## Summary

This review records the resolution and verification of three critical publication race conditions in Open Knowledge Framework (OKF) Slice 3.1:
1. SQLite publication ordering is atomic across separate connections using conditional SQL statements that evaluate base publication match or absence directly at write execution time, preventing paused concurrent workers from overwriting committed publications.
2. Lease expiry and clock assumptions are explicitly defined and validated directly at write execution by re sampling timestamps immediately before transaction dispatch, failing closed when authority is lost, and documenting client side clock evaluation in DynamoDB condition checks.
3. Authoritative ready PDF publications and their retrieved assertions are preserved intact during subsequent extraction attempts across process crashes, provider call timeouts, budget reconciliation errors, and storage write failures, keeping budget reservations durable separately in the budget ledger.

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
* Optimistic concurrent publication ordering against base publication identifiers.
* Fenced publication authority checking ledger existence, current fence token, and valid unexpired lease ownership at write time.
* Active worker lease retention across budget spend reconciliation.
* Bounded entity resolution pagination enforcing a 5 page safety bound failing closed to protect retrieval deadlines.
* Mandatory non empty worker identity on fenced publication writes across InMemory, SQLite, and DynamoDB Local repositories.

## New Test Evidence and Verifications

This hardening resolves the remaining publication races across [`backend/src/db/okf_repo.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), [`backend/src/ingestion/okf_builder.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py), and [`backend/tests/test_okf_slice3_1.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_okf_slice3_1.py):

1. **SQLite publication ordering atomic across separate connections**:
   * In [`backend/src/db/okf_repo.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), `SQLiteOkfRepository.save_manifest` replaces deferred pre read assumptions with atomic conditional statements:
     * When `target_base is None`, executes `INSERT INTO kre_okf_facts ... SELECT ... WHERE NOT EXISTS (SELECT 1 FROM kre_okf_facts WHERE pk = ? AND sk = ?)`.
     * When `target_base is not None`, executes `INSERT OR REPLACE INTO kre_okf_facts ... SELECT ... WHERE EXISTS (SELECT 1 FROM kre_okf_facts WHERE pk = ? AND sk = ? AND json_extract(data_json, '$.publication_id') = ?)`.
   * If a concurrent worker committed a manifest between the repository level pre check and the write, `cur.rowcount` is 0. The repository performs a post write inspection and raises `RuntimeError` describing the exact concurrent conflict, leaving the first committed publication intact.
   * Deterministic test `test_sqlite_publication_ordering_atomic_across_separate_connections` hooks Worker A's connection, pauses Worker A after its pre check query, allows Worker B on a separate connection to commit a ready manifest, resumes Worker A, and proves Worker A is rejected while Worker B remains authoritative for both absent base and replaced base scenarios.

2. **Lease expiry and clock assumptions at write dispatch**:
   * Explicit clock and network delay assumptions: DynamoDB condition expressions evaluate client supplied values (`:now_epoch`) because DynamoDB does not evaluate physical wall clock timestamps inside server side expressions. Clocks across workers must remain synchronized within standard NTP tolerances.
   * Client side re sampling: [`backend/src/db/okf_repo.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py) re samples `tx_now_epoch = Decimal(str(round(time.time(), 4)))` immediately before constructing the transaction condition check, ensuring any delays during serialization or staging invalidate expired leases at dispatch time. SQLite similarly re samples `write_now_ts = time.time()` immediately before statement execution.
   * Deterministic test `test_lease_expiry_and_fence_advancement_during_publication_interval` verifies:
     * A lease that expires after budget ledger validation but before transaction execution fails closed on both DynamoDB Local and SQLite with `RuntimeError`.
     * A fence token advanced by a competitor worker during the same interval fails closed with `RuntimeError` (`Worker fenced out`).

3. **Preservation of ready PDF publications during new attempt failures**:
   * In [`backend/src/ingestion/okf_builder.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py), extraction attempts preserve existing ready manifests.
   * Budget reservations are maintained durably in the dedicated budget ledger (`BUDGET#DOC#{doc}#VER#{ver}`) without modifying the manifest row.
   * When an attempt encounters a failure or timeout, the builder avoids overwriting a prior ready manifest with an in flight failed manifest.
   * In [`backend/src/db/okf_repo.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py), `InMemoryOkfRepository`, `SQLiteOkfRepository`, and `DynamoDBOkfRepository` reject attempts to overwrite a ready manifest with a failed manifest.
   * Deterministic test `test_preserve_ready_pdf_publication_during_new_attempt_failures` exercises all three engines across four distinct failure modes:
     * Mid attempt crash after budget reservation.
     * Provider call timeout (`TimeoutError`).
     * Budget spend reconciliation storage failure.
     * Failed final manifest write transaction.
   * In all cases across InMemory, SQLite, and DynamoDB Local, the initial ready publication remains authoritative, and assertions remain fully retrievable via `OkfRetriever.lookup_sync`.

## Test Suite Execution Results

* Focused test execution:
  `cd backend && PYTHONPATH=src uv run pytest tests/test_okf_slice3_1.py -k "test_sqlite_publication_ordering_atomic_across_separate_connections or test_lease_expiry_and_fence_advancement_during_publication_interval or test_preserve_ready_pdf_publication_during_new_attempt_failures" -v`
  3 passed in 4.19s
* Complete Slice 3.1 suite:
  `cd backend && PYTHONPATH=src uv run pytest tests/test_okf_slice3_1.py`
  56 passed in 10.68s
* Complete OKF regression suite:
  `cd backend && PYTHONPATH=src uv run pytest tests/test_okf_slice1.py tests/test_okf_ablation_slice2.py tests/test_okf_correctness_regressions.py tests/test_okf_graph_contract.py tests/test_contracts_and_identity.py`
  112 passed in 30.06s
* Total OKF test suite: 168 passed in 40.74s
* Zero test failures, zero errors, zero skips.

## Environment and Storage Emulation

* Testing was conducted in a local Linux environment using Python 3.12/3.14.
* DynamoDB Local container `test_ddb_local` was executed on `http://localhost:8000` to verify live AWS SDK botocore transaction expressions and conditions.
* Multi connection SQLite testing utilized disk backed temporary database files with WAL journal mode.
* Live AWS Bedrock and production AWS DynamoDB cloud endpoints remain unverified in CI without cloud credentials.

## Remaining Operational Boundaries and Limitations

1. DynamoDB clock synchronization: Because DynamoDB conditions compare client supplied epoch values, worker nodes in distributed deployments must synchronize their system clocks via NTP to prevent lease expiration drift.
2. Inert record cleanup: Staged and superseded entity and assertion records remain inert in DynamoDB to maintain reader isolation without non atomic cross partition deletions; physical garbage collection remains deferred to background maintenance.
3. Single transaction item ceiling: Transactions exceeding 99 items fail open per `ADR-001` or utilize the staged publication protocol for wide tables.

## Specification and Scope Status

* [`docs/specs/0006-okf-integration.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0006-okf-integration.md): Updated with Item 21 in build plan and extended `AC-3` details.
* [`docs/specs/0006-verify.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0006-verify.md): Updated recording 56 passing tests in `test_okf_slice3_1.py`.
* [`docs/specs/0007-knowledge-graph-enrichment.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0007-knowledge-graph-enrichment.md): Remains Proposed and planned; zero claims of implementation.
* Specification 0003: Remains Proposed and deferred.
