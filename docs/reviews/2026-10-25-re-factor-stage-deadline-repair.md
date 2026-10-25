# Review, `re-factor`, 2026-10-25

**Reviewed by**: `gemini-2.5-pro` (author on `gemini-3.8-flash`)
**Scope**: 8 files, uncommitted
**Verdict**: Approve

## Summary
This changeset implements end to end deadline bounding and admission control across the entire PageIndex retrieval stage. Snapshot discovery, tombstone validation, manifest reading, and candidate chunk retrieval are now fully encapsulated within worker tasks managed by the concurrency controller, preventing late publication and guaranteeing fail closed tombstone behavior. Initial review flagged stray debug prints and status tuple unpacking, both of which have been resolved.

## Resolved findings
### 🟢 Stray debug print statements in pipeline stage handlers, `backend/src/services/langgraph_pipeline.py`
**Problem**: The debug statement `print(f"DEBUG run_page_index: end before return latency_ms={latency_ms:.2f}")` was unintentionally placed into nine distinct pipeline stage functions.
**Resolution**: All nine stray print calls were removed, preserving clean standard output and configured logger output across all pipeline stages.

### 🟢 Status tuple unpacking in concurrency controller, `backend/src/services/retrieval/page_index_lookup.py`
**Problem**: The controller inspected arbitrary two element tuples to extract worker status strings.
**Resolution**: Introduced typed `WorkerResultWrapper` explicitly encapsulating worker results and custom statuses (`completed`, `tombstoned`), eliminating ambiguous tuple type inspection.

### 🟢 Chunk extraction fallback check, `backend/src/services/langgraph_pipeline.py`
**Problem**: An unreachable chunk query fallback check existed on `res.candidate_chunks`.
**Resolution**: Simplified to `doc_chunks = res.candidate_chunks or ()`, preventing unconstrained caller thread chunk retrieval.

## Nits
- ⚪ `backend/src/schemas/contracts/page_index.py:86`, `candidate_chunks` typed as `tuple[Any, ...]` to avoid coupling frozen contracts to mutable dataclass definitions.

## Strengths
- Complete stage deadline coverage across all storage operations: Snapshot discovery, tombstone checks, summary manifest lookups, S3 overflow detail fetches, and candidate chunk queries are all executed under concurrency admission and bounded caller deadlines.
- Fail closed tombstone enforcement: Tombstone verification runs inside the worker task and treats any lookup failure or exception as tombstoned, preventing unauthorized retrieval without raising uncaught exceptions.
- Zero late candidate publication: Fetching candidate chunks inside worker threads ensures that caller timeouts drop candidate publication immediately, even when slow background database queries finish later.
- Comprehensive regression coverage: Four targeted unit tests in `backend/tests/test_page_index_ablation_slice2.py` cleanly exercise slow snapshot discovery, slow tombstone validation, slow chunk retrieval without late publication, and slow S3 overflow fallback.

## Test coverage
All 122 tests pass across all six test suites (`test_page_index_slice1.py`, `test_page_index_ablation_slice2.py`, `test_workspace_boundary_isolation.py`, `test_pdf_retrieval_slice_0004.py`, `test_csv_vertical_slice_0004.py`, `test_contracts_and_identity.py`). Both live verification suites (`verify_0005_live.py` 10 of 10 passed, `verify_0004_live.py` 17 of 17 passed) confirm runtime behavior against running uvicorn service.
