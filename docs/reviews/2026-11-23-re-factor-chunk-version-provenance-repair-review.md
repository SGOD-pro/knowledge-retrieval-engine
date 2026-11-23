# Review: Chunk Version Provenance Repair and Scorer Diagnostic Framing

* **Author**: Senior Reviewer
* **Date**: 2026-11-23
* **Branch**: `re-factor`
* **Tested Commit**: `683b3d2f7677768df3cdc1cf2e9d11ca22585f26`
* **Supersedes**: [2026-11-23-re-factor-chunk-citation-provenance-repair-review.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-23-re-factor-chunk-citation-provenance-repair-review.md)

## Summary

This review documents the verification and review for repairing chunk version provenance resolution and clarifying offline evaluation diagnostics across the retrieval pipeline.

In the prior repair, `resolve_chunk_provenance` permitted an unversioned chunk to inherit the version of an active document snapshot whenever `cand_ver` was absent. This violated the core boundary that a document snapshot alone cannot supply a version for an unversioned chunk. Furthermore, an in memory rewrite of saved query Q144 was previously described as a verified benchmark recovery, when in reality it served strictly as an offline scorer diagnostic.

This bounded repair addresses both defects:
1. Provenance resolution now strictly enforces that a document snapshot alone cannot supply a version for an unversioned chunk. An unbound chunk resolves its canonical source filename from the snapshot while its version remains absent (`None`), even after the snapshot advances to subsequent versions. Only a verified binding to the exact chunk where declared version matches the snapshot version certifies the chunk version.
2. The Q144 manual rewrite test is reframed and renamed as an offline scorer diagnostic. Untouched historical artifacts in `query_details.json` remain preserved and unmodified, continuing to fail rescoring as originally observed. Benchmark recovery cannot be claimed from a simulated JSON rewrite, and will only be claimed if and when the actual live query path produces verified provenance.
3. Two offline controls replace the previous unversioned success test: proving an unbound chunk stays unversioned after the snapshot advances, and proving a verified chunk carries matching canonical filename and version through the actual query path.

## Architectural Changes

1. **Strict Version Authority in `response_builder.py`**:
   * In `resolve_chunk_provenance`, `cand_ver` must be verified against `snap_ver_str`.
   * When `cand_ver` is present and matches `snap_ver_str`, the verified canonical filename and snapshot version are returned.
   * When `cand_ver` is present but disagrees with `snap_ver_str`, the chunk retains its fallback filename and declared version, preventing unverified version relabeling.
   * When `cand_ver` is absent, the chunk resolves the canonical filename from the snapshot, but its version remains `None`. The snapshot version is never assigned to an unversioned chunk.
   * Preserves dynamic tombstone checks before delivery for both workspace and source levels.

2. **Offline Controls in `tests/test_chunk_citation_provenance_repair.py`**:
   * Control 1 (`test_unbound_chunk_stays_unversioned_after_snapshot_advances`): Demonstrates an unbound chunk resolving canonical filename while keeping version `None`, and proves that advancing the snapshot to a new version leaves the unbound chunk unversioned.
   * Control 2 (`test_verified_chunk_carries_matching_provenance_through_actual_query_path`): Demonstrates a verified chunk with matching version binding propagating canonical filename and version through `build_fast_path_response` and `evidence_item_from_chunk`.
   * Retained regressions: Cross workspace isolation (`test_wrong_workspace_binding_never_borrows_metadata`), version mismatch rejection (`test_version_mismatch_never_relabels_unverified_chunk`), and tombstone enforcement (`test_tombstone_checks_prevent_active_snapshot_certification`).

3. **Scorer Diagnostic Framing**:
   * `test_q144_manual_rewrite_scorer_diagnostic` explicitly confirms that the untouched saved Q144 record fails rescoring because historical benchmark artifacts are preserved intact.
   * The in memory rewrite is established strictly as an evaluator diagnostic showing that the scorer rules accept canonical provenance.
   * Documentation and test comments explicitly state that benchmark recovery is claimed only if the actual query path produces verified provenance.

## Verification Evidence

1. **Targeted Suite Execution**:
   * Executed `pytest tests/test_chunk_citation_provenance_repair.py`.
   * All 10 tests passed cleanly in 3.49 seconds, including `test_actual_query_path_snapshot_change_and_tombstone_before_delivery` verifying snapshot advances and tombstone blocking before delivery.

2. **Full Evaluation and Evidence Regression Suite**:
   * Executed 85 tests across all 7 evaluation suites:
     * `tests/test_chunk_citation_provenance_repair.py`: 10 passed
     * `tests/test_offline_benchmark_artifact_reproducibility.py`: 11 passed
     * `tests/test_grounded_answer_evaluator.py`: 35 passed
     * `tests/test_benchmark_guard.py`: 4 passed
     * `tests/test_benchmark_integrity_defects.py`: 10 passed
     * `tests/test_citation_retained_evidence.py`: 10 passed
     * `tests/test_evidence_providers.py`: 5 passed
   * All 85 tests passed cleanly with 0 skips and 0 failures.

3. **Preservation and Operational Discipline**:
   * Historical run artifacts in `reports/73075af21d9172155cad22866d8eef2e7c186edb/` remain completely untouched.
   * Scorer rules and ground truth remain intact.
   * Zero live provider calls.
   * Zero benchmark reruns.
   * Zero corpus writes.
