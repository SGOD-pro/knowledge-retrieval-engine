# Review: Financial Table Binding Review Blockers Resolved

* Author: Senior Reviewer
* Date: 2026-11-23
* Branch: `re-factor`
* Base Commit: `6344fdced3c44b7aa47bd21a96e4832173eab5f8`

## Summary

This review records the verification and acceptance of repairs resolving the four financial table binding review blockers across the query lifecycle.

In the initial implementation, evidence validation exhibited four gaps:
1. Candidate numbers mentioned as comparisons or secondary mentions could validate an answer when the primary claim was unsupported. Sign preservation and exact arithmetic for scale conversions (such as presenting millions as billions without valid arithmetic conversion) were not fully enforced.
2. When a query requested specific dates or durations, tables lacking period metadata could bypass checks. Date comparison used substring matching, which permitted June 27 evidence to pass a June 7 query.
3. Financial answers did not strictly require non empty citations, and citation supplied text could establish evidence independently for unknown citation identifiers.
4. When more than six candidates competed during reranking, metric aligned tables with lower initial cross encoder scores could be truncated by top k selection before affinity reordering took place.

This bounded repair addresses each defect:
1. Complete claim verification: Every required financial claim is parsed and verified using exact decimal arithmetic. Accounting parentheses and negative signs are preserved. Scale multipliers distinguish millions, billions, thousands, and dollars. Valid unit conversions (for example, 30,739 million converted to 30.739 billion) are accepted, whereas invalid conversions (for example, 30,739 billion) are rejected. Monetary amounts and percentages remain strictly separated. Secondary comparison values cannot validate an unsupported primary claim.
2. Complete period evidence: When a query requests a date or duration, cited tables lacking period metadata strictly fail verification. Date comparison extracts exact integer components (year, month, and day), proving that June 27 strictly fails a June 7 request. Supported duration expressions normalize to canonical categories.
3. Citation resolution to authoritative evidence: Non empty citations are required for delivered financial answers. Citation identifiers must resolve to authoritative pipeline chunks and retained evidence items. Plausible citation supplied text cannot establish evidence independently.
4. Metric affinity in top k evidence selection: Reranking incorporates metric affinity directly into candidate scoring and threshold filtering. When more than six candidates compete, the metric aligned table survives top k selection, while relevance ordering among equal affinity candidates is preserved.

## Architectural Changes

1. **Exact Decimal Arithmetic, Metric Precedence, and Multi Metric Binding in `table_metric_binding.py`**:
   * Added `classify_metric` to enforce strict precedence for specific phrases like `cost of sales` and `cost of revenue` before generic `sales` and `revenue`.
   * Added `parse_query_requirements` and extended `FinancialQueryBinding` to bind all explicitly requested (metric, entity) pairs while preserving single metric callers via `binding.metric`.
   * Added `parse_date_components` to extract exact integer tuples for year, month, and day.
   * Added `normalize_duration` to standardize duration expressions across query grammar and table headers.
   * Added `detect_table_scale` and `SCALE_MULTIPLIERS` to compute absolute values using `Decimal`.
   * Updated `parse_financial_table_cells` to capture negative accounting values enclosed in parentheses and negative signs.
   * Added `extract_financial_claims` to parse asserted claims, separating primary assertions from comparison clauses and classifying claim metrics consistently.
   * Updated `verify_financial_cell_binding` to require complete period metadata, exact date matches, sign consistency, exact unit scale verification, multi chunk claim support across independently parsed cited tables, and complete coverage of all explicitly requested metrics.

2. **Metric Affinity Integration in `services/retrieval/reranker.py`**:
   * Extended `rerank` and `score_table_chunk_metric_affinity` to evaluate metric affinity across all requested query metrics without mutual penalty.
   * Sorted candidates by affinity descending with base relevance score tie breaking, ensuring aligned tables survive top k selection while preserving relevance ordering where affinity is equal.
   * Calibrated threshold filtering to protect positive affinity tables from low probability score cutoffs.

3. **Citation Resolution in `services/langgraph_pipeline.py`**:
   * In `run_llm`, prevented fallback context citation borrowing on active financial queries.
   * In `delivery_gate`, required non empty citations for financial answers and resolved citation identifiers strictly against authoritative pipeline chunks. Unresolved identifiers or citation supplied text cannot establish evidence independently.
   * In `delivery_gate`, verified financial claims across the complete set of resolved cited chunks, ensuring answers requiring multiple cited tables are verified without cross chunk header leakage.

## Verification Evidence

* Focused suite: `cd backend && uv run pytest tests/test_financial_table_metric_binding.py -v` (36 passed in 4.11s).
* Regression suite: `cd backend && uv run pytest tests/test_financial_table_metric_binding.py tests/test_canonical_percentage_grading.py tests/test_offline_benchmark_artifact_reproducibility.py tests/test_grounded_answer_evaluator.py tests/test_benchmark_guard.py tests/test_benchmark_integrity_defects.py tests/test_citation_retained_evidence.py tests/test_evidence_providers.py tests/test_fast_path_fallback_regressions.py tests/test_chunk_citation_provenance_repair.py -rs` (162 passed in 10.19s).
