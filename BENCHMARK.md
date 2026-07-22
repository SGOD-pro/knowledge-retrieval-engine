# BENCHMARK.md — Targets and Evaluation

## Targets (all must pass simultaneously at Phase 5)

| Metric | Target | Method |
| :--- | :--- | :--- |
| Fast path p95 latency | <200ms | 200 runs on local hardware |
| Full pipeline p95 latency | <3000ms | 200 runs on local hardware |
| Graph traversal p95 | <150ms | 200 runs, relationship queries only |
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

## Metric Definitions

### MRR@5 (Mean Reciprocal Rank)
```text
MRR = (1 / |Q|) * Σ (1 / rank_of_first_relevant_result)
```
- For each query: what rank was the first correct chunk?
- `1.0` = correct answer was rank 1 every time.
- `0.5` = correct answer was rank 2 on average.

### nDCG@5 (Normalized Discounted Cumulative Gain)
- Rewards finding relevant chunks at higher ranks.
- Penalizes relevant chunks buried at rank 4–5.
```text
nDCG = DCG / IDCG where DCG = Σ (rel_i / log2(i + 1))
```
- Compute using `sklearn.metrics.ndcg_score`.

### Precision@k
- Of the top-k retrieved chunks, what fraction are relevant?
- More demanding than Recall. Catches over-retrieval.

### Context Recall
- % of query entities present in compressed context sent to LLM.
- **Target:** 100% (enforced by `fidelity_check`).
- **Benchmark measurement:** run against 40 entity-rich queries.

### Context Precision
- % of compressed context tokens actually cited in the final answer.
- Low context precision = sending irrelevant context to the LLM (wastes tokens, increases hallucination risk).
- **Target:** >65%.

### Answer Relevancy
- Human rater scores 1–5: does the answer actually answer the question?
- Report mean score > 3.75 (= >0.75 on 0–1 scale).

### Faithfulness
- For each claim sentence in the answer:
  - Is there a cited chunk that contains sufficient information to support this claim?
```text
faithfulness = supported_claims / total_claims
```
- Human verification. Cannot be automated reliably in v1.

### Hallucination Rate
- Did the answer state any specific fact (number, date, name) that does not appear in any cited chunk?
```text
hallucination_rate = hallucinated_facts / total_specific_facts
```

---

## Test Data Requirements

### Minimum 3 PDF Types:
- **Type A:** Dense text report (e.g., annual report, policy document)
- **Type B:** Multi-column academic or technical paper
- **Type C:** Table-heavy financial or operational document

Per type: 40 manually curated `(query, answer, source_page)` triples.  
Total: 120 test cases.

### Query Distribution (enforced, not optional):
- **Factual/lookup:** 40% (40 queries) → routes to fast path
- **Relationship/causal:** 30% (36 queries) → routes to graph path
- **Analytical/temporal:** 30% (44 queries) → routes to analytical path

> [!IMPORTANT]
> **CRITICAL:** Do not generate test queries with an LLM. Use real questions a real user would ask of the specific document. Synthetic LLM queries are biased toward what the LLM thinks is answerable — which is not the same as what users actually ask.

---

## Competitor Baseline (required before claiming KRE is better)

Run the same 120-query test set against:
- **Baseline A:** Naive vector RAG (LangChain default retriever + same LLM, no PageIndex, no OKF)
- **Baseline B:** BM25 only + same LLM (`rank-bm25`, no vector search, no graph)
- **Baseline C:** BM25 + vector hybrid + same LLM (no PageIndex structural scoring, no OKF, no graph)

For each baseline, compute: Recall@5, MRR@5, Faithfulness, Hallucination rate, p95 latency.

Document the delta. KRE's value claims must be backed by these numbers, not architectural diagrams.

---

## Regression Policy

Any change to these files requires full benchmark re-run before merge:
- `planner.py`
- `page_index_retriever.py`
- `reranker.py`
- `compressor.py`
- `graph_retriever.py`
- `okf_retriever.py`

- **CI gate:** benchmark regresses >5% on ANY metric → block merge.
- **Baseline numbers:** recorded in `benchmark_baseline.json` after Phase 3. Never update baseline without team review.

