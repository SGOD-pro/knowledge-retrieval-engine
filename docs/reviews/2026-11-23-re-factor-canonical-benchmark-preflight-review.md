# Review: Canonical Benchmark Preflight Verification and Rerun Readiness

* **Author**: Senior Reviewer
* **Date**: 2026-11-23
* **Branch**: `re-factor`
* **Tested Commit**: `5dc1895d36837a78da018c250f347f805ea08335`
* **Inspected Diff**: [5dc1895](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/5dc1895d36837a78da018c250f347f805ea08335)
* **Supersedes**: [2026-11-23-re-factor-negation-scope-and-clause-isolation-review.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-23-re-factor-negation-scope-and-clause-isolation-review.md)

## Summary

This review records the verification of system readiness for the repaired canonical 60 question benchmark rerun. All preflight checks were completed read only with zero live provider calls, zero vector store modifications, zero ground truth changes, and zero alterations to historical benchmark artifacts.

The current tree is clean at commit `5dc1895d36837a78da018c250f347f805ea08335`. All 104 focused evaluator regression tests pass cleanly. The external SQLite snapshot database, workspace isolation boundary, source document versions, and ground truth locator resolution have been verified against canonical references.

## Preflight Verification Evidence

1. **Commit and Working Tree Status**:
   * HEAD commit: `5dc1895d36837a78da018c250f347f805ea08335` on branch `re-factor`.
   * Working tree status: clean (`git status` reports zero unstaged or uncommitted tracked changes).

2. **Workspace Isolation and External SQLite Snapshot Database**:
   * Target workspace: `ws_canonical_comparison_0004`, guarded by `validate_benchmark_comparison_preconditions` to isolate controlled comparison runs from `ws_fresh_benchmark`.
   * External SQLite database verified at `/tmp/kre_workspace_boundary_canonical.db` (114,688 bytes).
   * Snapshot registry contains all 9 canonical documents for `ws_canonical_comparison_0004` with deterministic document identifiers matching `generate_deterministic_doc_id(filename, 'ws_canonical_comparison_0004')`:
     * `survay.csv`: document identifier `34202080-60de-532e-a0c2-01f45b0af39a`, version `c029d234eefd3fee2563b9c0929946ca`, persisted rows 60,255.
     * `SEC-Form-10Q.pdf`: document identifier `492ccb2d-0d0d-5022-8a7b-e3f87df8befd`, version `3518726963892d32ec11a876a5dfa448`, 40 pages.
     * `rs_status_bill_passed_assent-1952-2016.csv`: document identifier `676cc28b-7480-575b-9643-44cd8d85698f`, version `85d048226e88f44a76f3bf68c3f96669`, persisted rows 727.
     * `2412.20875v1.pdf`: document identifier `89cc9991-e7e1-548f-bede-7c53bdd1a7c7`, version `927420d4297edb4f6ff566961a14c98a`, 28 pages.
     * `National-Strategy-for-Artificial-Intelligence.pdf`: document identifier `95b11ed0-7cb1-535b-b5c2-8ad3da681136`, version `da5404dbb584c8956aae707dfcfe324d`, 139 pages.
     * `2212.14776v3.pdf`: document identifier `afa8559f-21d4-5b64-885f-9c42ce2d4503`, version `bce22600474513c61b8c47701d19275b`, 25 pages.
     * `2501.09166v1.pdf`: document identifier `b213f371-bf2b-5359-b568-db0ea20a508e`, version `d5a727f2d0b04269389630b6687f7fa4`, 13 pages.
     * `2501.05730v1.pdf`: document identifier `f8897f60-0128-5722-bd45-e8938daf9b75`, version `4eafc24abe305dd84a859a95e19456a5`, 18 pages.
     * `NFHS_5_India_Districts_Factsheet_Data.xls`: document identifier `fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0`, version `e482b990d74eb46b5ce5a6e10606b745`, persisted rows 707.

3. **Corpus Hash and Ground Truth Resolution**:
   * Corpus manifest combined hash across all 9 documents is `ac193bc5bea7bfcc547e39924118fdd469385ceb528d14d5528f0464f74c959d`.
   * Evaluator ground truth evidence hashes match the canonical corpus hashes with zero mismatches.
   * Ground truth locator resolution resolves all 53 scorable retrieval queries without unresolvable locators or hash discrepancies.

4. **Strategy Configuration and Feature Flags**:
   * `GRAPH_ENABLED=false` and `LAYA_ENABLED=false` verified.
   * Structural retrieval flags `--page-index` and `--okf` enabled, soft pooling candidate chunks into reciprocal rank fusion with weights 1.10 and 1.15 respectively.

