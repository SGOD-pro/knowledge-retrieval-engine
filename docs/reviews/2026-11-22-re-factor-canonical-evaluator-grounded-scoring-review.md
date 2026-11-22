# Review: Canonical Evaluator Grounded Answer Scoring

* **Author**: Senior Reviewer
* **Date**: 2026-11-22
* **Branch**: `re-factor`
* **Tested Commit**: `ceb7e25e45e99264837fa436b9ceb75d89ec3b81`
* **Inspected Diff**: [ceb7e25](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/ceb7e25e45e99264837fa436b9ceb75d89ec3b81)

## Summary

This review evaluates the implementation of grounded-answer scoring in the canonical benchmark evaluator, repairing the defect where citation validity was uncoupled from correctness and matching citation identifiers alone could not establish claim support.

Previously, `run_canonical_60_benchmark.py` evaluated answer correctness strictly by matching answer text or contracts (`is_correct`), and separately called `validate_citations`. However, citation validity never gated answer correctness. Consequently, an answerable query with missing citations, fabricated citations, wrong document sources, or wrong document versions could be graded as correct solely due to lexical overlap. Furthermore, citing a chunk ID without actual evidence in the context text could not prevent a hallucinated claim from passing.

This change preserves the historical answer-text score as an explicitly named metric (`answer_text_correct`, `answer_text_accuracy`, with legacy `overall_accuracy` preserved), while introducing grounded correctness (`is_grounded_correct`, `grounded_correct`, `grounded_accuracy`) that requires both answer-text correctness and validated request-retained evidence support.

## Core Rules and Implementation Details

1. **Preserved Metrics**:
   * `query_details.json` records `is_correct` (preserved answer-text correctness), `answer_text_correct` (explicit alias), `is_grounded_correct` (boolean), `grounded_correct` (boolean alias), and `grounded_evaluation_reason` (string explaining the evaluation decision).
   * `manifest.json` preserves `metrics["overall_accuracy"]` while adding `metrics["answer_text_accuracy"]` and `metrics["grounded_accuracy"]`.
   * `summary.md` reports Answer Text Accuracy, Grounded Accuracy, Overall Accuracy (Legacy), and Refusal Accuracy.
   * `benchmark_comparator.py` includes `answer_text_accuracy_delta` and `grounded_accuracy_delta`.

2. **Grounded Correctness Gating**:
   * **Infrastructure Failures**: If `is_infra_error` is True, grounded scoring immediately evaluates to False (`reason: "infrastructure_error"`).
   * **Text Correctness Prerequisite**: If `not is_answer_text_correct`, grounded scoring evaluates to False (`reason: "answer_text_incorrect"`).
   * **Refusal Contracts**: For refusal queries, a valid refusal requires zero citations. A response providing a refusal with zero citations passes grounded scoring (`reason: "valid_unsupported_refusal"`). Fabricating citations on a refusal fails grounded scoring (`reason: "refusal_contract_with_fabricated_citations"`).
   * **Answerable Queries**:
     * Missing citations (`citations == []`) immediately fails grounded scoring (`reason: "missing_citations"`).
     * Citations with missing fields (e.g. missing `chunk_id`, missing `document_filename`) fail citation validation and grounded scoring.
     * Citations referencing wrong source documents (e.g. citing `2212.14776v3.pdf` when `SEC-Form-10Q.pdf` is expected) fail citation validation and grounded scoring.
     * Citations referencing wrong document versions (e.g. version 2 instead of version 1) fail citation validation and grounded scoring.
     * Citations that alter document filename or version relative to retained request evidence items fail citation validation and grounded scoring.
   * **Claim Support Verification (Matching IDs alone do not establish support)**:
     * **Structured Aggregates**: Citations must map to retained structured execution evidence matching `selection_hash`, `table_id`, `operator`, `target_column`, and `document_version`. Furthermore, the answer text must assert the operation-specific `result_value` rather than metadata counts like `selection_count` or `total_rows`.
     * **Text Retrieval**: Citations must contain non-empty context snippets. The answer must satisfy lexical faithfulness (`compute_faithfulness(answer, joined_context) >= 0.25`). Any numeric amounts or percentages claimed in the answer must be present in the cited context snippets or explicitly grounded in contract target calculation values.

3. **Preservation of Contradictory Answer Text Defect**:
   * As explicitly specified in the requirements, the known contradictory-answer grading defect in lexical text scoring remains open.
   * For question Q136 ("What is Apple's Commission File Number?"), the contradictory answer "According to the SEC filing, Apple's Commission File Number is definitely NOT 001-36743" continues to evaluate to `is_correct = True` in text scoring due to token overlap. A dedicated regression test verifies this defect remains open in text scoring.

## Code Changes Inspected

1. [backend/src/services/evaluation/benchmark_scorer.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/evaluation/benchmark_scorer.py):
   * Added `extract_numbers_normalized` to reliably extract numbers, integers, floats, and percentages across commas and units.
   * Enhanced `validate_citations` with optional `expected_sources` and `expected_source_version` parameters. Added verification that citations do not alter document filename, document version, or document ID from retained request evidence items.
   * Implemented `evaluate_grounded_answer` implementing complete grounded answer verification.

2. [backend/scripts/run_canonical_60_benchmark.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/run_canonical_60_benchmark.py):
   * Imported `evaluate_grounded_answer`.
   * Evaluated grounded correctness for each query, recording `answer_text_correct`, `is_grounded_correct`, `grounded_correct`, and `grounded_evaluation_reason` in each query entry.
   * Computed `answer_text_accuracy` and `grounded_accuracy`, recording both in `manifest["metrics"]` while preserving `overall_accuracy`.
   * Updated `summary.md` table to display Answer Text Accuracy, Grounded Accuracy, Overall Accuracy (Legacy), and Refusal Accuracy.

3. [backend/src/services/evaluation/benchmark_comparator.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/evaluation/benchmark_comparator.py):
   * Added `answer_text_accuracy_delta` and `grounded_accuracy_delta` to manifest comparison deltas.

4. [backend/src/modules/query/query_service.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/modules/query/query_service.py):
   * Added `"retained_evidence_items"` to `response_dict` returned by `execute_query`.

5. [backend/tests/test_grounded_answer_evaluator.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_grounded_answer_evaluator.py):
   * Added 16 focused unit tests covering positive controls, negative controls (missing citations, invalid fields, wrong source, wrong version, altered retained evidence, low faithfulness, unsupported numeric claims, refusal with fabricated citations, structured aggregate claiming row count, infrastructure errors, text incorrect), and defect controls.

## Verification Evidence

* Focused evaluator test suite passed (57 passed in 3.75s):
  * `tests/test_grounded_answer_evaluator.py`: 16 passed
  * `tests/test_evaluator_defect_reproduction.py`: 15 passed
  * `tests/test_canonical_benchmark_runner_settings.py`: 6 passed
  * `tests/test_benchmark_integrity_defects.py`: 10 passed
  * `tests/test_citation_retained_evidence.py`: 10 passed
* Full backend test suite execution confirmed zero regressions.
* Benchmark scores remain provisional. Zero live provider calls, zero benchmark reruns, zero ground truth edits, zero corpus alterations, and zero changes to historical benchmark reports.
