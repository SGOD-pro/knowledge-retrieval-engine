# Review: Fast Path Unsupported Prose Answer Repair

* **Author**: Senior Reviewer
* **Date**: 2026-11-22
* **Branch**: `re-factor`
* **Tested Commit**: `883b134d1b747378d3eb6ffbc257fcaea12b5b48`
* **Inspected Diff**: [883b134](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/883b134d1b747378d3eb6ffbc257fcaea12b5b48)

## Summary

This review evaluates the repair to unsupported fast prose answers in the LangGraph pipeline, superseding earlier ungrounded prose fallback logic.

Previously, `end_fast_path` contained a sentence level extractive fallback based purely on token intersection between query terms and chunk sentences. For questions such as "What is Apple's Commission File Number?" and candidate chunk "The Commission regulates financial reporting.", the single overlapping token "Commission" caused the fallback to accept the irrelevant sentence, assign fast path success, and deliver ungrounded prose as a factual answer.

Under this repair, token overlap only acceptance is completely removed from `end_fast_path`. When verified factual answer extraction cannot establish an answer, execution uses the existing escalation mechanism into full retrieval, refreshing vector candidates, enforcing the single synthesis limit, and returning a clean refusal when evidence remains insufficient.

## Code Changes Inspected

1. [backend/src/services/langgraph_pipeline.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L2166-L2320):
   * Removed lines 2169 to 2226 (`# Sentence-level extractive fallback for fast-path prose queries`), eliminating the `_STOPS` set, token intersection heuristic, and ungrounded prose acceptance.
   * Preserved supported verified factual extraction via `extract_verified_fact` and `verified_ans and verified_chunk`.
   * Unverified queries in `end_fast_path` directly fall through to the established escalation payload (`executed_path="full"`, `final_answer="NOT_FOUND"`, `confidence_score=0.0`, empty citations, and `fast_path_ms` recorded in `stage_timings`).
   * Updated `route_after_extraction` so that candidate chunks without verified answers route to `end_fast_path` to record fast path latency before escalating via `route_after_fast_path` to `escalate_to_full_retrieval`.

2. [backend/tests/test_fix4_fast_path_confidence.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_fix4_fast_path_confidence.py#L30-L55):
   * Updated test fixture inputs with explicit verified answers and verified chunks, ensuring the test validates that verified fast path confidence reflects chunk similarity scores rather than relying on legacy token overlap.

3. [backend/tests/test_fast_path_fallback_regressions.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_fast_path_fallback_regressions.py#L789-L985):
   * Added `test_end_fast_path_rejects_unsupported_token_overlap_prose` proving that question "What is Apple's Commission File Number?" with chunk "The Commission regulates financial reporting." returns `executed_path="full"`, `final_answer="NOT_FOUND"`, zero confidence, and empty citations.
   * Added `test_end_fast_path_preserves_supported_verified_factual_answers` verifying that legitimate abbreviations like CFN complete on the fast path with zero LLM generation.
   * Added `test_compiled_pipeline_escalates_unsupported_fast_prose_and_refreshes_retrieval` proving that compiled pipeline escalation recovers evidence outside the fast top 1 and respects the single synthesis limit.
   * Added `test_compiled_pipeline_refusal_when_evidence_remains_insufficient` verifying clean refusal and empty citations when evidence remains insufficient.

## Verification Evidence

All 33 regression tests in the fast path and answer quality test suites passed on tested HEAD `883b134d1b747378d3eb6ffbc257fcaea12b5b48`:

* [backend/tests/test_fast_path_fallback_regressions.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_fast_path_fallback_regressions.py): 17 passed
* [backend/tests/test_fix4_fast_path_confidence.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_fix4_fast_path_confidence.py): 1 passed
* [backend/tests/test_answer_quality_remediation.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_answer_quality_remediation.py): 15 passed
* Total: 33 passed, 0 failed, 0 skipped in 5.68s.

Benchmark scores remain provisional. Zero live provider calls, zero benchmark reruns, zero ground truth modifications, and zero corpus alterations were made during this work.
