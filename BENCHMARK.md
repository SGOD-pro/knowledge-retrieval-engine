# BENCHMARK.md — Targets and Evaluation

## Targets (all must pass simultaneously at Phase 5)

| Metric | Target | Method |
| :--- | :--- | :--- |
| Fast path p95 latency (warm) | <400ms | 200 runs, Lambda warm path |
| Fast path p95 latency (cold start) | <1500ms | 200 runs, first invocation after >15 min idle |
| Full pipeline p95 latency (warm) | <4000ms | 200 runs, Lambda warm path |
| Decomposed path p95 | <5500ms | 200 runs, decomposed query path |
| Graph traversal p95 (2-hop CTE) | <200ms | 200 runs, relationship queries only |
| Graph traversal p95 (3-hop CTE) | <350ms | 200 runs, relationship queries only |
| Cold start delta | <1200ms | p95_cold - p95_warm |
| Recall@3 (fast path) | >0.75 | 40-query factual test set |
| Recall@5 (full path) | >0.85 | 80-query complex test set |
| MRR@5 | >0.70 | Full 120-query test set |
| nDCG@5 | >0.72 | Full 120-query test set |
| Precision@3 | >0.70 | Full 120-query test set |
| Context Recall | >0.80 | % query entities in top-k context |
| Context Precision | >0.65 | % context used in final answer |
| Faithfulness | >0.80 | Citation-to-source manual check |
| Answer Relevancy | >0.75 | Human rater on 40-query sample |
| Hallucination rate | <5% | Citation mismatch check |
| Compression ratio | >30% | raw_tokens vs compressed_tokens |
| Entity coverage post-compress | 100% | fidelity_check test suite |
| LLM activation rate | <60% | % queries reaching LLM call |
| Cache hit rate (post-warmup) | >30% | Redis hit/miss counter |
| PageIndex candidate reduction | >60% | candidate_pages / total_pages |

---

## Updated Latency Targets

| Metric | Target (rev 5) | Prior (rev 3) | Reason for change |
| :--- | :--- | :--- | :--- |
| Fast path p95 (warm) | <400ms | <200ms | API calls replace local inference (now restored for BGE-small, but 400ms target kept) |
| Fast path p95 (cold start) | <1500ms (tracked separately) | n/a | New metric — Lambda-specific |
| Full pipeline p95 (warm) | <4000ms | <3000ms | Multiple sequential API calls |
| Decomposed path p95 | <5500ms | <4000ms | Same reason, plus parallel calls |
| Graph traversal p95 (2-hop CTE) | <200ms | <150ms | Postgres query vs in-memory dict |
| Graph traversal p95 (3-hop CTE) | <350ms | <250ms | Same |

> [!WARNING]
> **RE-VERIFICATION REQUIRED:** The Phase 1 exit criterion "100-page PDF ingested in <30 seconds" is unverified and flagged for re-verification given the new cross-Lambda synchronous call (`odl-parser-lambda`) is now in the critical path.

## New Metric — Cold Start Overhead
[Same as rev 4]

## Dev vs Prod Provider Parity Check
[Same as rev 4]

## Metric Definitions
[Same as rev 4]

## Test Data Requirements
[Same as rev 4]

## Competitor Baseline
[Same as rev 4]

## Regression Policy
[Same as rev 4]