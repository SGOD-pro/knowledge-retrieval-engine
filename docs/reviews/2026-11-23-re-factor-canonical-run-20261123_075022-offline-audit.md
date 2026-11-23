# Review: Offline Audit of Canonical Benchmark Run 20261123_075022

* **Author**: Senior Acceptance Reviewer
* **Date**: 2026-11-23
* **Branch**: `re-factor`
* **Tested Commit**: `73075af21d9172155cad22866d8eef2e7c186edb`
* **Inspected Diff**: [73075af](https://github.com/SGOD-pro/knowledge-retrieval-engine/commit/73075af21d9172155cad22866d8eef2e7c186edb)
* **Supersedes**: [2026-11-23-re-factor-canonical-benchmark-2.2.0-rerun-review.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-23-re-factor-canonical-benchmark-2.2.0-rerun-review.md)

## Summary

This review records the offline audit of canonical run `20261123_075022` using only the saved artifacts, surviving execution logs, and offline replay tools.

No live provider calls, benchmark reruns, ground truth edits, or corpus modifications were made. All original artifacts and scores are preserved. The audit confirms file integrity via cryptographic hashes, clarifies the invocation arguments by checking the task logs, validates 100% offline grounded verdict reproduction, isolates the exact failure mechanisms for Q149, Q144, and Q193, and recommends one prioritized repair for immediate engineering action.

## Artifact Hashes and Run Parameters

The audit verified the three saved benchmark artifacts under `backend/reports/73075af21d9172155cad22866d8eef2e7c186edb/20261123_075022/`:

* [manifest.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/73075af21d9172155cad22866d8eef2e7c186edb/20261123_075022/manifest.json):
  * SHA256: `5ac5d8ec77ed23041eaa5e7a0450d86fdf88f14ca37a668ffd02d44cf950ccb1`
  * Size: 2,901 bytes
* [query_details.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/73075af21d9172155cad22866d8eef2e7c186edb/20261123_075022/query_details.json):
  * SHA256: `bdc677510d88117fc44097957a9551b9c675fb8128f33e301da164a43d3bd7cd`
  * Size: 6,019,824 bytes
* [summary.md](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/73075af21d9172155cad22866d8eef2e7c186edb/20261123_075022/summary.md):
  * SHA256: `0382764cb87be9dce4d1bd4cf51b559566b6509ece032f1385dc714fb47f2305`
  * Size: 1,510 bytes

Run parameters recorded from the artifacts and execution environment:
* **Run Commit SHA**: `73075af21d9172155cad22866d8eef2e7c186edb`
* **Workspace ID**: `ws_canonical_comparison_0004`
* **Query Count**: 60 questions (Q136 through Q195)
* **Provider Regions**:
  * AWS Bedrock: `ap-south-1` region (configured profile `aws` in `~/.aws/credentials`, with Bedrock Nova Lite inference routing to Asia Pacific endpoint `apac.amazon.nova-lite-v1:0`)
  * Qdrant Cloud: `sa-east-1` region (`https://c3e0dbc5-bffe-4bfa-8051-011c5dc46834.sa-east-1-0.aws.cloud.qdrant.io:6333`)

## Reconciling the Unsupported Baseline Commit Flag

The benchmark script `backend/scripts/run_canonical_60_benchmark.py` accepts `--baseline-path` for comparative evaluation, but does not define a `--baseline-commit` argument in its argument parser.

The surviving execution log in the task history confirms that the command executed was:

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

Because `--baseline-path` was not supplied, the runner defaulted `manifest["baseline_commit_sha"]` to `commit_sha` (`73075af21d9172155cad22866d8eef2e7c186edb`) as specified on line 664 of the runner script. The published command template in preflight documentation included `--baseline-commit`, but the actual process invocation ran without this unsupported flag.

## Offline Replay of all 60 Saved Records

All 60 records from `query_details.json` were rescored using `rescore_query_record_offline(record)`.

* **Verdict Agreement**: 60 out of 60 records (100.0%) reproduced the exact grounded pass and fail verdicts.
  * 12 records passed grounded evaluation.
  * 48 records failed grounded evaluation.
* **Reason Comparison**: 58 out of 60 reasons matched character for character.
* **Reason Divergence Explanation**: The 2 minor string variations are non semantic formatting differences resulting from Python hash randomization across unordered sets:
  * In Q140, the rejection reason printed `{'27', '5374'}` during live execution and `{'5374', '27'}` in offline replay because `unsupported_nums` is a Python set.
  * In Q162, the rejection reason printed entity `'Anti-Hijacking'` during live execution and `'ANTI-HIJACKING'` in offline replay because `candidates = set()` in `detect_contradiction_against_evidence` collects both uppercase and title case candidate tokens from table text, iterating them in set hash order.
  * Both runs consistently produced `is_grounded_correct: False` for identical underlying reasons.

## Detailed Trace of Targeted Records

### Q149 Trace: Exact Text Grading Rule that Rejected 99.58%

* **Question**: `South Andaman mein kitne percent log bijli wale gharon mein rehte hain? NFHS file se batao.`
* **Expected Answer**: `99.58% of the population lives in households with electricity.`
* **Actual Answer**: `99.58%`
* **Contract**: Semantic contract (`contract_type: "semantic"`).
* **Grading Logic**: Evaluated via lines 538 to 541 in `run_canonical_60_benchmark.py`:
  `is_correct = c_match and relevancy >= 0.40`
* **Mechanism of Rejection**:
  1. In `benchmark_scorer.py:tokenize`, token extraction is implemented as:
     `[w.lower() for w in re.findall(r"\w+", text) if len(w) > 2]`
  2. For the answer `"99.58%"`, `re.findall(r"\w+", "99.58%")` extracts `['99', '58']`.
  3. Both `'99'` and `'58'` have a character length of 2, failing the strict `len(w) > 2` filter.
  4. Consequently, `tokenize("99.58%")` returns an empty token list `[]`.
  5. In `content_match(chunk_text, expected_answer)`, the token overlap is 0, giving a token recall of `0.0`.
  6. The numeric overlap rule requires:
     `if num_overlap >= 0.5 and recall >= 0.25: return True`
  7. While `num_overlap` is 1.0 (100% of numbers match: `99.58`), the token recall is `0.0`, which fails the `recall >= 0.25` condition.
  8. The function falls through to `return recall >= 0.50`, which evaluates to `False`.
  9. As a result, `c_match` evaluates to `False`, forcing `is_correct = False` even though `compute_answer_relevancy` was 0.60.

### Q144 Trace: Provenance of Doc-fcc723b5 in Final Citation Construction

* **Question**: `How many men aged 15–54 were interviewed in South Andaman in the NFHS workbook?`
* **Expected Answer**: `134`, Actual Answer: `134`.
* **Expected Source**: `NFHS_5_India_Districts_Factsheet_Data.xls`.
* **Retained Document ID**: `fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0`.
* **Retained Version**: `None`.
* **Origin of Doc-fcc723b5**:
  1. The chunk originated from tabular Excel extraction where `chunk.metadata` holds `{'sheet': 'Sheet1', 'row': 4, 'headers': [...]}` without an explicit `filename` key.
  2. In `services/retrieval/evidence_contract.py:evidence_item_from_chunk`, the fallback logic created `citation_payload.document_filename = f"doc_{did}.pdf"` (`doc_fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0.pdf`).
  3. During query finalization in `services/retrieval/response_builder.py:build_citation`, `document_filename` was not passed as an argument, and `getattr(chunk, "document_filename", "")` was empty.
  4. Lines 124 to 131 attempted to retrieve the filename via `CloudRepository().get(did_str)` against DynamoDB. Because the benchmark ran with an isolated SQLite boundary database (`/tmp/kre_workspace_boundary_canonical.db`), DynamoDB lookups returned `None`.
  5. The code reached the fallback on line 133 of `response_builder.py`:
     `doc_fname = f"Doc-{str(chunk.document_id)[:8]}"`
  6. This truncated the UUID to 8 characters and produced `"Doc-fcc723b5"`.
  7. During grounded evaluation, `evaluate_grounded_answer` checked whether `"Doc-fcc723b5"` belonged to `expected_sources` (`{'NFHS_5_India_Districts_Factsheet_Data.xls'}`), triggering citation rejection:
     `invalid_citations: Citation 0: source document 'Doc-fcc723b5' not in expected sources {'NFHS_5_India_Districts_Factsheet_Data.xls'}`.

### Q193 Trace: First Saved Stage Supporting 23,245 instead of 30,739

* **Question**: Adversarial question asking for Apple Services sales for the three months ended June 27, 2026.
* **Expected Answer**: `30,739` million dollars (Net Sales).
* **Actual Answer**: `23,245` (Gross Margin).
* **Corpus Grounding**:
  * Page 18 element 17 and page 9 element 8 in `SEC-Form-10Q.pdf` contain the Net Sales table stating Services net sales of `30,739`.
  * Page 19 element 18 in `SEC-Form-10Q.pdf` contains the Gross Margin table stating Services gross margin of `23,245`.
* **Stage by Stage Tracing in Saved Artifacts**:
  1. **Pre Rerank Candidate Generation Stage**: Not saved in `query_details.json`. The raw pool of 40 candidates before reranking cannot be established from the artifacts.
  2. **Reranker Output Stage (`reranked_chunk_ids`)**: First saved stage containing `23,245`. Chunk `492ccb2d-0d0d-5022-8a7b-e3f87df8befd:page:19:element:18` appears at index 3.
  3. **Compressor Context Stage (`compressed_context_chunk_ids`)**: Contains both the net sales chunk (`page:18:element:17`) and the gross margin chunk (`page:19:element:18`).
  4. **LLM Citation Stage (`llm_cited_chunk_ids`)**: The generation model chose `492ccb2d-0d0d-5022-8a7b-e3f87df8befd:page:19:element:18` as its sole supporting citation.
  5. **LLM Generation Output (`answer`)**: Emitted `23,245`, taking the Services gross margin number rather than net sales.

## One Prioritized Repair

* **Highest Impact Defect**: Document filename resolution fallback in `services/retrieval/response_builder.py`.
* **Problem**: When running on SQLite workspace databases or tabular Excel chunks, `build_citation` cannot resolve the document filename via DynamoDB `CloudRepository` and falls back to `f"Doc-{chunk.document_id[:8]}"`.
* **Impact**: At least 6 queries (Q144, Q145, Q146, Q148, Q154, Q194) produced completely correct numerical answers and accurate chunk citations, but failed grounded scoring solely because the citation filename was emitted as `"Doc-fcc723b5"` or `"Doc-34202080"` instead of matching `expected_sources`.
* **Recommended Repair**: Update `response_builder.py:build_citation` to resolve document filenames using the active workspace boundary repository (supporting SQLite, TableStore, and document snapshots) before falling back to truncated UUID identifiers. This single surgical change will eliminate multiple false citation failures and directly improve grounded accuracy.
