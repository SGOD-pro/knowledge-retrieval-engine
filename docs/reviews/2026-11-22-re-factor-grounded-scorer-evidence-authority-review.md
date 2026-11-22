# Review: Grounded Scorer Evidence Authority Repair

* **Author**: Senior Reviewer
* **Date**: 2026-11-22
* **Branch**: `re-factor`
* **Tested Commit**: `3852bbdc3f4d5c61c4812504adf41ff770c3051c`
* **Inspected Diff**: [3852bbd](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/3852bbdc3f4d5c61c4812504adf41ff770c3051c)
* **Supersedes**: [2026-11-22-re-factor-canonical-evaluator-grounded-scoring-review.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-22-re-factor-canonical-evaluator-grounded-scoring-review.md)

## Summary

This review records the verification and code review for the evidence authority repair in the canonical benchmark evaluator.

While the previous iteration introduced the grounded scoring framework (`is_grounded_correct` and `grounded_accuracy`), audit of edge cases uncovered three vulnerabilities where ungrounded answers could falsely pass:
1. **Fabricated Citation Snippets (False Pass 1)**: For text answers, the evaluator constructed claim context from citation dictionary snippets (`c.get("text")`). If a model cited a legitimate chunk identifier but fabricated the answer text inside its citation dictionary, the claim support check treated the fabricated snippet as ground truth context and granted a pass.
2. **Ground Truth Target Substitution (False Pass 2)**: For structured aggregate answers where execution failed to produce or retain a `result_value`, the evaluator previously populated `computed_results` with benchmark ground truth target values from the test contract. This permitted an answer matching the target to pass despite having zero retained execution results from the engine.
3. **Missing Result Fallback (False Pass 3)**: When a structured aggregate execution returned no computed result, an open fallback branch (`grounded_structured_aggregate_fallback`) marked the answer as grounded correct instead of failing.

This repair closes all three false pass pathways by making retained request evidence authoritative across citation validation, claim support, and aggregate evaluation.

## Core Rules and Implementation Details

1. **Text Answer Claim Support & Authoritative Retained Evidence**:
   * Context assembly for text answers now draws strictly from request retained evidence items via `_get_ev_text(matching_ev)`. Citation snippets provided in citation dictionaries are not used as claim ground truth.
   * `validate_citations` validates regular text citation snippets against the authoritative text of the matched retained evidence item. Any snippet that fabricates numbers not present in the authoritative retained text, or that falls below lexical faithfulness (0.40), is rejected with an explicit validation error.
   * Answerable queries with missing or empty authoritative retained evidence (`retained_evidence_items`) immediately fail grounded scoring (`reason: "missing_authoritative_evidence"`).

2. **Structured Answers Execution Result Enforcement**:
   * Ground truth target values from contracts are never added to `computed_results` for structured aggregate questions.
   * Structured answers require actual retained execution results (`explicit_results` extracted from `result_value` or `secondary_value` in the retained evidence item or outcome payload).
   * The missing result fallback branch (`grounded_structured_aggregate_fallback`) has been removed entirely. When `computed_results` is empty, grounded scoring fails (`reason: "missing_retained_result_value"`).

3. **Preservation of Legitimate Cases**:
   * **Legitimate COUNT Operations**: For COUNT operations, `selection_count` in metadata counts is legitimately used as the computed count result when no separate `result_value` is set. For non-count operations, metadata counts cannot substitute for `result_value`.
   * **Legitimate Zero-Result Answers**: `extract_numbers_normalized` recognizes "zero" and "none" as "0". Zero-result queries and answers asserting 0 or zero match correctly.
   * **Legitimate Derived Calculations**: In text retrieval, contract target values are allowed to satisfy numeric answers when the calculation operands or supporting figures are present in the authoritative retained context. When operands are missing from the context, derived calculations fail.
   * **Refusal Contracts**: Clean refusals with zero citations pass grounded scoring (`reason: "valid_unsupported_refusal"`). Fabricating citations on a refusal fails grounded scoring (`reason: "refusal_contract_with_fabricated_citations"`).

4. **Preserved Metrics & Explicitly Open Defect**:
   * Legacy text accuracy scores (`is_correct`, `answer_text_correct`, `overall_accuracy`, `answer_text_accuracy`) remain preserved and untouched.
   * The known contradictory answer grading defect in legacy text scoring remains explicitly open. A dedicated regression test verifies that contradictory answers continue to score True under legacy lexical scoring due to ungrounded token overlap.

## Code Changes Inspected

1. [benchmark_scorer.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/evaluation/benchmark_scorer.py):
   * Updated `extract_numbers_normalized` to support word representations of zero ("zero", "none").
   * Added `_get_ev_text` to extract authoritative text across `EvidenceItem`, namespace objects, and dictionaries.
   * Added `_ev_matches_citation` to match citations against retained evidence items via chunk identifier or selection hash.
   * Updated `validate_citations` to reject citation snippets that fabricate numbers or terms not present in the authoritative retained evidence item.
   * Updated `evaluate_grounded_answer` to mandate `retained_evidence_items` for answerable queries, enforce actual retained execution results for structured answers without contract target substitution, remove the missing result fallback, and assemble text claim support strictly from authoritative retained evidence items while preserving legitimate derived calculations.

2. [run_structured_aggregate.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/run_structured_aggregate.py):
   * Retained `result_value` and `secondary_value` strings in `structured_payload` when constructing `EvidenceItem` for structured aggregate queries.

3. [test_grounded_answer_evaluator.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_grounded_answer_evaluator.py):
   * Added regression test for False Pass 1 (`test_false_pass_1_fabricated_citation_snippet_fails`).
   * Added regression test for False Pass 2 (`test_false_pass_2_structured_aggregate_ground_truth_target_substitution_fails`).
   * Added regression test for False Pass 3 (`test_false_pass_3_structured_aggregate_missing_result_fallback_fails`).
   * Added regression test for missing authoritative evidence (`test_missing_authoritative_evidence_fails_grounded_scoring`).
   * Added positive controls for legitimate COUNT operations (`test_legitimate_count_operation_positive_control`), legitimate zero-result answers (`test_legitimate_zero_result_positive_control`), and legitimate derived calculations (`test_legitimate_derived_calculation_positive_control`).
   * Added negative control for unsupported derived calculations when operands are absent (`test_unsupported_derived_calculation_operands_missing_fails`).
   * Maintained defect control for contradictory answers in legacy text scoring (`test_contradictory_answer_defect_remains_open_in_text_scoring`).

4. [AGENTS.md](file:///home/swyra/projects/knowledge-retrieval-engine/backend/AGENTS.md):
   * Synchronized evaluator guidelines to document the authoritative request retained evidence and aggregate execution result requirements.

## Verification Evidence

* Focused evaluator test suite passed cleanly (75 passed in 5.47s):
  * `tests/test_grounded_answer_evaluator.py`: 24 passed (including all 3 false pass regressions and positive controls)
  * `tests/test_evaluator_defect_reproduction.py`: 15 passed
  * `tests/test_canonical_benchmark_runner_settings.py`: 6 passed
  * `tests/test_benchmark_integrity_defects.py`: 10 passed
  * `tests/test_citation_retained_evidence.py`: 10 passed
  * `tests/test_mandatory_audit_guards.py`: 10 passed
* Benchmark runner and persistence test suite passed cleanly (18 passed in 1.94s):
  * `tests/test_benchmark_runner_and_persistence_0004.py`: 18 passed
* Benchmark scores remain provisional. Zero live provider calls, zero benchmark reruns, zero ground truth edits, zero corpus alterations, and zero changes to historical benchmark artifacts.
