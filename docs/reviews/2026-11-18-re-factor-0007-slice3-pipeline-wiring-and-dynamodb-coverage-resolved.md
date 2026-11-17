# Review, re-factor, 2026-11-18

**Reviewed by**: pro (author on sonnet)
**Scope**: 4 files (`backend/src/services/langgraph_pipeline.py`, `backend/src/db/knowledge_graph_repo.py`, `backend/tests/test_knowledge_graph_ablation_slice3.py`, `docs/specs/0007-verify.md`)
**Verdict**: Approve

## Summary
This review records the resolution and verification of the final 0007 pipeline wiring and DynamoDB coverage gaps on the `re-factor` branch:
1. **Wire TableStore into Query Pipeline**: Resolved shared TableStore via `get_shared_table_store()` and passed it into `KnowledgeGraphRetriever` inside `run_graph`. Added dependency injection support for `table_store`, `knowledge_graph_repo`, `boundary_repo`, `doc_repo`, and `retriever` in `PipelineState` and `Pipeline.run(...)` for isolated unit and pipeline testing. Preserved caller deadline bounds (75 ms), fail closed handling around `retriever.expand`, workspace and document version isolation, and tombstone checks. Fixed strategy tagging in `run_reranker` to match chunk IDs rather than mutating frozen dataclass references.
2. **Test Production Pipeline Path**: Added `test_production_pipeline_tablestore_hydration_and_evidence_fusion` in `backend/tests/test_knowledge_graph_ablation_slice3.py`. Leaves document chunks absent in the repository, forcing TableStore hydration. Verified nonempty graph candidates with real cell contents, table locator, and row references. Verified that candidates survive reranking into `retained_evidence_items` with strategy `knowledge_graph`. Exercised five negative controls: missing table rows, mismatched document versions, cross workspace access, source tombstones, and unavailable TableStore; all negative controls produce zero graph candidates while strictly preserving baseline retrieval.
3. **Make DynamoDB Coverage Explicit**: Parameterized recovery tests (`test_recovery_on_historical_stub_with_completed_calls_and_zero_tokens` and `test_recovery_fails_on_competing_publication_cas_conflict`) across `["memory", "sqlite", "dynamodb"]`. Fixed DynamoDB KnowledgeGraphRepository fence verification in `commit_manifest` to inspect authoritative top level DynamoDB item attributes (`fence_token`, `lease_owner_id`, `lease_expires_at`) instead of stale `data_json`. Verified all backends directly against running DynamoDB Local with isolated tables and reliable cleanup in pytest fixtures.
4. **Verification and Records**: Executed Slice 1 (62 passed, 3 skipped), Slice 2 (59 passed, 5 skipped), Slice 3 (33 passed, 0 skipped), TableStore and CSV ingestion (32 passed, 0 skipped). Total 223 passed tests with zero regressions. Preserved historical review records and updated `docs/specs/0007-verify.md`.

## Disposition of Findings
* **Finding 1 (Pipeline TableStore wiring)**: Resolved. In `backend/src/services/langgraph_pipeline.py`, `run_graph` resolves `table_store` through the application boundary or caller injection and provides it to `KnowledgeGraphRetriever`. In `run_reranker`, candidate chunks from `graph` match by ID to ensure proper assignment of strategy `knowledge_graph` on `retained_evidence_items`.
* **Finding 2 (Production pipeline path test)**: Resolved. Implemented `test_production_pipeline_tablestore_hydration_and_evidence_fusion` covering the complete `run_graph` to `run_reranker` flow without mocking the retriever or hydration layer. Proved real cell data reaches pipeline evidence items, and verified 5 negative controls.
* **Finding 3 (Explicit DynamoDB coverage)**: Resolved. Parameterized recovery and publication CAS race tests across `["memory", "sqlite", "dynamodb"]`. Fixed DynamoDB fence attribute lookup bug in `backend/src/db/knowledge_graph_repo.py`. All parameterized test cases run and pass against live DynamoDB Local.

## Test Coverage
* `tests/test_knowledge_graph_ablation_slice3.py`: 33 passed in 16.61s
* `tests/test_knowledge_graph_slice1.py`: 62 passed, 3 skipped in 28.28s
* `tests/test_knowledge_graph_slice2.py`: 59 passed, 5 skipped in 132.35s
* `tests/test_workspace_boundary_isolation.py` & `tests/test_csv_ingestion_complete.py`: 32 passed in 11.75s
* Total: 223 unit tests passed, 0 failed.
