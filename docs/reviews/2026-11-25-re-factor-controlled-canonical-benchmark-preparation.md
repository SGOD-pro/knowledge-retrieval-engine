# Review: Controlled Canonical Benchmark Preparation and Q193 Verification

* **Author**: Senior Acceptance Reviewer
* **Date**: 2026-11-25
* **Branch**: `re-factor`
* **Tested Commit**: `5884d52a3c2a2ab8b713f679c40128ec0c6bcbe1`
* **Inspected Diff**: [5884d52](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/5884d52a3c2a2ab8b713f679c40128ec0c6bcbe1)
* **Supersedes**: [2026-11-23-re-factor-canonical-run-20261123_075022-offline-audit.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-23-re-factor-canonical-run-20261123_075022-offline-audit.md)

## Summary

This review records the offline verification of question Q193, an exhaustive failure triage of the 48 grounded misses in canonical run `20261123_075022`, and complete preparation for the next controlled 60 question benchmark rerun (Q136 to Q195).

All verifications were executed offline using saved artifacts, in memory fixtures, and local test doubles. Zero live provider calls, zero paid benchmark runs, and zero corpus modifications were performed. Historical benchmark records and ground truth data remain completely intact.

## Working Tree and HEAD Status

* **HEAD Commit**: `5884d52a3c2a2ab8b713f679c40128ec0c6bcbe1` on branch `re-factor`
* **Working Tree**: Clean tracked state (only untracked `.playwright-mcp/` directory present)

## 1. Q193 Offline Verification and Defect Localization

### Untouched Record Reproduction
Replaying the untouched saved query record from [query_details.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/73075af21d9172155cad22866d8eef2e7c186edb/20261123_075022/query_details.json) reproduced the exact verdict:
* **Question**: Adversarial request asking for Apple Services sales for the three months ended June 27, 2026.
* **Expected Answer**: `30,739` million dollars.
* **Actual Answer**: `23,245` million dollars.
* **Grounded Verdict**: `is_grounded_correct: False`.
* **Reason**: `answer_text_incorrect`.

### In Memory Fixture Evaluation
1. **Fixture A ($30,739 million with Net Sales citation, page 4/18)**:
   * **Answer**: `Services net sales were $30,739 million for the three months ended June 27, 2026.`
   * **Citations**: Page 4/18 Net Sales table in `SEC-Form-10Q.pdf`.
   * **Grounded Verdict**: `is_grounded_correct: True`.
   * **Reason**: `grounded_and_verified`.
2. **Fixture B ($23,245 million with Gross Margin citation, page 19)**:
   * **Answer**: `Services net sales were $23,245 million for the three months ended June 27, 2026.`
   * **Citations**: Page 19 Gross Margin table in `SEC-Form-10Q.pdf`.
   * **Grounded Verdict**: `is_grounded_correct: False`.
   * **Reason**: `answer_text_incorrect`.

### Delivery Gate Verification
In [table_metric_binding.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/verification/table_metric_binding.py), `verify_financial_cell_binding` rejects Gross Margin table cells when the query requests Net Sales (`cell_metric == "gross_margin"` vs `requested_metric == "net_sales"`), enforcing a fail closed refusal.

### Defect Localization
* **Scorer**: Not defective. The evaluator correctly accepts $30,739 million with Net Sales citations and rejects $23,245 million.
* **Answer Delivery**: Not defective. Financial cell metric binding strictly refuses conflicting metric cells.
* **Remaining Defect**: Located in **retrieval and generation context competition**. The compressed context supplied both Net Sales and Gross Margin tables to the LLM, and the generation prompt selected the Gross Margin cell.

## 2. Triage of the 48 Grounded Misses

Evaluation of the 48 grounded misses in canonical run `20261123_075022` yields four distinct failure mechanisms:

