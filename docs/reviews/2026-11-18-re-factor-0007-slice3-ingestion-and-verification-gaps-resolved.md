# Review, re-factor, 2026-11-18

**Reviewed by**: pro (author on sonnet)
**Scope**: 5 files (`backend/src/ingestion/csv_table_ingester.py`, `backend/src/ingestion/graph_builder.py`, `backend/src/services/retrieval/strategies/knowledge_graph.py`, `backend/tests/test_knowledge_graph_ablation_slice3.py`, `docs/specs/0007-verify.md`)
**Verdict**: Approve

## Summary
This review records the complete resolution and verification of the three remaining 0007 ingestion and verification gaps on the `re-factor` branch:
1. **Exact-version CSV schema authority**: Removed unversioned schema inheritance (`store.get_schema(table_id, workspace_id)`) and unversioned `get_table` fallback from the graph extraction path. Relations resolve exclusively from authoritative schema records bound to the exact workspace, table, document, and requested version. Storage keys and authoritative metadata are strictly validated; missing optional metadata alone never substitutes for proof. Old-version relations are never copied onto new-version rows. Unauthorized or unverifiable bindings produce zero relationships and zero hydrated graph candidates.
2. **Repair recovery using actual historical stub state**: Inspected the historical `client=None` stub behavior, which completed synthetic reconciliation with `completed_calls=1` and `spent_output_tokens=0`. Safe recovery distinguishes historical stubs from legitimate empty extractions by checking `spent_output_tokens == 0`. Implemented explicit `recover_stub_manifest` with strict provenance checks, atomic CAS base publication replacement (`base_publication_id=existing_man.publication_id`), and preservation of spent budgets, reservations, and monotonic fence tokens. Tested across Memory, independent SQLite connections, and DynamoDB Local, including racing concurrent publications.
3. **Prove hydrated evidence through the application path**: Strengthened `DocumentsService` CSV and mocked PDF ingestion tests to verify nonempty `candidate_chunks`, actual stored text, exact provenance bindings (CSV row/cells and PDF page), and survival into `EvidenceItem`. Exercised the real graph builder, persistence, retriever, and TableStore hydration interfaces with negative controls proving deleted chunks, tombstones, absent rows, and version mismatches return zero candidates.

## Disposition of Findings
* **Finding 1 (Exact-version CSV schema authority)**: Resolved. In `backend/src/ingestion/csv_table_ingester.py` and `backend/src/ingestion/graph_builder.py`, all fallback lookups to unversioned tables or schemas were removed. New regressions `test_csv_unversioned_and_older_version_schema_relations_not_inherited` and `test_csv_same_table_name_collision_across_documents_rejected` prove complete version and document boundary isolation.
* **Finding 2 (Historical stub state recovery)**: Resolved. Explicit recovery operation `recover_stub_manifest` added to `KnowledgeGraphBuilder`. Bounded recovery safely repairs historical stubs (`spent_output_tokens == 0`) while refusing to overwrite legitimate empty publications that spent model tokens (`test_recovery_preserves_legitimate_empty_publication`). Concurrent publication races trigger `OptimisticConcurrencyError` (`test_recovery_fails_on_competing_publication_cas_conflict`). Tested on Memory, independent SQLite connections, and DynamoDB Local.
* **Finding 3 (Hydrated evidence through the application path)**: Resolved. Tested through real `DocumentsService` ingestion entry points for CSV and PDF. Provenance assertions verify nonempty `candidate_chunks`, actual stored text, exact page/row/cell bindings, and conversion into `EvidenceItem`. Negative controls in `test_hydrated_evidence_negative_controls` verify zero candidates on deleted chunks, tombstones, absent rows, and mismatched versions.

## Test Coverage
* `tests/test_knowledge_graph_ablation_slice3.py`: 28 passed in 39.26s
* `tests/test_knowledge_graph_slice1.py`: 62 passed, 3 skipped in 28.51s
* `tests/test_knowledge_graph_slice2.py`: 59 passed, 5 skipped in 132.35s
* `tests/test_workspace_boundary_isolation.py` & `tests/test_csv_ingestion_complete.py`: 32 passed in 11.75s
* `verify_0007_live.py`: 18/18 live HTTP verification behaviors passed
* Total: 218 unit tests passed, 0 failed, 18 live HTTP behaviors verified.
