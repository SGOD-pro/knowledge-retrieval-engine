# Review: Canonical Cold Benchmark 2.2.0 Rerun and Grounded Scoring Verification

* **Author**: Senior Acceptance Reviewer
* **Date**: 2026-11-23
* **Branch**: `re-factor`
* **Tested Commit**: `73075af21d9172155cad22866d8eef2e7c186edb`
* **Inspected Diff**: [73075af](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/73075af21d9172155cad22866d8eef2e7c186edb)
* **Supersedes**: [2026-11-23-re-factor-offline-benchmark-artifact-reproducibility-review.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-23-re-factor-offline-benchmark-artifact-reproducibility-review.md)

## Summary

This review confirms the execution, provider operations, offline reproducibility, and audited outcomes of the authorized single cold canonical benchmark rerun.

The benchmark executed cleanly on workspace `ws_canonical_comparison_0004` using the isolated external SQLite database at `/tmp/kre_workspace_boundary_canonical.db`, with cold cache configuration, PageIndex enabled, OKF enabled, and both LAYA and GRAPH disabled.

All 60 query records, request retained evidence items, and evaluation inputs were saved under `backend/reports/73075af21d9172155cad22866d8eef2e7c186edb/20261123_075022/`. Replaying all 60 saved query records offline reproduced the exact identical grounded pass and fail verdicts (100% agreement, 12 grounded passes and 48 grounded failures). The legacy answer text accuracy reached 61.67% (37 / 60 correct), while grounded accuracy scored strictly at 20.00% (12 / 60 grounded) due to rigorous enforcement of evidence authority, citation validity, and claim level contradiction rejection. Strict comparator gates against historical run `20261122_182354` correctly enforce non comparability due to runner and scorer version isolation (`2.2.0` compared to `2.1.0`).

## Preflight and Execution Verification

* **Working Tree and Commit**: Clean checkout confirmed at commit `73075af21d9172155cad22866d8eef2e7c186edb`.
* **Workspace Boundary Database**: External restored SQLite database confirmed at `/tmp/kre_workspace_boundary_canonical.db` (114,688 bytes, 9 documents, 9 document snapshots).
* **Execution Parameters**:
  * Workspace: `ws_canonical_comparison_0004`
  * Cache profile: cold (`--benchmark-type cold`, bypassing semantic and exact query caches)
  * RAG strategies: PageIndex enabled (weight 1.1), OKF fusion enabled (weight 1.15)
  * Feature flags: `LAYA_ENABLED=false GRAPH_ENABLED=false`
  * Backend repo: `KRE_WORKSPACE_REPO_BACKEND=sqlite KRE_WORKSPACE_SQLITE_PATH=/tmp/kre_workspace_boundary_canonical.db`
* **Single Run Command Executed**:
  `KRE_WORKSPACE_REPO_BACKEND=sqlite KRE_WORKSPACE_SQLITE_PATH=/tmp/kre_workspace_boundary_canonical.db LAYA_ENABLED=false GRAPH_ENABLED=false python3 backend/scripts/run_canonical_60_benchmark.py --workspace-id ws_canonical_comparison_0004 --benchmark-type cold --baseline-commit 73075af21d9172155cad22866d8eef2e7c186edb --page-index --okf`

## Provider Identities, Remote Operations, and Measured Costs

* **LLM Provider and Model**: AWS Bedrock `apac.amazon.nova-lite-v1:0` in region `ap-southeast-1`.
  * Total generation calls: 51 generation calls across 60 questions (85.00% activation rate). 9 questions bypassed text generation (structured table queries and deterministic refusals).
  * Observed token usage: Bedrock Nova Lite input tokens (mean ~2,400 per query) and output tokens logged per query.
* **Embedding Provider and Model**: AWS Bedrock `amazon.titan-embed-text-v2:0` in region `ap-southeast-1`.
  * Observed embedding usage: 1,390 total embedding tokens logged across retrieval questions.