1. **Citation and Provenance Failures (19 queries, 39.6% of misses)**:
   * **Synthetic fallback filename / extension mismatch (8 queries)**: Q144, Q145, Q146, Q148, Q152, Q153, Q154, Q194 emitted correct numeric answers and chunk locators, but failed grounded grading because citations contained `Doc-fcc723b5`, `Doc-34202080`, or missing extensions like `survay`.
   * **Extraneous / cross document citations (5 queries)**: Q165, Q169, Q174, Q183, Q191 included unnecessary chunks from unrelated documents in multi chunk citations.
   * **Citation snippet fabrication / mismatch (3 queries)**: Q136, Q141, Q175 cited chunks where snippet text did not match the retained evidence payload.
   * **Low citation faithfulness / unsupported numbers in snippet (3 queries)**: Q139, Q161, Q181 failed on snippet unfaithfulness.

2. **Answer and Refusal Errors (16 queries, 33.3% of misses)**:
   * **Incorrect answer text / arithmetic error (15 queries)**: Q142, Q143, Q147, Q155, Q157, Q163, Q164, Q168, Q171, Q172, Q173, Q176, Q180, Q188, Q193.
   * **Refusal contract failure (1 query)**: Q159 failed to refuse an adversarial prompt.

3. **Retrieval Misses (9 queries, 18.8% of misses)**:
   * **Unwarranted refusals due to low recall or missing chunks (6 queries)**: Q150, Q156, Q158, Q178, Q190, Q192.
   * **Cited chunk ID mismatched ground truth locators (3 queries)**: Q138, Q167, Q184.

4. **Confirmed Scorer Defects (4 queries, 8.3% of misses)**:
   * **Q149**: Tokenizer `len(w) > 2` discarded two character numbers (`99` and `58` from `99.58%`), causing zero recall.
   * **Q140**: The year number `27` was parsed as an unsupported numeric claim.
   * **Q162**: Scorer negation check flagged "No" as negating entity "Anti-Hijacking".
   * **Q189**: Scorer negation check flagged "No" as negating entity "EA-series".

### Highest Impact Repair to Verify Next
The **citation provenance resolution repair** in `services/retrieval/response_builder.py` is the highest impact repair. Resolving filenames and versions directly from workspace boundary snapshots eliminates false citation rejections for at least 8 queries (Q144, Q145, Q146, Q148, Q152, Q153, Q154, Q194) that answered with complete factual correctness.

## 3. Cold Canonical Rerun Preparation (Q136–Q195)

* **Target Workspace**: `ws_canonical_comparison_0004`
* **Workspace Boundary Database**: `/tmp/kre_workspace_boundary_canonical.db` (SQLite, 9 canonical document snapshots)
* **Corpus Combined Hash**: `ac193bc5bea7bfcc547e39924118fdd469385ceb528d14d5528f0464f74c959d`
* **Cold Cache Policy**: `--benchmark-type cold`, bypassing semantic and exact caches, resetting BM25 chunk caches
* **Runner and Scorer Version**: `2.2.0`
* **Reranker Policy and Strict Comparability**:
  * Configured policy: `openrouter:nvidia/llama-nemotron-rerank-vl-1b-v2:free (fallback: lexical_bm25_plus)`.
  * Remote endpoint returns HTTP 404 client error because the free model is deprecated.
  * The runner cleanly degrades to deterministic local lexical fallback (`circuit_open_lexical_fallback` via BM25Plus).
  * Impact on comparability: Changing the policy string alters the manifest `reranker_policy` field, causing `compare_manifests` to emit `NOT_COMPARABLE`. Keeping the existing fallback policy preserves strict comparator gate compatibility while maintaining deterministic scoring.
* **Comparability Verdict**: Strictly comparable against run `20261123_075022` under `2.2.0` schema. Strictly non comparable against legacy run `20261122_182354` (`2.1.0`).

### Exact Controlled Rerun Command

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

### Estimated Provider Calls and Cost

* **AWS Bedrock Titan Embeddings** (`amazon.titan-embed-text-v2:0`): ~42 to 60 calls (~$0.01)
* **AWS Bedrock Nova Lite Completions** (`apac.amazon.nova-lite-v1:0`): ~40 to 51 calls with `MAX_SYNTHESIS_CALLS = 1` (~$0.02 to $0.05)
* **OpenRouter Reranker**: 42 remote attempts failing fast on 404 ($0.00)
* **Total Estimated Cost**: less than $0.10 USD

Execution is stopped before live provider calls or paid benchmark execution.
