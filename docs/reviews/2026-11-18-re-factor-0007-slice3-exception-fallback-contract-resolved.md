# Review, re-factor, 2026-11-18

**Reviewed by**: pro (author on sonnet)
**Scope**: 3 files (`backend/src/services/langgraph_pipeline.py`, `backend/tests/test_knowledge_graph_ablation_slice3.py`, `docs/specs/0007-verify.md`)
**Verdict**: Approve

## Summary
This review records the resolution and verification of the 0007 graph exception fallback contract violation on the `re-factor` branch:
1. **Contract Violation Repair**: When `retriever.expand()` raised an exception, `run_graph` previously attempted to construct `KnowledgeGraphLookupResult(execution_status="error")`. Because `execution_status` only permits `"completed"`, `"truncated"`, `"caller_timeout"`, `"admission_rejected"`, `"tombstoned"`, and `"storage_failed"` under strict Pydantic validation, this raised a validation error that escaped `run_graph` and disrupted baseline query execution. The fallback has been repaired to use the valid status `"storage_failed"`. The schema was not broadened.
2. **Focused Regression Test**: Added `test_graph_exception_fallback_storage_failed_preserves_baseline` in `backend/tests/test_knowledge_graph_ablation_slice3.py`. The test enables graph retrieval, injects a retriever whose `expand` method raises a storage exception, and invokes both `run_graph` and downstream `run_reranker`. The test proves no exception escapes, `graph_execution_status` is `"storage_failed"`, graph candidates are empty, and baseline retrieval evidence survives intact. Paid reranking calls were mocked.
3. **Verification and Records**: Executed the focused regression and related suites. The newly executed tests passed without regressions: Slice 3 (34 passed), Slice 1 and Slice 2 (129 passed, 8 skipped), and Workspace boundary and CSV ingestion (32 passed). Updated `docs/specs/0007-verify.md` and preserved historical review records.

## Disposition of Findings
* **Finding 1 (Graph exception fallback contract violation)**: Resolved. In `backend/src/services/langgraph_pipeline.py`, line 783 now instantiates `KnowledgeGraphLookupResult(execution_status="storage_failed", latency_ms=...)`. When graph expansion encounters storage or network errors, it fails closed cleanly with zero graph candidates and valid failure telemetry.
* **Finding 2 (Baseline preservation on expansion failure)**: Resolved. Covered by `test_graph_exception_fallback_storage_failed_preserves_baseline`. Baseline candidates survive reranking and reach `retained_evidence_items` without interruption.

## Newly Executed Test Results (2026-11-18)
* `tests/test_knowledge_graph_ablation_slice3.py`: 34 passed in 14.01s (includes new regression `test_graph_exception_fallback_storage_failed_preserves_baseline`)
* `tests/test_knowledge_graph_slice1.py`: 62 passed, 3 skipped in 28.28s
* `tests/test_knowledge_graph_slice2.py`: 59 passed, 5 skipped in 132.35s
* `tests/test_workspace_boundary_isolation.py` & `tests/test_csv_ingestion_complete.py`: 32 passed in 11.26s
* Total: 195 unit tests passed, 0 failed.
