# Review, re-factor, 2026-11-02

**Reviewed by**: Staff Software Engineer
**Scope**: OKF Slice 3.1 Publication Race Resolution and Absent Ledger Boundary Ceilings
**Verdict**: Approve

## Summary

This review records the resolution and verification of publication fencing races and initial reservation ceiling enforcement in Open Knowledge Framework (OKF) Slice 3.1. It clearly distinguishes previously reported results from the new test evidence delivered in this change.

## Previously Reported Results

Prior rounds established and verified:
* Repaired DynamoDB ConditionExpression grammar by replacing invalid arithmetic expressions with precomputed thresholds.
* Atomic call and token ceiling enforcement on existing ledgers during subsequent reservation attempts.
* Separate prompt and output token allocations tracked in [`OkfBudgetLedger`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/okf.py#L134) and persisted across restarts without integer division heuristics.
* Authoritative request token counting via Bedrock Runtime `count_tokens` failing closed with zero Converse calls on missing capability or invalid counts.
* Fail closed reconciliation semantics discarding extracted text when reconciliation returns errors, exceptions, or stale worker detection.
* Idempotent DynamoDB reconciliation under concurrency via atomic condition `(attribute_not_exists(#rf) OR NOT contains(#rf, :expected_ft))`.

## New Test Evidence and Verifications

This change resolves the remaining publication races and initial reservation ceiling vulnerabilities:

1. **DynamoDB publication fencing via native transactions**:
   - In [`DynamoDBOkfRepository.save_manifest`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L1690), the repository no longer relies on a preliminary read followed by an unconditioned batch write.
   - When `fence_token` is provided, all items (manifest, entities, assertions) are committed atomically using `client.transact_write_items` with a native `ConditionCheck` on the budget ledger item (`attribute_not_exists(PK) OR #ft <= :expected_fence`).
   - If a stale worker was delayed or paused after reading its fence and another worker advanced the fence, DynamoDB aborts the entire transaction with `TransactionCanceledException`.
   - As a result, zero manifest records, zero entities, and zero assertions are written to DynamoDB.
   - Verified on real DynamoDB Local in `test_dynamodb_deterministic_publication_race_with_pause`.

2. **SQLite publication fencing via conditional insert**:
   - In [`SQLiteOkfRepository.save_manifest`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L846), manifest persistence uses a conditional `INSERT OR REPLACE INTO kre_okf_facts ... SELECT ... WHERE NOT EXISTS (SELECT 1 FROM kre_okf_facts WHERE pk = ? AND sk = ? AND CAST(json_extract(data_json, '$.fence_token') AS INTEGER) > ?)`.
   - If a competing connection advances the fence, the insert produces zero rows, raises a `RuntimeError`, and rolls back, writing zero entities and zero assertions.
   - Verified with separate SQLite connections in `test_sqlite_deterministic_publication_race_with_pause`.

3. **Ceiling enforcement on first reservations with absent ledgers**:
   - In [`DynamoDBOkfRepository.reserve_extraction_budget`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L2010), the `ConditionExpression` now guards the absent item branch: `(attribute_not_exists(PK) AND :one <= :max_calls AND :est_prompt <= :max_input AND :max_output <= :max_out_ceiling AND :est_tokens <= :max_total)`.
   - Application pre checks validate `max_calls >= 1`, `p_thresh >= 0`, `o_thresh >= 0`, and `t_thresh >= 0` across DynamoDB, SQLite, and InMemory repositories before updating storage.
   - If the first reservation requests zero calls, prompt tokens exceeding 4000, output tokens exceeding 1200, or total tokens exceeding 5200, it is immediately rejected without creating a ledger item.
   - Verified on real DynamoDB Local in `test_dynamodb_local_first_and_subsequent_reservation_boundary_ceilings`, and on SQLite and InMemory in `test_sqlite_and_memory_first_and_subsequent_reservation_boundary_ceilings`.

4. **Failure and in flight manifest write protection**:
   - In [`OkfBuilder._invoke_bedrock_extraction`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1649), in flight reservation attempt manifests pass `fence_token=reservation_fence`.
   - In [`OkfBuilder.extract_and_persist`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1080), the exception handler passes `fence_token=getattr(budget, "last_reservation_fence", None)`, preventing stale failed manifests from overwriting newer active or ready document states.
   - Baseline ingestion remains available per ADR-001 if optional OKF publication fails.

5. **Specification Status**:
   - Specification 0007 remains Proposed and blocked on full live cloud sign off of specification 0006.
   - Specification 0003 remains Proposed and deferred.
