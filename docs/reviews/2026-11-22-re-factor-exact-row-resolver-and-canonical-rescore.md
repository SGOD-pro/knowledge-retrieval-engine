# Review: Exact Row Resolver and Offline Canonical Rescore

**Author**: Senior Software Engineer
**Date**: 2026-11-22
**Branch**: `re-factor`
**Published Commit**: `d83668b0cdeef2155fdc5bc0c506f3d4ff08c2b0`
**Inspected Diff**: [d83668b](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/d83668b0cdeef2155fdc5bc0c506f3d4ff08c2b0)

## Summary

This review validates the exact row matching fix in the canonical benchmark evaluator, verifies the regression suite, and reconciles the offline retrieval metric rescore for the cold benchmark run `20261122_182354`.

All changes have been published to `origin/re-factor` under commit `d83668b0cdeef2155fdc5bc0c506f3d4ff08c2b0`. The original benchmark artifacts remain preserved and untouched, and the offline rescore artifacts are stored separately.

## Published Code Changes

The commit touches two files:
1. [backend/scripts/run_canonical_60_benchmark.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/run_canonical_60_benchmark.py#L151-L260):
   * Replaced substring matching in `resolve_ground_truth_chunk_ids` with exact row resolution.
   * Priority 1: When explicit row metadata exists on the chunk (`row`, `row_number`, `row_idx`, or `row_id`), the evaluator compares the integer value directly against the target locator row. Location references are not evaluated if explicit row metadata is present.
   * Priority 2: When explicit row metadata is absent, the evaluator extracts exact integer row numbers using regular expression `(?i)\bRow\s*:?\s*(\d+)\b`. This prevents substring matches such as row 4 matching row 40, 41, 400, or 499.
2. [backend/tests/test_evaluator_defect_reproduction.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_evaluator_defect_reproduction.py#L196-L315):
   * Added `test_defect_10_q149_exact_row_matching` verifying:
     * Row 4 resolves while rows 40 and 400 do not match when explicit metadata is present.
     * A fixture with only rows 40 and 400 marks Q149 as unscorable.
     * Row 4 resolves while rows 40 and 400 do not match when metadata is absent and location reference strings are parsed.
     * A fixture without explicit metadata containing only rows 40 and 400 marks Q149 as unscorable.

## Test Verification

Focused evaluator test suites executed on clean HEAD `d83668b0cdeef2155fdc5bc0c506f3d4ff08c2b0`:
* [backend/tests/test_evaluator_defect_reproduction.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_evaluator_defect_reproduction.py): 15 passed in 2.50s
* [backend/tests/test_canonical_benchmark_runner_settings.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_canonical_benchmark_runner_settings.py): 6 passed in 1.20s
* [backend/tests/test_benchmark_integrity_defects.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_benchmark_integrity_defects.py): 10 passed in 1.45s
* [backend/tests/test_benchmark_runner_and_persistence_0004.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_benchmark_runner_and_persistence_0004.py): 18 passed in 5.30s
* Total: 49 passed, 0 failed, 0 skipped.

## Artifact Separation and Input Verification

The offline rescore was conducted completely offline with zero provider calls, zero Qdrant writes, zero benchmark reruns, and zero changes to ground truth.

Inputs verified against original run `20261122_182354`:
* Saved rankings: [backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/query_details.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/query_details.json) (SHA-256: `830669af6c44940e5ecb05dde5cb6f9b376f071db04c5baa52c1fe283b1f32a3`). All 60 query candidates and answers were read directly from this file.
* Ground truth file: [backend/evaluation_assets/canonical_60_ground_truth.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/evaluation_assets/canonical_60_ground_truth.json) (SHA-256: `c0e89a4b3d7097c6cdb398a7d51f7cf0dfebb8fba2f71989b7c3defb823c5c96`). Unchanged.
* Canonical chunk inventory: read only scan of workspace `ws_canonical_comparison_0004` from TableStore DynamoDB `ap-south-1`. Exactly 6,697 chunks loaded, identical to the canonical point inventory.
* Suite metadata: [data/test.json](file:///home/swyra/projects/knowledge-retrieval-engine/data/test.json) (SHA-256: `233bbd9dd19d02c0055453e9bcc40a878a8398bfd4a274b263b19dc11f194058`), matches `test_suite_sha256` recorded in [backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/manifest.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/manifest.json).

Separation of artifacts:
* Original run artifacts preserved unmodified:
  * [backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/manifest.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/manifest.json)
  * [backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/query_details.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/query_details.json)
  * [backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/summary.md](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/summary.md)
* Separate follow up rescore artifacts:
  * [backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/offline_retrieval_rescore_followup.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/offline_retrieval_rescore_followup.json)
  * [backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/offline_rescored_query_details.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/offline_rescored_query_details.json)

## Denominator and Answer Score Reconciliation

* Total questions: 60
* Scorable retrieval questions denominator: 53 (unchanged, all 53 answerable retrieval queries resolved cleanly with 0 unscorable queries)
* Answerable structured aggregate questions: 2 (excluded from retrieval metrics)
* No evidence questions: 5 (excluded from retrieval metrics)
* Preserved overall answer score: 32 out of 60 correct (53.33% accuracy)
* Preserved refusal accuracy: 38.46% (5 out of 13 correct)
* Infrastructure failures: 0

## Reconciliation of the 12 Metric Changes

Because substring row matching inflated tabular evidence candidate sets by 50 to 111 false positive chunks, retrieving 1 relevant chunk previously yielded a capped recall of 0.20 (1/5). With exact row resolution, ground truth sets reflect only genuine rows, causing accurate scoring across 12 queries.

Summary of aggregate metric deltas:
* Grounded Recall@5: 0.3481 -> 0.4858 (+0.1377, +13.77 percentage points)
* nDCG@5: 0.3447 -> 0.4459 (+0.1012, +10.12 percentage points)
* Precision@3: 0.2264 -> 0.2201 (-0.0063, -0.63 percentage points)
* MRR@5: 0.5013 -> 0.4918 (-0.0095)

Breakdown of the 12 affected queries:
1. Q144 (NUMERIC, NFHS workbook, South Andaman, row 4):
   * Expected relevant chunks: 1
   * Recall@5: 0.20 -> 1.00
   * nDCG@5: 0.3392 -> 1.0000
2. Q145 (NUMERIC, NFHS workbook, clean cooking fuel gap, rows 3 and 4):
   * Expected relevant chunks: 2
   * Recall@5: 0.40 -> 1.00
   * nDCG@5: 0.5531 -> 1.0000
3. Q146 (AGGREGATION, NFHS workbook, 3 districts, rows 2, 3, and 4):
   * Expected relevant chunks: 3
   * Recall@5: 0.60 -> 1.00
   * nDCG@5: 0.7227 -> 1.0000
4. Q148 (HALLUCINATION_PREMISE, NFHS workbook, North & Middle Andaman, row 3):
   * Expected relevant chunks: 1
   * Recall@5: 0.20 -> 1.00
   * nDCG@5: 0.3392 -> 1.0000
5. Q154 (NUMERIC, survay.csv, 2025 All industries, rows 2 and 3):
   * Expected relevant chunks: 2
   * Recall@5: 0.20 -> 0.50
   * nDCG@5: 0.3392 -> 0.6131
6. Q157 (HALLUCINATION_PREMISE, survay.csv, 2025 Beverage & Tobacco, row 6):
   * Expected relevant chunks: 1
   * Recall@5: 0.20 -> 1.00
   * nDCG@5: 0.1696 -> 0.5000
7. Q160 (TEMPORAL, rs_status_bill.csv, Anti-Hijacking Bill, row 1):
   * Expected relevant chunks: 1
   * Recall@5: 0.20 -> 1.00
   * nDCG@5: 0.3392 -> 1.0000
8. Q161 (NUMERIC, rs_status_bill.csv, Sikh Gurdwaras Bill, row 3):
   * Expected relevant chunks: 1
   * Recall@5: 0.20 -> 1.00
   * nDCG@5: 0.3392 -> 1.0000
9. Q162 (FORMAT_SPECIFIC, rs_status_bill.csv, Anti-Hijacking committee cell, row 1):
   * Expected relevant chunks: 1
   * Recall@5: 0.20 -> 1.00
   * nDCG@5: 0.2140 -> 0.6309
10. Q163 (AMBIGUITY, rs_status_bill.csv, Bill No. LIII assent date, row 2):
    * Expected relevant chunks: 1 (`row2`)
    * Recall@5: 0.40 -> 0.00
    * Precision@3: 0.3333 -> 0.0000
    * MRR@5: 0.5000 -> 0.0000
    * nDCG@5: 0.3452 -> 0.0000
11. Q165 (HALLUCINATION_PREMISE, rs_status_bill.csv, Anti-Hijacking introduction year, row 1):
    * Expected relevant chunks: 1
    * Recall@5: 0.20 -> 1.00
    * nDCG@5: 0.3392 -> 1.0000
12. Q194 (EDGE_ADVERSARIAL, NFHS workbook, men interviewed, row 3):
    * Expected relevant chunks: 1
    * Recall@5: 0.20 -> 1.00
    * nDCG@5: 0.3392 -> 1.0000

## Specific Findings for Q149 and Q163

### Q149 Exact Row and Retrieved Rank
* Question: "South Andaman mein kitne percent log bijli wale gharon mein rehte hain? NFHS file se batao."
* Document: `NFHS_5_India_Districts_Factsheet_Data.xls`
* Exact target row: Row 4
* Resolved chunk ID: `fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0:xls:Sheet1:row3` (metadata: `sheet: Sheet1, row: 4`; location reference: `Sheet: Sheet1, Row: 4`).
* Rank in saved `reranked_chunk_ids`: Rank 6 (index 5).
* Rationale for unchanged score: Prior to the fix, Q149 resolved to 111 chunks (matching row 4, row 40, row 400, rows 41 to 49, rows 401 to 499). Because `fcc723b5...:row3` was at rank 6, it fell outside the top 5 retrieval cutoff both before and after the fix, keeping Recall@5 at 0.00.

### Q163 False Match Removal
* Question: "What is the assent date for Bill No. LIII?"
* Document: `rs_status_bill_passed_assent-1952-2016.csv`
* Ground truth locator: row 2 (`676cc28b-7480-575b-9643-44cd8d85698f:csv:row2`).
* Defect mechanism: Under the old evaluator, `"Row: 2" in c.location_reference or "Row 2" in c.location_reference` falsely matched every row string starting with 2 (`Row 227`, `Row 206`, rows 20 through 29, rows 200 through 299).
* Retrieved candidates in `reranked_chunk_ids`:
  * Rank 2: `676cc28b-7480-575b-9643-44cd8d85698f:csv:row227` (`location_reference='Row 227'`)
  * Rank 5: `676cc28b-7480-575b-9643-44cd8d85698f:csv:row206` (`location_reference='Row 206'`)
* False matches removed: `row227` and `row206` were falsely accepted as relevant evidence because `"Row 2"` is a substring of `"Row 227"` and `"Row 206"`.
* Impact: Removing these false positive matches left only the true single row `row2`. Because `row2` was not retrieved among the top 5 candidates, Q163 correctly drops from 0.40 recall to 0.00 recall.