* **Reranker Provider and Remote First Behavior**:
  * Configured remote model: OpenRouter `nvidia/llama-nemotron-rerank-vl-1b-v2:free`.
  * Remote attempts: 42 remote POST requests sent to OpenRouter rerank endpoint.
  * Remote failures: 42 failures (100% returned HTTP 404 client error because the model identifier is deprecated on OpenRouter).
  * Fallback mode: Fail safe deterministic lexical fallback (`circuit_open_lexical_fallback` via BM25Plus) activated for all 42 retrieval questions. 18 questions bypassed reranking.
* **Financial Cost Accounting (Measured vs Estimated)**:
  * OpenRouter charges: $0.00 measured (HTTP 404 client errors incur zero provider cost).
  * AWS Bedrock charges: approximately $0.005 measured total across Nova Lite synthesis and Titan V2 embedding tokens.
  * Zero additional provider costs incurred.
* **Latency Profile**:
  * Query latency: minimum 1,019.27 ms, maximum 96,867.96 ms, mean 7,734.33 ms.

## Unresolved Vector Provenance and Corpus Integrity

* **Qdrant Vector Provenance**: 1,697 points in the collection continue to have unresolved model provenance out of 6,697 total points.
* **Corpus and Database Safety**: Zero writes to Qdrant occurred during benchmark execution. No re ingestion, no re embedding, and no corpus modifications took place.

## Benchmark Artifacts and 100% Offline Replay

* **Saved 2.2.0 Artifacts**: Located under `backend/reports/73075af21d9172155cad22866d8eef2e7c186edb/20261123_075022/`:
  * [manifest.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/73075af21d9172155cad22866d8eef2e7c186edb/20261123_075022/manifest.json)
  * [query_details.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/73075af21d9172155cad22866d8eef2e7c186edb/20261123_075022/query_details.json)
  * [summary.md](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/73075af21d9172155cad22866d8eef2e7c186edb/20261123_075022/summary.md)
* **Offline Replay Verification**:
  * Executed `rescore_query_record_offline(record)` across all 60 saved records in `query_details.json`.
  * Exactly 60 out of 60 records (100.0%) reproduced the exact identical grounded correctness verdict (`is_grounded_correct`).
  * 12 records passed grounded evaluation, and 48 records failed grounded evaluation.
  * Replay executed completely offline with zero network calls, zero provider invocations, and zero database queries.

## Accuracy Metrics Summary

* **Answer Text Accuracy (Legacy Metric)**: 61.67% (37 / 60 passed, 23 failed).
* **Grounded Accuracy (Audited Contract)**: 20.00% (12 / 60 passed, 48 failed).
* **Refusal Accuracy**: 46.15% (6 / 13 passed).
* **Retrieval Recall@5**: 50.47%.
* **Retrieval Precision@3**: 23.27%.
* **Retrieval MRR@5**: 0.4950.
* **Retrieval nDCG@5**: 0.4523.
* **LLM Activation Rate**: 85.00% (51 / 60).
* **Grounded Passed Question IDs (12)**: Q137, Q151, Q160, Q166, Q170, Q177, Q179, Q182, Q185, Q186, Q187, Q195.

## Strict Comparison Gate Result

Comparing candidate manifest `20261123_075022` against canonical baseline `20261122_182354` via `compare_manifests`:
* **Comparison Outcome**: `NOT COMPARABLE — no accuracy delta may be claimed`
* **Violations Enforced**:
  * `baseline_commit_sha mismatch`: candidate reference `73075af21d9172155cad22866d8eef2e7c186edb` != baseline commit `a50c0a4f127c7418ffd79447430a9093d8b37f66`
  * `runner_version mismatch`: candidate `2.2.0` != baseline `2.1.0`
  * `scorer_version mismatch`: candidate `2.2.0` != baseline `2.1.0`
* **Integrity Gate Assessment**: The comparator correctly isolates the new evaluator release, preventing ungrounded historical claims while establishing a clean reproducible baseline for future 2.2.0 evaluations.

## Detailed Breakdown of Text Accuracy Failures (23 Questions)

The following 23 questions failed answer text grading:

