# Review, branch re-factor, 2026-11-15

**Reviewed by**: Staff Reviewer (author on claude-3-5-sonnet)
**Scope**: 5 files, commit 04cdc6938f9b18eaebb2b5921d49654b96f93e8f vs 9ab88ed1d4fbef9e6f0ea74c7488e34d1eb7b94b
**Verdict**: Approve with nits

## Summary
This change completes the final repairs for specification 0006 slice 3.1 publication authority and test isolation. It combines base publication checks with budget ledger lease validations into atomic conditional statements for SQLite unfenced writes across both absent and existing base publications. The change hardens ready PDF publication preservation testing by giving each failure mode an independent document and budget ledger while verifying actual provider calls and reconciliation attempts.

## Nits
- ⚪ [`backend/src/db/okf_repo.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L1199), The SQL condition in `SQLiteOkfRepository.save_manifest` checks `json_extract(data_json, '$.lease_owner_id') IS NOT NULL AND CAST(json_extract(data_json, '$.lease_expires_at') AS REAL) > ?`, which is more direct than the summary snippet in [`docs/reviews/2026-11-14-re-factor-okf-publication-races-resolved.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-14-re-factor-okf-publication-races-resolved.md#L41-L42) referencing `status = 'active'`. Consider updating that prose snippet in future documentation sync passes to reflect the exact column expression.
- ⚪ [`backend/tests/test_okf_slice3_1.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_okf_slice3_1.py#L5102), In `test_preserve_ready_pdf_publication_during_new_attempt_failures`, reserving budget explicitly before invoking `extract_and_persist` consumes one of the two permitted calls on the budget ledger. Because the single test chunk consumes only the remaining call, the test succeeds cleanly, but callers should remember that multi chunk tests would exhaust two call ceilings if pre reserved.

## Strengths
- Atomic conditional statements in `SQLiteOkfRepository.save_manifest` combine base publication ordering and active lease protection in one statement, completely closing the race window between pre checks and manifest insertion.
- The new test `test_sqlite_unfenced_publication_ordering_blocked_by_concurrent_lease_across_separate_connections` provides genuine multi connection concurrency coverage on disk backed SQLite using thread synchronization and connection proxies.
- Test `test_preserve_ready_pdf_publication_during_new_attempt_failures` isolates documents, publications, and budget ledgers across all four failure scenarios, explicitly validating successful reservation, actual Converse dispatch, reconciliation error interception, and ready manifest preservation across Memory, SQLite, and DynamoDB Local.
- Clear and precise systems documentation in [`backend/src/db/okf_repo.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L2139-L2158) and [`docs/specs/0006-okf-integration.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0006-okf-integration.md#L47-L50) regarding client timestamp sampling versus DynamoDB condition expression evaluation, non cancellation of server transactions by client socket timeouts, and fence token advancement guarantees.

## Test coverage
The changes are thoroughly covered by deterministic test suites. All 4 focused tests in `tests/test_okf_slice3_1.py` pass in 7.22s, all 57 tests in `tests/test_okf_slice3_1.py` pass in 9.36s, and the complete 112 test OKF regression suite passes in 16.01s (169 passing tests total, zero failures, zero errors, zero skips).
