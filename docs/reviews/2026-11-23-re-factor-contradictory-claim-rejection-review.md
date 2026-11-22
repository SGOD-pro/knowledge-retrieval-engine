# Review: Contradictory Claim Rejection in Grounded Answer Scoring

* **Author**: Senior Reviewer
* **Date**: 2026-11-23
* **Branch**: `re-factor`
* **Tested Commit**: `c8643b5e5ec6d5b6ea8d82b91160ef30153a1ab0`
* **Inspected Diff**: [c8643b5](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/c8643b5e5ec6d5b6ea8d82b91160ef30153a1ab0)
* **Supersedes**: [2026-11-22-re-factor-calculation-and-structured-authority-repair-review.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-22-re-factor-calculation-and-structured-authority-repair-review.md)

## Summary

This review records the verification and code review for rejecting contradictory claims in grounded answer scoring within the canonical benchmark evaluator.

Previously, grounded answer scoring checked lexical faithfulness and numeric presence against retained context snippets without validating claim polarity or negation. This allowed contradictory answers (such as Q136 where the answer explicitly claimed Apple's Commission File Number was definitely NOT 001-36743) to pass grounded scoring because the key tokens and numbers existed in the context.

This repair implements claim level contradiction detection against authoritative retained evidence. If an answer explicitly denies or contradicts facts affirmed in the authoritative context, or if it asserts facts that the context explicitly denies, grounded scoring fails. Blanket rejection of negation words was avoided so that legitimate premise corrections and valid refusals continue to pass. Historical legacy text metrics remain preserved separately.

## Core Rules and Implementation Details

1. **Claim Level Contradiction Detection**:
   * Implemented `detect_contradiction_against_evidence` to detect polarity conflicts between answer statements and authoritative evidence.
   * Text is parsed into clauses using contrast conjunctions (but, however, instead of, rather than, although, whereas, while, yet) and non numeric punctuation, preserving numeric decimals and thousands separators.
   * Candidate facts evaluated include alphanumeric identifiers (such as `001-36743`), normalized numbers, contract target values, and retained structured aggregate results.
   * If an entity is affirmed in evidence but negated in any clause of the answer, grounded scoring fails with `claim_contradicts_evidence`.
   * If an entity is explicitly negated in evidence but affirmed in the answer, grounded scoring fails with `claim_contradicts_evidence`.

2. **Avoiding Blanket Rejection for Premise Corrections and Refusals**:
   * Blanket rejection of words like "not" or "no" was avoided.
   * In premise corrections (such as correcting a question that asks whether EPS was $5.00), the answer may say "No, basic EPS is not $5.00, it is $2.03".
   * Because $5.00 was never in the evidence context, negating $5.00 does not contradict the evidence.
   * The evidence affirmed $2.03, which the answer asserts positively without negation.
   * Numeric claim validation now checks asserted amounts (filtering out numbers explicitly negated in the answer), ensuring legitimate premise refutations pass without false rejection.
   * Clean refusals with zero citations for refusal contracts continue to pass via the dedicated refusal gate.

3. **Application Across Structured and Text Retrieval**:
   * In structured aggregate questions, `detect_contradiction_against_evidence` verifies that the answer does not negate the retained computed result value.
   * In text retrieval questions, `detect_contradiction_against_evidence` checks the answer against the joined authoritative context text immediately following faithfulness verification.

4. **Preserved Metrics and Explicit Defect Isolation**:
   * Legacy text metrics (`is_correct`, `answer_text_correct`, `overall_accuracy`, `answer_text_accuracy`) remain preserved separately.
   * The known contradictory answer grading defect in lexical text scoring remains open in text scoring and is covered by `test_contradictory_answer_defect_remains_open_in_text_scoring`.

## Code Changes Inspected

1. [benchmark_scorer.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/evaluation/benchmark_scorer.py):
   * Added negation vocabulary, clause segmentation, tokenization, and `detect_contradiction_against_evidence`.
   * Integrated contradiction checks into `evaluate_grounded_answer` for both structured aggregate and text retrieval paths.
   * Updated numeric claim validation to filter out explicitly negated numbers so legitimate premise refutations pass.

2. [test_grounded_answer_evaluator.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_grounded_answer_evaluator.py):
   * Added `test_control_1_supported_assertion_passes_grounded_scoring` proving supported assertions pass.
   * Added `test_control_2_explicit_denial_reproduction_fails_grounded_scoring` reproducing Q136 denial failure in grounded scoring while proving legacy text scoring continues to score True.
   * Added `test_control_3_conflicting_assertions_fails_grounded_scoring` covering both conflicting clauses and alternative number assertions denying evidence values.
   * Added `test_control_4_valid_premise_correction_passes_grounded_scoring` proving premise corrections starting with "No." or negating false premises pass.
   * Added `test_control_5_valid_refusal_passes_grounded_scoring` proving clean refusals pass.
   * Added `test_control_6_structured_aggregate_denial_fails_grounded_scoring` proving structured aggregate denials fail.

3. [AGENTS.md](file:///home/swyra/projects/knowledge-retrieval-engine/backend/AGENTS.md):
   * Updated canonical benchmark evaluation conventions to record contradiction rejection.

## Verification Evidence

* Evaluator and runner test suite passed cleanly (102 passed in 5.67s):
  * `tests/test_grounded_answer_evaluator.py`: 33 passed
  * `tests/test_evaluator_defect_reproduction.py`: 15 passed
  * `tests/test_canonical_benchmark_runner_settings.py`: 6 passed
  * `tests/test_benchmark_integrity_defects.py`: 10 passed
  * `tests/test_citation_retained_evidence.py`: 10 passed
  * `tests/test_mandatory_audit_guards.py`: 10 passed
  * `tests/test_benchmark_runner_and_persistence_0004.py`: 18 passed
* Benchmark scores remain provisional. Zero live provider calls, zero benchmark reruns, zero ground truth edits, zero corpus alterations, and zero changes to historical benchmark artifacts.