* **Q142**:
  * Expected: `No. Basic EPS is $2.03 per share for the three months ended June 27, 2026. Per-share amounts are excluded from the millions convention.`
  * Actual: `No, the correct unit is not $2.03 million. The correct unit for basic earnings per share (EPS) is dollars per share. The context shows that the basic EPS for the three months ended June 27, 2026, is $2.03 per share, not $2.03 million.`
* **Q143**:
  * Expected: `Gross margin was 54,770 million dollars, or approximately 50.06% of total net sales (54,770 / 109,417 × 100).`
  * Actual: `Apple's gross margin in the June 2026 quarter was $54,770 million in dollar amount and 50.1% in percentage.`
* **Q147**:
  * Expected: `North & Middle Andaman, 100.00%.`
  * Actual: `North District has the highest percentage of children under five with registered births at 98.14%.`
* **Q149**:
  * Expected: `99.58% of the population lives in households with electricity.`
  * Actual: `99.58%`
* **Q150**:
  * Expected: `No. Sheet2 and Sheet3 are empty; district data are on Sheet1.`
  * Actual: `I couldn't find any relevant passages in the workspace documents matching your query.`
* **Q155**:
  * Expected: `Not exactly. Income minus expenditure is 108,327 million dollars, while H23 reports 109,848 million dollars. The difference is 1,521 million dollars, matching closing stocks minus opening stocks.`
  * Actual: `The sum for Value is 16790554.`
* **Q156**:
  * Expected: `The unit is Percentage and the value is 79.`
  * Actual: `I couldn't find any relevant passages in the workspace documents matching your query.`
* **Q157**:
  * Expected: `No. The Value field contains C. It is not a numeric zero; the numeric amount cannot be recovered from that cell.`
  * Actual: `No, it is not correct. The value for H05 in 2025 Beverage and Tobacco Product Manufacturing (CC15) is 'C', which is not zero and not numeric.`
* **Q158**:
  * Expected: `Please specify the year and industry code or industry name; closing stocks appears for multiple year–industry combinations.`
  * Actual: `I couldn't find any relevant passages in the workspace documents matching your query.`
* **Q159**:
  * Expected: `None` (Refusal expected)
  * Actual: `No rows matched the requested filters in table 'survay' (0 matching rows). Sum is 0.`
* **Q163**:
  * Expected: `Bill No. LIII occurs in multiple years. Please specify the bill title or year before selecting an assent date.`
  * Actual: `The assent date for Bill No. LIII is 13/05/2016.`
* **Q164**:
  * Expected: `7 records.`
  * Actual: `0`
* **Q168**:
  * Expected: `Focus-Classify Attention Model.`
  * Actual: `Focused Convolutional Attention Mechanism`
* **Q171**:
  * Expected: `No. It explicitly demonstrates error modes where an attention model predicts accurately but is not interpretable or focuses incorrectly.`
  * Actual: `No, the interpretability paper does not establish that a model with accurate predictions must focus on the most important input tokens. In fact, it provides examples where attention maps fail to reflect the true importance of features.`
* **Q172**:
  * Expected: `Yes. For ViT-Base at 12.5% capacity, MoD reaches 78.49% and A-MoD 78.42%; MoD is higher by 0.07 percentage points.`
  * Actual: `No, A-MoD beats standard MoD in every Table 1 configuration.`
* **Q173**:
  * Expected: `Accuracy rises by 2.04 percentage points, from 69.76% to 71.8%. FLOPs rise from 0.71 G to 0.92 G, an increase of 0.21 G.`
  * Actual: `The accuracy of DeiT-Tiny A-MoD improves from 0.97% to 52.6% when capacity rises from 12.5% to 50%. The FLOPs increase from 0.71G to 0.92G.`
* **Q176**:
  * Expected: `They remove a token and measure the change in model loss using a leave-one-out procedure, then correlate those importance estimates with routing scores.`
  * Actual: `A-MoD researchers compute the importance scores directly from the attention maps of previous layers to evaluate token importance for routing decisions.`
* **Q178**:
  * Expected: `A few important tokens receive large attention weights while the others receive little weight, producing a sharply concentrated distribution.`
  * Actual: `I couldn't find any relevant passages in the workspace documents matching your query.`
