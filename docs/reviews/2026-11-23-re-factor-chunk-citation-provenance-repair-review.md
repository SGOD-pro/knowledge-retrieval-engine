# Review: Citation Provenance Repair for Chunk Backed Query Answers

* **Author**: Senior Reviewer
* **Date**: 2026-11-23
* **Branch**: `re-factor`
* **Tested Commit**: `2da52b08c850adab2427601992f9d77c14201a03`
* **Inspected Diff**: [2da52b0](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/2da52b08c850adab2427601992f9d77c14201a03)
* **Supersedes**: [2026-11-23-re-factor-canonical-run-20261123_075022-offline-audit.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-23-re-factor-canonical-run-20261123_075022-offline-audit.md)

## Summary

This review documents the verification and architectural code review for repairing citation provenance in chunk backed query answers across the retrieval pipeline.

During the offline audit of canonical benchmark run `20261123_075022`, query Q144 failed grounded answer evaluation because its citation carried the synthetic filename `Doc-fcc723b5` instead of the canonical source filename `NFHS_5_India_Districts_Factsheet_Data.xls`. Investigation localized the root cause to two decoupled locations:
1. In `response_builder.py`, `build_citation` only queried DynamoDB via `CloudRepository` without checking `WorkspaceBoundaryRepository`. When no entry existed in DynamoDB or cache, it fell back to `Doc-{id[:8]}`, and the `Citation` dataclass lacked a `document_version` attribute.
2. In `evidence_contract.py`, `evidence_item_from_chunk` fell back to `f"doc_{did}.pdf"`, causing retained evidence items and citations to diverge from each other and from the authoritative workspace snapshot.

This bounded repair introduces authoritative chunk provenance resolution through `resolve_chunk_provenance`, binding chunk provenance directly to request authorized workspace snapshots while strictly preventing cross workspace borrowing and unverified version relabeling.

## Core Rules and Implementation Details

1. **Authoritative Snapshot Resolution**:
   * Resolves canonical `source` and `version` from `WorkspaceBoundaryRepository.get_document_snapshot(workspace_id, document_id)`.
   * When an unversioned chunk (such as an XLS or tabular record) or a matching versioned chunk is verified in the active snapshot, both final citations and retained evidence items receive the authoritative filename and version.

2. **Rejection of Cross Workspace Borrowing**:
   * If a chunk carries an explicit workspace binding that differs from the request authorized workspace, the engine never queries or borrows metadata from the request workspace snapshot.
   * Prevents foreign chunks from being certified under an unauthorized workspace.

3. **Rejection of Unverified Version Relabeling**:
   * If a chunk specifies an explicit version that disagrees with the snapshot version, the engine never relabels the chunk as the snapshot version or certifies it with the snapshot source filename.
   * The chunk retains its declared version and fallback identifier, failing closed under version matching gates.

4. **Preservation of Tombstone Enforcement**:
   * Tombstoned workspaces and tombstoned document sources block active snapshot certification, ensuring revoked sources cannot acquire active provenance.

5. **Parity Between Citations and Retained Evidence Across Pipeline Paths**:
   * Both `build_citation` and `evidence_item_from_chunk` use `resolve_chunk_provenance`.
   * `Citation` now includes `document_version: str | None = None`.
   * `EvidenceItem` now includes `document_filename: str | None = None`.
   * `ResponseObject` enforces mutual parity between final citations and retained evidence items for `document_filename` and `document_version`.
   * Fast query path and full query path both maintain identical canonical filename and version bindings.

## Verification Evidence

1. **Focused Regression Suite (`tests/test_chunk_citation_provenance_repair.py`)**:
   * `test_xls_chunk_missing_filename_metadata_resolves_canonical_provenance`: Verified that unversioned XLS chunks lacking metadata resolve `NFHS_5_India_Districts_Factsheet_Data.xls` and version `e482b990d74eb46b5ce5a6e10606b745`.
   * `test_wrong_workspace_binding_never_borrows_metadata`: Verified that chunks with foreign workspace bindings never borrow filename or version from the request workspace.
   * `test_version_mismatch_never_relabels_unverified_chunk`: Verified that unverified versions are never relabeled as current snapshot versions.
   * `test_tombstone_checks_prevent_active_snapshot_certification`: Verified that source level and workspace level tombstones prevent active certification.
   * `test_fast_query_path_citation_and_evidence_parity`: Verified that fast query path responses preserve identical canonical filename and version across citations and retained evidence.
   * `test_full_query_path_citation_and_evidence_parity`: Verified that full query path responses preserve identical canonical filename and version across citations and retained evidence.
   * `test_saved_q144_recovery_under_authoritative_provenance`: Verified that saved canonical record Q144 recovers from `is_grounded_correct: False` (`Doc-fcc723b5 not in expected sources`) to `is_grounded_correct: True`, `grounded_correct: True`, `reason: 'grounded_and_verified'` under offline rescoring.
   * `test_q149_and_q193_remain_isolated_and_unaltered`: Verified that saved records Q149 (tokenizer recall rejection on 99.58%) and Q193 (candidate selection of 23,245 over 30,739) maintain their exact original failure statuses and reasons without modification.

2. **Full Regression Suite Pass**:
   * Executed 83 tests across all 7 evaluation and evidence test suites:
     * `tests/test_chunk_citation_provenance_repair.py`: 8 passed
     * `tests/test_offline_benchmark_artifact_reproducibility.py`: 11 passed
     * `tests/test_grounded_answer_evaluator.py`: 35 passed
     * `tests/test_benchmark_guard.py`: 4 passed
     * `tests/test_benchmark_integrity_defects.py`: 10 passed
     * `tests/test_citation_retained_evidence.py`: 10 passed
     * `tests/test_evidence_providers.py`: 5 passed
   * All 83 tests passed cleanly in 4.69 seconds.

3. **Operational Discipline**:
   * Zero live provider calls.
   * Zero benchmark reruns.
   * Zero ground truth edits.
   * Zero corpus modifications.
   * All historical benchmark artifacts preserved intact.
