# Review: Negation Scope Binding and Contradiction Helper Isolation

* **Author**: Senior Reviewer
* **Date**: 2026-11-23
* **Branch**: `re-factor`
* **Tested Commit**: `f630ab9d30b83dd51877615bb935118e4a1903de`
* **Inspected Diff**: [f630ab9](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/f630ab9d30b83dd51877615bb935118e4a1903de)
* **Supersedes**: [2026-11-23-re-factor-contradictory-claim-rejection-review.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-23-re-factor-contradictory-claim-rejection-review.md)

## Summary

This review records the verification and code review for binding negation scope strictly to its target claim and isolating contradiction helpers in the canonical benchmark evaluator.

In the initial contradiction detection implementation, two issues emerged:
1. Negation was checked within token windows without enforcing syntactic binding. An answer stating that Apple's Commission File Number was `001-36743 and not 999-99999` was incorrectly flagged as negating `001-36743` because `not` appeared two tokens after it. Furthermore, component parts of hyphenated numbers (`999` and `99999`) were not recognized by token matching, leaving them in asserted amounts and failing numeric verification.
2. The newly introduced clause splitter was named `_split_into_clauses`, inadvertently overriding the existing line 373 helper relied upon by `grade_structured_numeric` for role-based numeric grading.

This repair resolves both issues. Negation is bound strictly to its claim using syntactic constraints, hyphenated token parts are recognized, and the contradiction clause splitter is given a distinct name (`_split_into_contradiction_clauses`), restoring the original numeric splitter and verifying legacy grading behavior.

## Core Rules and Implementation Details

1. **Strict Negation Scope Binding**:
   * Preceding negation: A preceding negation word governs a target only when at most 4 tokens away, with no intervening numbers, entities, or coordinating conjunctions. If another number or conjunction (`and`, `but`, `or`) intervenes, the negation binds to the intervening claim instead of jumping across.
   * Following negation: A following negation word governs a target only in copular predicate constructions (such as `001-36743 is not` or `001-36743 is incorrect`), where all intervening tokens are copular linking verbs or adverbs (`is`, `was`, `are`, `were`, `definitely`, `clearly`). Conjunctions like `and` or `,` between a target and a following `not` mean the negation introduces a new conjunct rather than predicating of the target.

2. **Hyphenated Token Matching in Asserted Amount Filtering**:
   * Enhanced `_target_in_token` to inspect component parts of hyphenated tokens (`tok_clean.split("-")`).
   * Component numbers such as `999` and `99999` from `999-99999` are recognized as negated when the identifier is negated, correctly excluding them from asserted amounts and allowing valid corrections to pass numeric verification.

3. **Helper Isolation and Preserving Legacy Numeric Grading**:
   * Renamed the contradiction splitter to `_split_into_contradiction_clauses`.
   * Preserved the original line 373 `_split_into_clauses` helper untouched, ensuring `grade_structured_numeric` maintains its original parsing for comma-separated numbers (such as `4,191`) and role keyword bindings.

4. **Comprehensive Control Coverage**:
   * **Valid Correction (Order 1)**: `Apple Commission File Number is 001-36743 and not 999-99999.` passes grounded scoring.
   * **Valid Correction (Order 2A)**: `Apple Commission File Number is not 999-99999 and is 001-36743.` passes grounded scoring.
   * **Valid Correction (Order 2B)**: `Apple Commission File Number is not 999-99999, it is 001-36743.` passes grounded scoring.
   * **Explicit Denial Negative Controls**: Preceding denial, copular denial (`001-36743 is not...`), and predicate adjective denial (`001-36743 is incorrect`) fail grounded scoring.
   * **Conflicting Assertion Negative Controls**: Answers affirming then denying evidence numbers across `and` and `but`, and answers asserting alternative numbers while denying evidence numbers across both orders fail grounded scoring.
   * **Premise Corrections and Refusals**: Legitimate premise refutations and clean refusals continue to pass without false rejection.

## Code Changes Inspected

1. [benchmark_scorer.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/evaluation/benchmark_scorer.py):
   * Renamed contradiction clause splitter to `_split_into_contradiction_clauses`, preserving line 373 `_split_into_clauses`.
   * Added `_COPULAR_AND_ADVERB_TOKENS` and `_CONJUNCTION_AND_BOUNDARY_TOKENS` to enforce strict negation scoping.
   * Updated `_is_target_negated_in_clause` to prevent negation from jumping over intervening entities or coordinating conjunctions.
   * Updated `_target_in_token` to match component parts of hyphenated tokens.

2. [test_grounded_answer_evaluator.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_grounded_answer_evaluator.py):
   * Added `test_control_1b_valid_correction_both_orders_pass_grounded_scoring` verifying both orders of valid corrections pass.
   * Expanded `test_control_2_explicit_denial_reproduction_fails_grounded_scoring` covering preceding, copular, and predicate adjective denials.
   * Expanded `test_control_3_conflicting_assertions_fails_grounded_scoring` covering all four permutations of conflicting assertions.
   * Added `test_control_7_legacy_numeric_clause_splitter_is_not_overridden` proving `_split_into_clauses` preserves commas inside numbers and `grade_structured_numeric` runs cleanly.

3. [AGENTS.md](file:///home/swyra/projects/knowledge-retrieval-engine/backend/AGENTS.md):
   * Updated canonical benchmark evaluation conventions to note negation scope binding and helper isolation.

## Verification Evidence

* Evaluator and runner test suites passed cleanly (104 passed in 6.87s):
  * `tests/test_grounded_answer_evaluator.py`: 35 passed
  * `tests/test_evaluator_defect_reproduction.py`: 15 passed
  * `tests/test_canonical_benchmark_runner_settings.py`: 6 passed
  * `tests/test_benchmark_integrity_defects.py`: 10 passed
  * `tests/test_citation_retained_evidence.py`: 10 passed
  * `tests/test_mandatory_audit_guards.py`: 10 passed
  * `tests/test_benchmark_runner_and_persistence_0004.py`: 18 passed
* Benchmark scores remain provisional. Zero live provider calls, zero benchmark reruns, zero ground truth edits, zero corpus alterations, and zero changes to historical benchmark artifacts.
