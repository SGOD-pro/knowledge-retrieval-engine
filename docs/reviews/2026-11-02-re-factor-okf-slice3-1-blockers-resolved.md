# Review, re-factor, 2026-11-02

**Reviewed by**: Staff Software Engineer
**Scope**: OKF Slice 3.1 Review Blockers Resolution
**Verdict**: Approve

## Summary

This review records the complete resolution of the remaining Slice 3.1 review blockers across DynamoDB budget reservations, fail closed reconciliation, token counting, atomic idempotent reconciliation under concurrency, publication path race prevention, and specification corrections.

## Key Changes and Verifications

1. **DynamoDB budget reservations repaired**:
   - ConditionExpression grammar: Replaced invalid arithmetic expressions (`#spent + :est_tokens`) with precalculated comparison thresholds (`:p_thresh`, `:o_thresh`, `:t_thresh`) in [`DynamoDBOkfRepository.reserve_extraction_budget`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L1885).
   - Atomic call and token ceiling enforcement: Enforces `max_calls`, `max_input_tokens`, and `max_output_ceiling` against all active reservations atomically.
   - Separate prompt and output ceilings preserved across restart: [`OkfBudgetLedger`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/okf.py#L134) now tracks `reserved_prompt_tokens`, `reserved_output_tokens`, `spent_prompt_tokens`, and `spent_output_tokens`. In [`get_or_create_document_budget`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L240), token usage is recovered directly from these fields without arbitrary integer division heuristics.
   - Real DynamoDB Local verification: Tested directly against DynamoDB Local container (`http://localhost:8000`) in `test_dynamodb_local_reservations_and_ceiling_enforcement`.

2. **Fail closed on reconciliation**:
   - In [`OkfBuilder._invoke_bedrock_extraction`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1705), if reconciliation returns an error, raises an exception, expires its lease, or is fenced by a newer worker, the response text is discarded and empty text is returned. Downstream assertion extraction and publication are completely suppressed, publishing zero assertions.
   - The reserved spend remains accounted for on the durable ledger.

3. **Strict token counting**:
   - In [`OkfBuilder._invoke_bedrock_extraction`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1495), Bedrock Runtime `count_tokens` is strictly required before provider dispatch.
   - Missing client capability, exception during count, malformed response, or non positive token counts immediately abort provider dispatch with zero Converse calls.
   - Verified via unit test `test_token_counting_missing_or_invalid_zero_converse_dispatches`.

4. **Idempotent DynamoDB reconciliation under concurrency**:
   - In [`DynamoDBOkfRepository.reconcile_extraction_budget`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L2025) and [`DynamoDBOkfRepository.fail_extraction_budget`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L2155), atomic condition `(attribute_not_exists(#rf) OR NOT contains(#rf, :expected_ft))` ensures that concurrent duplicate requests or stale worker calls cannot double count completed calls or token expenditures.
   - Verified against DynamoDB Local in `test_dynamodb_local_reconciliation_concurrent_idempotency` and `test_dynamodb_local_stale_reconciliation_concurrent_idempotency`.

5. **Publication path race prevention**:
   - In [`OkfRepositoryBase.save_manifest`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L86) across all repository backends, `fence_token` validation checks whether a later worker has superseded the active lease fence. If the fence token does not match the ledger, a `RuntimeError` is raised and the manifest write is rejected.
   - In [`OkfBuilder.build_and_persist_workspace_manifest`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1400), `budget.last_reservation_fence` is supplied to `save_manifest`.
   - Verified via unit test `test_publication_path_fencing_race_prevention`.

6. **Specification alignment and graph schema correction**:
   - In [`docs/specs/0007-knowledge-graph-enrichment.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0007-knowledge-graph-enrichment.md), updated entity lookup sort key to `SK = NAME#{normalized_canonical_name}#DOC#{document_id}#VER#{document_version}#ENT#{entity_id}` to prevent collisions across documents and versions while supporting deterministic prefix lookups via `begins_with`. 0007 remains Proposed with zero graph code implemented.
   - In [`docs/specs/0006-okf-integration.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0006-okf-integration.md) and [`docs/specs/0006-verify.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0006-verify.md), recorded the verified DynamoDB Local integration results, separate token limits across restarts, fail closed reconciliation semantics, and publication race fencing, while honestly recording that live AWS cloud endpoints remain unverified in CI without live credentials.