* **Q180**:
  * Expected: `No. Its stated inference complexity is O(tD): independent of sequence length L, but dependent on policy dimension D and time step t.`
  * Actual: `No, it is not correct. The computation of EA-series is minimally affected by changes in batch size, not sequence length.`
* **Q188**:
  * Expected: `The interpretability paper shows accurate predictions can coexist with poor interpretability. The A-MoD paper demonstrates that higher routing capacity improves accuracy but increases FLOPs, requiring an explicit trade-off.`
  * Actual: `The two papers that support caution about treating an attention heatmap as proof of why a model predicted an outcome are: 1. Focus-Classify Attention Model paper, 2. Retention Layer paper.`
* **Q190**:
  * Expected: `Apple: 149,818 / 149,326 × 100 ≈ 100.33%. Survey All industries: 79%. Apple's ratio is approximately 21.33 percentage points higher.`
  * Actual: `I couldn't find any relevant passages in the workspace documents matching your query.`
* **Q192**:
  * Expected: `No. The NFHS workbook reports the 99.58% district figure, while the strategy describes AI opportunities in healthcare, agriculture, and education; it does not set electricity targets.`
  * Actual: `I couldn't find any relevant passages in the workspace documents matching your query.`
* **Q193**:
  * Expected: `Services net sales were 30,739 million dollars.`
  * Actual: `23,245`

## Detailed Breakdown of Grounded Accuracy Failures (48 Questions)

The following 48 questions failed grounded accuracy evaluation. This comprises the 23 text failures above, plus 25 questions whose answer text passed legacy grading but failed grounded scoring due to citation fabrications, missing authoritative evidence, unsupported claims, or contradictions:

* **Q136**:
  * Reason: `invalid_citations: Citation 0: snippet fabricates supporting content not in retained evidence (unsupported numbers {'240'})`
  * Actual: `001-36743`
* **Q138**:
  * Reason: `invalid_citations: No citations match evaluator ground-truth evidence chunk IDs`
  * Actual: `28.09%, calculated as 30739 / 109417 × 100`
* **Q139**:
  * Reason: `claim_unsupported: low citation faithfulness (0.1667)`
  * Actual: `Three months ended June 27, 2026: 109,417 million dollars. Nine months ended June 27, 2026: 334,923 million dollars.`
* **Q140**:
  * Reason: `claim_unsupported: numeric claims {'27', '5374'} not supported by cited snippets`
  * Actual: `5,374 million dollars (27,088 minus 21,714)`
* **Q141**:
  * Reason: `invalid_citations: Citation 0: snippet fabricates supporting content not in retained evidence (unsupported numbers {'471'})`
  * Actual: `134,845 million dollars (total term debt 98,660 + commercial paper 4,775 + other 31,410)`
* **Q142**:
  * Reason: `answer_text_incorrect`
  * Actual: `No, the correct unit is not $2.03 million...`
* **Q143**:
  * Reason: `answer_text_incorrect`
  * Actual: `Apple's gross margin in the June 2026 quarter was $54,770 million in dollar amount and 50.1% in percentage.`
* **Q144**:
  * Reason: `invalid_citations: Citation 0: source document 'Doc-fcc723b5' not in expected sources {'NFHS_5_India_Districts_Factsheet_Data.xls'}`
  * Actual: `99.58%`
* **Q145**:
  * Reason: `invalid_citations: Citation 0: source document 'Doc-fcc723b5' not in expected sources {'NFHS_5_India_Districts_Factsheet_Data.xls'}`
  * Actual: `99.58%`
* **Q146**:
  * Reason: `invalid_citations: Citation 0: source document 'Doc-fcc723b5' not in expected sources {'NFHS_5_India_Districts_Factsheet_Data.xls'}`
  * Actual: `68.2% of women age 15-49 years have 10 or more years of schooling in South Andaman district.`
* **Q147**:
  * Reason: `answer_text_incorrect`
  * Actual: `North District has the highest percentage of children under five with registered births at 98.14%.`
* **Q148**:
  * Reason: `invalid_citations: Citation 0: source document 'Doc-fcc723b5' not in expected sources {'NFHS_5_India_Districts_Factsheet_Data.xls'}`
  * Actual: `64.08%`
