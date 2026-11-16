# Review, re-factor, 2026-11-17

**Reviewed by**: Gemini Pro (author on Gemini Pro)
**Scope**: 8 files, re-factor vs af02997
**Verdict**: Qualified acceptance following verified resolution of 6 correctness findings

## Summary

This review evaluated the complete implementation of Spec 0007 Slice 2 (Grounded narrative PDF extraction and durable Bedrock budgeting) alongside carried forward Slice 1 nits. The initial implementation provided foundational extraction prompts, JSON parsing, basic syntax grounding, document scoped entity identity, CountTokens pre-validation, atomic budget reservations, and publication staging.

During pre-acceptance evaluation, 6 correctness issues were identified and repaired. The repaired implementation was verified with targeted tests across live DynamoDB Local transactions and independent SQLite connections.

## Correctness findings and disposition

### 1. DynamoDB lease cleanup races
- **Issue**: Manifest publication and failure cleanup used separate or unconditioned updates that could clear another worker's lease if worker B took over after worker A expired.
- **Disposition**: Resolved. DynamoDB `commit_manifest` now combines manifest creation and lease release into a single atomic `transact_write_items` call conditioned on `fence_token = :ft AND lease_owner_id = :owner AND lease_expires_at >= :now_ts`. In `fail_extraction_budget`, late workers record spend without clearing newer worker leases.
- **Verification**: Verified via `test_dynamodb_local_publication_cleanup_never_clears_another_workers_lease` and `test_dynamodb_local_failure_cleanup_never_clears_another_workers_lease`.

### 2. Reconciliation atomicity and idempotency
- **Issue**: Reading and updating `reconciled_fences` in python application memory allowed duplicate reconciliation of the same attempt and risked overwriting concurrent worker markers.
- **Disposition**: Resolved. DynamoDB reconciliation now uses native `list_append(if_not_exists(#rf, :empty_list), :this_fence)` guarded by `attribute_not_exists(#rf) OR NOT contains(#rf, :exp_fence)`. Duplicate reconciliation is idempotent and cumulative spend is protected.
- **Verification**: Verified via `test_dynamodb_local_atomic_idempotent_reconciliation`.

### 3. PDF evidence, provenance, and identity
- **Issue**: Model output could fabricate evidence, cite mismatched source documents, lose chunk page numbers, or fabricate cross document disambiguation tokens.
- **Disposition**: Resolved. Extraction enforces verbatim evidence presence in the cited chunk, rejects chunks with mismatched workspace, document, or version IDs, preserves chunk `page_number`, and strips model supplied disambiguation tokens unless verified against an authoritative source mapping.
- **Verification**: Verified via `test_extract_pdf_relationships_rejects_empty_or_fabricated_evidence`, `test_extract_pdf_relationships_rejects_wrong_source_bindings`, `test_extract_pdf_relationships_preserves_actual_page_number`, and `test_extract_pdf_relationships_rejects_unverified_model_disambiguation_token`.

### 4. Sentence grounding across unrelated clauses and modalities
- **Issue**: Word order matching in a single sentence accepted unrelated clauses (such as "Acme acquired Gamma, while Beta opened an office") as well as hypothetical or reported claims.
- **Disposition**: Resolved. Grounding validation partitions sentences by punctuation and conjunction clauses, validates syntactic direct object attachments, and rejects unsupported modalities including conditional and reported speech.
- **Verification**: Verified via `test_grounding_rejects_unrelated_clauses_in_one_sentence` and `test_grounding_rejects_hypothetical_and_reported_claims`.

### 5. Missing or invalid provider usage
- **Issue**: Non numeric, missing, or zero provider usage could be accepted or substituted with estimates.
- **Disposition**: Resolved. Authoritative `inputTokens` and `outputTokens` are required. Missing, boolean, negative, zero inputTokens, or out of range counts fail closed without publishing extracted relationships, while preserving existing ready publications.
- **Verification**: Verified via `test_build_pdf_graph_fails_closed_on_invalid_usage`.

### 6. Provider deadline and capacity bounds
- **Issue**: Provider calls lacked bounded execution deadlines and capacity retention, while boto3 hidden retries could mask spend.
- **Disposition**: Resolved. `KnowledgeGraphProviderConcurrencyController` enforces caller deadlines with bounded queue capacity, retains semaphore slots until underlying provider calls complete, and disables client retries.
- **Verification**: Verified via `test_provider_call_deadline_and_capacity_retention` and `test_disable_client_retries_unregisters_boto3_handlers`.

## Nits

- `backend/src/ingestion/graph_builder.py`: Slicing raw text bytes at 2000 and decoding with `errors="ignore"` safely prevents UTF-8 decoding exceptions, but character-aware slicing could be adopted if exact boundary preservation is needed.
- `backend/src/ingestion/graph_builder.py`: Fallback heuristic prompt token estimation applies only when no Bedrock client is configured; restricted to offline test doubles.

## Test coverage

TESTS = configured. All 38 tests in `backend/tests/test_knowledge_graph_slice2.py` pass cleanly in 7.84s. In addition, Slice 1 regressions (65 tests), OKF Slice 3.1 regressions (57 tests), and TableStore regressions (6 tests) all pass cleanly (166 total passing tests).
