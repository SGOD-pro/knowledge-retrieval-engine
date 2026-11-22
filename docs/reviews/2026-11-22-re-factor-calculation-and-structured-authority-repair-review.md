# Review: Calculation and Structured Evidence Authority Repair

* **Author**: Senior Reviewer
* **Date**: 2026-11-22
* **Branch**: `re-factor`
* **Tested Commit**: `6246ac4031ecb85614b8a499c30a0ccb9f74c141`
* **Inspected Diff**: [6246ac4](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/6246ac4031ecb85614b8a499c30a0ccb9f74c141)
* **Supersedes**: [2026-11-22-re-factor-grounded-scorer-evidence-authority-review.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-22-re-factor-grounded-scorer-evidence-authority-review.md)

## Summary

This review records the verification and code review for closing the remaining evidence authority gaps in the canonical benchmark evaluator.

While earlier repairs ensured that authoritative request retained evidence items are mandatory and removed ground truth target substitution, two specific calculation authority gaps remained open:
1. **Partial Operand Retention and Arbitrary Context Unlocking**: In derived calculations, the evaluator previously checked whether any operand intersected with context numbers. If only one of two required operands was retained, or if a contract specified no operands while arbitrary unrelated numbers appeared in the context, contract targets were unlocked as accepted results.
2. **Citation Injection of Accepted Structured Results**: In structured aggregates, the evaluator previously inspected citation dictionaries, their secondary values, and nested payloads for accepted results. This created a loophole where an adversarial citation could introduce an unexecuted result value (such as a fabricated secondary value of 999 alongside a retained SUM of 100), and accept an answer claiming that ungrounded number.

This repair closes both gaps. It requires every necessary operand before unlocking derived calculation targets, prohibits arbitrary context numbers from unlocking targets, and binds structured results strictly to matching authoritative retained executions.

## Core Rules and Implementation Details

1. **All Necessary Operands Required for Derived Results**:
   * For text retrieval questions evaluated under numeric contracts, every operand specified in the contract must have a normalized match in the authoritative context text.
   * If even one necessary operand is missing from the retained context, the derived calculation target is not unlocked, and answers claiming that derived number fail grounded scoring.
   * If a contract specifies no operands, arbitrary context numbers never unlock contract targets. Verbatim amounts present directly in the context remain supported, while ungrounded derived claims fail.

2. **Structured Results Obtained Strictly from Matching Retained Executions**:
   * In `evaluate_grounded_answer`, structured results (`explicit_results` and `metadata_counts`) are extracted solely from matching authoritative retained evidence items.
   * Citation dictionaries (`c`), citation result values, citation secondary values, and nested citation payloads are never inspected for accepted results.
   * In `validate_citations`, structured citations are validated against the matched retained execution item. Any citation that fabricates or alters `result_value`, `secondary_value`, or nested payload fields relative to the retained execution is rejected with an explicit error.

3. **Preservation of Legitimate Capabilities**:
   * **Supported Derived Calculations**: When all required operands appear in the authoritative retained context, legitimate derived calculations pass grounded scoring.
   * **Legitimate COUNT Operations**: For count operations, metadata selection counts are legitimately accepted as the count result when no separate result value is specified.
   * **Legitimate Zero Results**: Normalized matching recognizes both numeric 0 and textual words (zero, none) as 0, allowing valid zero result queries to pass.
   * **Clean Refusal Contracts**: Refusal queries with zero citations pass, while fabricating citations on a refusal fails.

4. **Preserved Metrics and Explicit Contradiction Defect**:
   * Legacy text metrics (`is_correct`, `answer_text_correct`, `overall_accuracy`, `answer_text_accuracy`) remain preserved.
   * The known contradictory answer grading defect in lexical text scoring remains explicitly open. A dedicated regression test verifies that contradictory answers continue to score True under legacy lexical scoring due to ungrounded token overlap.

## Code Changes Inspected

1. [benchmark_scorer.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/evaluation/benchmark_scorer.py):
   * Enhanced `validate_citations` to check both top level and nested `secondary_value` and `result_value` in structured citations against matching retained evidence items, rejecting fabricated or altered values.
   * Updated `evaluate_grounded_answer` to extract structured execution results strictly from matching authoritative retained executions (`matching_structured_items`), removing inspection of citation dictionaries and nested citation payloads.
   * Updated derived calculation validation in `evaluate_grounded_answer` to require that every necessary operand in `contract["operands"]` has a match in `context_nums`. Prohibited arbitrary context numbers from unlocking contract targets when operands are absent.

2. [test_grounded_answer_evaluator.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_grounded_answer_evaluator.py):
   * Added regression test `test_derived_calculation_partial_operands_retained_fails` proving that retaining only one of two operands fails grounded scoring.
   * Added regression test `test_arbitrary_context_numbers_do_not_unlock_contract_targets` proving that arbitrary context numbers cannot unlock contract target values.
   * Added control test `test_retained_sum_100_fabricated_secondary_value_999_control` proving that a retained SUM of 100 with a citation fabricating `secondary_value=999` fails citation validation, ignores the fabricated 999, and rejects an answer claiming 999.

3. [AGENTS.md](file:///home/swyra/projects/knowledge-retrieval-engine/backend/AGENTS.md):
   * Updated canonical benchmark evaluation conventions to document the requirement for all calculation operands and the strict binding of structured results to matching retained executions.

## Verification Evidence

* Evaluator and runner test suite passed cleanly (96 passed in 5.29s):
  * `tests/test_grounded_answer_evaluator.py`: 27 passed
  * `tests/test_evaluator_defect_reproduction.py`: 15 passed
  * `tests/test_canonical_benchmark_runner_settings.py`: 6 passed
  * `tests/test_benchmark_integrity_defects.py`: 10 passed
  * `tests/test_citation_retained_evidence.py`: 10 passed
  * `tests/test_mandatory_audit_guards.py`: 10 passed
  * `tests/test_benchmark_runner_and_persistence_0004.py`: 18 passed
* Benchmark scores remain provisional. Zero live provider calls, zero benchmark reruns, zero ground truth edits, zero corpus alterations, and zero changes to historical benchmark artifacts.