* **Q149**:
  * Reason: `answer_text_incorrect`
  * Actual: `99.58%`
* **Q150**:
  * Reason: `answer_text_incorrect`
  * Actual: `I couldn't find any relevant passages in the workspace documents matching your query.`
* **Q152**:
  * Reason: `invalid_citations: Citation 0: source 'survay' not in expected sources {'survay.csv'}`
  * Actual: `79%`
* **Q153**:
  * Reason: `invalid_citations: Citation 0: source 'survay' not in expected sources {'survay.csv'}`
  * Actual: `The difference is 4,191 Dollars (millions), from 980,268 to 976,077`
* **Q154**:
  * Reason: `invalid_citations: Citation 0: source document 'Doc-34202080' not in expected sources {'survay.csv'}`
  * Actual: `1521 million dollars: 88214 minus 86693`
* **Q155**:
  * Reason: `answer_text_incorrect`
  * Actual: `The sum for Value is 16790554.`
* **Q156**:
  * Reason: `answer_text_incorrect`
  * Actual: `I couldn't find any relevant passages in the workspace documents matching your query.`
* **Q157**:
  * Reason: `answer_text_incorrect`
  * Actual: `No, it is not correct. The value for H05 in 2025 Beverage and Tobacco Product Manufacturing (CC15) is 'C' ...`
* **Q158**:
  * Reason: `answer_text_incorrect`
  * Actual: `I couldn't find any relevant passages in the workspace documents matching your query.`
* **Q159**:
  * Reason: `refusal_contract_failed_to_refuse`
  * Actual: `No rows matched the requested filters in table 'survay' (0 matching rows). Sum is 0.`
* **Q161**:
  * Reason: `claim_unsupported: numeric claims {'50'} not supported by cited snippets`
  * Actual: `50 days, from 16 March 2016 to 5 May 2016`
* **Q162**:
  * Reason: `claim_contradicts_evidence: answer negates evidence-affirmed entity 'Anti-Hijacking'`
  * Actual: `No, a blank Committee / Report Presented cell does not prove that the Anti-Hijacking Bill was never referred to a committee...`
* **Q163**:
  * Reason: `answer_text_incorrect`
  * Actual: `The assent date for Bill No. LIII is 13/05/2016.`
* **Q164**:
  * Reason: `answer_text_incorrect`
  * Actual: `0`
* **Q165**:
  * Reason: `invalid_citations: Citation 4: source document 'National-Strategy-for-Artificial-Intelligence.pdf' not in expected sources {'rs_status_bill_passed_assent-1952-2016.csv'}`
  * Actual: `No, the Anti-Hijacking Bill with the year 2016 in its title was introduced on 17/12/2014, not in 2016.`
* **Q167**:
  * Reason: `invalid_citations: No citations match evaluator ground-truth evidence chunk IDs`
  * Actual: `Selective Dependence Classification`
* **Q168**:
  * Reason: `answer_text_incorrect`
  * Actual: `Focused Convolutional Attention Mechanism`
* **Q169**:
  * Reason: `invalid_citations: Citation 4: source document 'SEC-Form-10Q.pdf' not in expected sources {'2212.14776v3.pdf'}`
  * Actual: `The average attention values for full, random 20, and top 20 are 0.539, 0.24, and 0.429 respectively.`
* **Q171**:
  * Reason: `answer_text_incorrect`
  * Actual: `No, the interpretability paper does not establish that a model with accurate predictions must focus...`
* **Q172**:
  * Reason: `answer_text_incorrect`
  * Actual: `No, A-MoD beats standard MoD in every Table 1 configuration.`
* **Q173**:
  * Reason: `answer_text_incorrect`
  * Actual: `The accuracy of DeiT-Tiny A-MoD improves from 0.97% to 52.6% when capacity rises from 12.5% to 50%...`
* **Q174**:
  * Reason: `invalid_citations: Citation 4: source document '2212.14776v3.pdf' not in expected sources {'2412.20875v1.pdf'}`
  * Actual: `No, DETR A-MoD does not achieve a higher mAP than standard DETR MoD at 50% capacity...`
