# Review: Offline Canonical Benchmark Artifact Reproducibility and Scorer Version 2.2.0

* **Author**: Senior Reviewer
* **Date**: 2026-11-23
* **Branch**: `re-factor`
* **Tested Commit**: `ba787b19283ba4ba4287be177b963172e2764951`
* **Inspected Diff**: [ba787b1](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/ba787b19283ba4ba4287be177b963172e2764951)
* **Supersedes**: [2026-11-23-re-factor-canonical-benchmark-preflight-review.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-23-re-factor-canonical-benchmark-preflight-review.md)

## Summary

This review validates the implementation making canonical benchmark artifacts reproducible offline, introducing distinct evaluator version `2.2.0`, restoring typed `EvidenceItem` representations from serialized query details, and correcting preflight runtime explanations.

Prior to this change, `run_canonical_60_benchmark.py` evaluated `retained_evidence_items` in memory during live query execution, but omitted them from the saved `query_details.json` output artifact. Consequently, offline evaluators could not reproduce grounded grades without live database and vector store access, failing answerable queries with `missing_authoritative_evidence`.

This implementation resolves the gap. All request retained evidence items and required evaluation inputs are serialized using existing `EvidenceItem.to_dict()`, restored to typed `EvidenceItem` representations via `EvidenceItem.from_dict` and `restore_evidence_item`, and rescored offline via `rescore_query_record_offline`. Strict comparator gates in `compare_manifests` are preserved, historical `2.1.0` benchmark artifacts remain untouched, and all 115 evaluator regression tests pass.

## Core Rules and Implementation Details

1. **EvidenceItem Typed Serialization and Restoration**:
   * Added `from_dict(cls, data: dict[str, Any]) -> EvidenceItem` on `EvidenceItem` in `services.retrieval.evidence_contract`.
   * Added `restore_evidence_item(item: Any) -> Any` supporting transparent restoration of dictionaries to typed `EvidenceItem` objects while preserving existing typed instances and mock doubles.
   * `EvidenceItem.to_dict()` provides standard dictionary serialization matching dataclass fields.

2. **Durable Request-Retained Evidence and Input Persistence**:
   * In `scripts/run_canonical_60_benchmark.py`, query result records (`res_entry`) now serialize `retained_evidence_items` using `to_dict()`.
   * Preserved all evaluation inputs needed for offline scoring: `contract_type`, `contract`, `question_type`, `expected_sources`, `expected_source_version`, `ground_truth_chunk_ids`, `is_infra_error`, `ans_is_refusal`, `structured_aggregate`, and `execution_type`.

3. **Offline Scoring Function**:
   * Implemented `rescore_query_record_offline(record: dict[str, Any]) -> dict[str, Any]` in `services.evaluation.benchmark_scorer`.
   * Deserializes `retained_evidence_items` to typed `EvidenceItem` instances.
   * Calls `evaluate_grounded_answer` with preserved evaluation inputs, reproducing identical grounded correctness verdicts, rejection reasons, citation validation results, and faithfulness scores directly from saved JSON artifacts.

4. **Distinct Evaluator Scorer Version 2.2.0**:
   * Defined `SCORER_VERSION = "2.2.0"` in `services.evaluation.benchmark_scorer` and referenced it in `scripts/run_canonical_60_benchmark.py` (runner version `2.2.0`).
   * Preserves strict comparator gates: `compare_manifests` rejects comparisons between candidate `2.2.0` manifests and historical `2.1.0` manifests, protecting benchmark integrity while enabling controlled comparison runs on matching `2.2.0` baselines.

5. **Preflight Reranker Reconciliation and Remote-First Accuracy**:
   * Corrected the circuit breaker explanation: the benchmark runtime is remote first. On each of the 42 retrieval queries, the engine attempts an HTTP request to OpenRouter for model `nvidia/llama-nemotron-rerank-vl-1b-v2:free`.
   * The daily quota circuit breaker (`_trip_circuit`) only trips on HTTP 429 quota exhaustion messages; for HTTP 404 client errors, each retrieval query executes remote first, records 1 remote call, and immediately falls back fail safe to deterministic BM25Plus lexical scoring (`_term_coverage_fallback`).
   * Total remote OpenRouter calls across the benchmark: 42 remote calls attempted (remote first on each retrieval question, failing fast on 404 with 0 financial cost).
   * Exact one run command verified with zero trailing spaces after line continuation backslashes.

## Code Changes Inspected

1. [evidence_contract.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/evidence_contract.py):
   * Added `from_dict` classmethod to `EvidenceItem`.
   * Added `restore_evidence_item` helper.

2. [benchmark_scorer.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/evaluation/benchmark_scorer.py):
   * Defined `SCORER_VERSION = "2.2.0"`.
   * Updated `evaluate_grounded_answer` and `validate_citations` to restore dictionary items in `retained_evidence_items` to typed `EvidenceItem` instances.
   * Added `rescore_query_record_offline` for offline query record evaluation.

3. [run_canonical_60_benchmark.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/run_canonical_60_benchmark.py):
   * Updated manifest to use `runner_version: "2.2.0"` and `scorer_version: SCORER_VERSION`.
   * Added `retained_evidence_items` and evaluation inputs persistence in `res_entry`.

4. [test_offline_benchmark_artifact_reproducibility.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_offline_benchmark_artifact_reproducibility.py):
   * 11 comprehensive unit and round trip tests proving identical offline scoring for supported text, structured results, refusals, negative controls, and comparator version gates.

5. [2026-11-23-re-factor-canonical-benchmark-preflight-review.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-23-re-factor-canonical-benchmark-preflight-review.md):
   * Corrected reranker circuit breaker and call estimate sections to reflect remote first behavior and 42 remote calls attempted.

6. [AGENTS.md](file:///home/swyra/projects/knowledge-retrieval-engine/backend/AGENTS.md):
   * Updated canonical benchmark evaluation conventions with scorer version 2.2.0 and typed evidence persistence.

## Verification Evidence

All 115 evaluator regression tests passed cleanly in 5.84s:
* `tests/test_offline_benchmark_artifact_reproducibility.py`: 11 passed
* `tests/test_grounded_answer_evaluator.py`: 35 passed
* `tests/test_evaluator_defect_reproduction.py`: 15 passed
* `tests/test_canonical_benchmark_runner_settings.py`: 6 passed
* `tests/test_benchmark_integrity_defects.py`: 10 passed
* `tests/test_citation_retained_evidence.py`: 10 passed
* `tests/test_mandatory_audit_guards.py`: 10 passed
* `tests/test_benchmark_runner_and_persistence_0004.py`: 18 passed

Zero live provider calls, zero benchmark reruns, zero ground truth edits, zero corpus alterations, and zero changes to historical benchmark artifacts. Benchmark execution remains strictly pending user authorization.