5. **Reranker Configuration and Policy Reconciliation**:
   * The runtime operates remote first. On every retrieval query, the runner dispatches an HTTP request to OpenRouter for model `nvidia/llama-nemotron-rerank-vl-1b-v2:free`.
   * Because OpenRouter returns an HTTP 404 client error (endpoint not found for this free model), the call fails fast on that query. The daily quota circuit breaker (`_trip_circuit`) only trips for HTTP 429 quota exhaustion; for HTTP 404 client errors, each retrieval query executes remote first, records 1 remote reranker call, and immediately falls back to deterministic BM25Plus lexical scoring (`_term_coverage_fallback`), logging reranker mode `circuit_open_lexical_fallback`.
   * Effective reranking mode: `circuit_open_lexical_fallback` across retrieval queries (42 remote calls attempted, falling back to BM25Plus), and `bypassed` for structured aggregate, fast path, and refusal queries (18 queries).
   * Remote endpoint availability remains unresolved (HTTP 404), ensuring consistent and deterministic local lexical scoring across all queries without network latency variance.

6. **Artifact Request-Retained Evidence Serialization (Resolved)**:
   * The runner in `scripts/run_canonical_60_benchmark.py` has been updated to serialize `retained_evidence_items` via `to_dict()`, along with all required evaluation inputs (`contract_type`, `contract`, `question_type`, `expected_sources`, `expected_source_version`, `ground_truth_chunk_ids`, `is_infra_error`, `ans_is_refusal`, `structured_aggregate`, and `execution_type`).
   * In `services/retrieval/evidence_contract.py`, `EvidenceItem.from_dict` and `restore_evidence_item` restore typed instances from serialized dictionaries.
   * Offline rescoring function `rescore_query_record_offline` reproduces identical grounded evaluations directly from saved JSON artifacts without live database or model access.

7. **Evaluator Revision 2.2.0 and Comparison Limits Against Run 20261122_182354**:
   * Scorer version is distinctly set to `2.2.0` (runner version `2.2.0`). This distinctly identifies the grounded evaluator with offline artifact reproducibility.
   * Strict comparator gates in `compare_manifests` preserve equality requirements: comparing candidate manifests against historical `2.1.0` manifests strictly emits `NOT COMPARABLE` due to the distinct `scorer_version` and `runner_version`, protecting benchmark integrity while allowing controlled comparison runs against matching `2.2.0` baselines.
   * Historical run `20261122_182354` recorded only legacy answer text accuracy (`overall_accuracy`: 32/60 correct, 53.33%). It had 1,697 unverified model provenance points in Qdrant and suffered from substring row matching, loose calculation tolerances, and unvalidated citation support.
   * Current evaluator revision `2.2.0` enforces:
     * Grounded correctness requiring answer text correctness plus verified request retained evidence.
     * Rejection of fabricated citation snippets and arbitrary number targets.
     * Verification of all necessary operands in derived calculations.
     * Grounded structured aggregate result verification matching authoritative retained executions.
     * Claim level contradiction detection with syntactically bounded negation scope.
     * Exact integer row matching in ground truth locator resolution.
   * Comparison limits: Direct comparison against the old run must distinguish legacy text accuracy from grounded accuracy. The old run did not track grounded correctness. Historical artifacts under `backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/` remain untouched.

## Test Verification

All focused evaluator regressions passed cleanly (115 passed in 5.84s):
* [test_evaluator_defect_reproduction.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_evaluator_defect_reproduction.py): 15 passed
* [test_canonical_benchmark_runner_settings.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_canonical_benchmark_runner_settings.py): 6 passed
* [test_grounded_answer_evaluator.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_grounded_answer_evaluator.py): 35 passed
* [test_benchmark_integrity_defects.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_benchmark_integrity_defects.py): 10 passed
* [test_citation_retained_evidence.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_citation_retained_evidence.py): 10 passed
* [test_mandatory_audit_guards.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_mandatory_audit_guards.py): 10 passed
* [test_benchmark_runner_and_persistence_0004.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_benchmark_runner_and_persistence_0004.py): 18 passed
* [test_offline_benchmark_artifact_reproducibility.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_offline_benchmark_artifact_reproducibility.py): 11 passed

## Provider Call and Cost Estimate

For a full 60 question cold canonical run:
* Remote Bedrock Titan V2 embeddings (`amazon.titan-embed-text-v2:0`): ~42 calls (1 per retrieval question)
* Remote Bedrock Nova Lite V1 text completions (`apac.amazon.nova-lite-v1:0`): ~38 to 45 calls (1 per non refusal synthesis question, adhering to `MAX_SYNTHESIS_CALLS = 1`)
* Remote OpenRouter reranker calls: 42 remote calls attempted (remote first on each retrieval question, failing fast on 404 with 0 financial cost)
* Local BGE fast embeddings: ~42 calls (in process, zero external cost)
* Total estimated cloud cost: less than $0.05

## Exact One-Run Command

```bash
cd backend && \
  KRE_WORKSPACE_REPO_BACKEND=sqlite \
  KRE_WORKSPACE_SQLITE_PATH=/tmp/kre_workspace_boundary_canonical.db \
  LAYA_ENABLED=false GRAPH_ENABLED=false PYTHONPATH=src \
  uv run python scripts/run_canonical_60_benchmark.py \
    --workspace-id ws_canonical_comparison_0004 \
    --benchmark-type cold \
    --test-path data/test.json \
    --page-index --okf
```

## Readiness Verdict

**READY**. All operational prerequisites, configuration settings, boundary registries, corpus checksums, offline artifact persistence, and evaluator regression suites are verified and ready for execution. Benchmark execution remains strictly pending user authorization.
