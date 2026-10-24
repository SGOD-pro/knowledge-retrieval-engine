# Review, re-factor, 2026-10-25

**Reviewed by**: Staff Software Engineer (author on colleague)
**Scope**: 10 files, uncommitted
**Verdict**: Approve with nits

## Summary
This change repairs the two confirmed integration gaps by routing production PageIndex lookups through a synchronous concurrency controller with caller deadlines and capacity retention, bounding stage duration with PAGEINDEX_STAGE_TIMEOUT_MS, measuring complete summary payloads including cross references before persistence, and rechecking bounds after overflow transformation. Overall quality is high with deterministic execution, zero token consumption, robust error handling, and comprehensive regression tests across both slices. The implementation is safe to merge with minor observations regarding loop termination efficiency and dual semaphore concurrency ceilings.

## Minor
### 🟡 Redundant outer loop iteration on stage budget exhaustion, `backend/src/services/langgraph_pipeline.py:341`
**Problem**: When remaining stage budget drops below one millisecond, line 341 breaks out of the inner loop over target labels. If the elapsed stage time at line 398 is slightly below the stage limit, the outer loop over PDF document snapshots continues to the next document snapshot, where line 341 triggers again and logs another budget exhaustion warning.
**Why it matters**: In workspaces containing many PDF documents, an exhausted stage budget causes log spam and redundant loop iterations across subsequent documents rather than exiting the stage immediately.
**Suggested fix**: Set a flag when stage budget is exhausted to break the outer loop immediately, or evaluate the remaining stage budget check at the outer loop boundary.

### 🟡 Decoupled semaphores between async and sync controllers, `backend/src/services/retrieval/page_index_lookup.py:55`
**Problem**: The concurrency controller maintains both an asyncio semaphore and a threading semaphore, each initialized to max concurrency.
**Why it matters**: Because the two semaphores operate independently, concurrent async lookups and sync lookups do not share permit pools. If four async and four sync lookups arrive concurrently, eight tasks could pass admission and enter the underlying thread pool executor whose worker limit is four, queuing excess tasks internally while caller deadlines tick. Production currently uses only sync lookups in the pipeline node, so this does not impact current production traffic, but mixed usage in future extensions could exceed the intended concurrency ceiling.
**Suggested fix**: Coordinate permits through a shared thread lock counter or route async requests through the same synchronization primitive to enforce an absolute concurrency bound across both call styles.

## Nits
* ⚪ `backend/src/db/page_index_repo.py:355`, Serialized size estimation in `_measure_overflow_dict` calculates byte length with raw payload bytes set to zero before assigning the resulting integer, which introduces a negligible byte difference that remains well within the 150KB DynamoDB safety margin.

## Strengths
* Synchronous production lookup protection: Lookups in `run_page_index` now route through `lookup_target_sync`, enforcing bounded admission queues, caller deadlines, worker capacity retention during lingering storage tasks, and total stage deadline bounding via `PAGEINDEX_STAGE_TIMEOUT_MS`.
* Complete summary sizing and lossless S3 overflow: `persist_manifest` accurately measures full summary payloads including cross references and definitions before inline persistence, rechecks size after initial section bounding, and preserves complete layout topology in S3 without silent truncation.
* Clean fallback to baseline: Storage timeouts, queue rejections, and retrieval failures fall back gracefully to baseline candidate chunks without dropping valid evidence or throwing uncaught exceptions.
* Comprehensive regression tests: Slice 1 and Slice 2 suites add rigorous test cases validating slow storage fallback, admission saturation, caller capacity retention, cross reference heavy payload sizing, and cumulative stage deadline bounding.

## Test coverage
Test coverage is comprehensive across both slices. The suite in `backend/tests/test_page_index_slice1.py` contains 22 passing tests verifying complete summary sizing, S3 overflow rechecking, capacity retention, caller deadline enforcement, and explicit botocore retry disabling. The suite in `backend/tests/test_page_index_ablation_slice2.py` contains 11 passing tests verifying pipeline slow storage fallback, admission queue saturation shedding, and total stage deadline bounding across documents and labels. All 33 PageIndex tests pass deterministically in under 11 seconds, and all 461 backend tests pass without regressions.