* **Q175**:
  * Reason: `invalid_citations: Citation 0: snippet fabricates supporting content not in retained evidence (unsupported numbers {'86'})`
  * Actual: `{"answer": "The three smaller image datasets used for transfer learning in the A-MoD experiments are CUB-200-2011, Cars-196, and Stanford Dogs."}`
* **Q176**:
  * Reason: `answer_text_incorrect`
  * Actual: `A-MoD researchers compute the importance scores directly from the attention maps of previous layers...`
* **Q178**:
  * Reason: `answer_text_incorrect`
  * Actual: `I couldn't find any relevant passages in the workspace documents matching your query.`
* **Q180**:
  * Reason: `answer_text_incorrect`
  * Actual: `No, it is not correct. The computation of EA-series is minimally affected by changes in batch size...`
* **Q181**:
  * Reason: `claim_unsupported: numeric claims {'3'} not supported by cited snippets`
  * Actual: `The four stages of social learning that motivate the Retention Layer paper are: 1) Attention, 2) Retention, 3) Reproduction, and 4) Motivation.`
* **Q183**:
  * Reason: `invalid_citations: Citation 3: source document '2212.14776v3.pdf' not in expected sources {'2501.09166v1.pdf'}`
  * Actual: `The Retention Layer paper suggests three symbolic storage representations as alternatives to raw embeddings...`
* **Q184**:
  * Reason: `invalid_citations: No citations match evaluator ground-truth evidence chunk IDs`
  * Actual: `In Section 4.1 of the Retention Layer paper, m represents the number of stored templates or episodes.`
* **Q188**:
  * Reason: `answer_text_incorrect`
  * Actual: `The two papers that support caution about treating an attention heatmap as proof of why a model predicted an outcome are: 1. Focus-Classify Attention Model paper, 2. Retention Layer paper.`
* **Q189**:
  * Reason: `claim_contradicts_evidence: answer negates evidence-affirmed entity 'EA-series'`
  * Actual: `No, the EA-series constant-length inference cost does not establish the same kind of cross-session memory transfer that the Retention Layer provides.`
* **Q190**:
  * Reason: `answer_text_incorrect`
  * Actual: `I couldn't find any relevant passages in the workspace documents matching your query.`
* **Q191**:
  * Reason: `invalid_citations: Citation 3: source document '2501.09166v1.pdf' not in expected sources {'National-Strategy-for-Artificial-Intelligence.pdf', '2212.14776v3.pdf'}`
  * Actual: `{"answer":"The interpretability paper demonstrates that while a Focus-Classify Attention Model (FCAM) can achieve high accuracy..."}`
* **Q192**:
  * Reason: `answer_text_incorrect`
  * Actual: `I couldn't find any relevant passages in the workspace documents matching your query.`
* **Q193**:
  * Reason: `answer_text_incorrect`
  * Actual: `23,245`
* **Q194**:
  * Reason: `invalid_citations: Citation 0: source document 'Doc-fcc723b5' not in expected sources {'NFHS_5_India_Districts_Factsheet_Data.xls'}`
  * Actual: `{"district": "North & Middle Andaman", "men_interviewed": 108}`

## Conclusion and Recommendations

1. **Benchmark Execution Complete**: Exactly one cold benchmark run was executed with configured providers, cleanly fulfilling the user authorization. No automatic or additional reruns are required.
2. **Evaluator Release 2.2.0 Proven Durable**: All 60 query records can be rescored offline with 100% fidelity, validating that the evaluation engine is reproducible independently of live database or network access.
3. **OpenRouter Reranker Decommissioning Noted**: The remote OpenRouter reranker endpoint consistently returns HTTP 404 for model `nvidia/llama-nemotron-rerank-vl-1b-v2:free`. Upgrading to a current provider model or configuring a reliable local/remote alternative will eliminate the automatic fail safe fallback to lexical BM25Plus.
4. **Vector Provenance Backlog**: 1,697 points in Qdrant retain unresolved model provenance. Future ingestion tasks should resolve vector provenance systematically.
