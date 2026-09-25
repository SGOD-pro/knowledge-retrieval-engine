# Retrieval Strategy Decoupling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple the coupled retrieval chain in `knowledge-retrieval-engine` into independently testable evidence retrieval strategies behind one shared contract, evaluate each strategy independently on the canonical benchmark, generate a strategy capability matrix, implement a data-driven router, and integrate into the pipeline with strict retained-evidence citation validation.

**Architecture:** 
1. Introduce a single, shared `EvidenceItem` and `RetrievalStrategyResult` contract in `backend/src/services/retrieval/evidence_contract.py`.
2. Define a clean, uniform async `RetrievalStrategy` protocol in `backend/src/services/retrieval/strategies/base.py`.
3. Implement 6 independent strategies: `VectorRerankStrategy`, `LexicalBM25Strategy`, `StructuredTableStrategy`, `PageIndexStrategy`, `KnowledgeGraphStrategy`, and `OKFStrategy`.
4. Update citation validation in `backend/src/services/evaluation/benchmark_scorer.py` so every final citation must strictly match an `EvidenceItem` retained during the same request.
5. Create `backend/scripts/evaluate_retrieval_strategies.py` and produce the comparison report in `backend/reports/strategy_evaluation/<commit>/<timestamp>/strategy_matrix.md`.
6. Implement `backend/src/services/retrieval/strategy_router.py` based on measured strategy capabilities.
7. Integrate the router into `backend/src/services/langgraph_pipeline.py`.
8. Validate with tests, strategy evaluation harness, and canonical 60 benchmark.

**Tech Stack:** Python 3.14, FastAPI, LangGraph, Qdrant, DynamoDB / SQLite TableStore, Rank-BM25, Pytest.

**Spec:** User request at commit `54ec773`.

## Global Constraints
- Do not put benchmark question IDs, expected answers, filenames, or corpus-specific routing rules in production code.
- Keep one shared workspace/document identity model, authorization scope, evidence contract, citation validator, answer generation stage, and benchmark scorer.
- Each retrieval strategy must be testable alone before routing.
- Missing evidence must be visible as a strategy failure, not hidden by later fallback.
- Preserve telemetry: latency, token cost, remote calls, retrieved evidence IDs, cited evidence IDs, and strategy name per query. Missing telemetry must be reported as unknown, not zero.
- Infrastructure failure is scored as incorrect or unmeasured, never as a correct refusal.
- No strategy may call answer generation; each strategy only returns evidence.

## Review Focus
1. **Citation validation fabrication resistance:** A citation claiming a valid hash or chunk ID that was not actually in the retained `EvidenceItem` list for this request must be rejected.
2. **Telemetry honesty:** A strategy making remote calls (e.g. Bedrock Titan embeddings, Cohere reranker) must accurately reflect them; BM25 and Structured must record strictly zero remote/LLM/embedding calls.
3. **Structured strategy fail-closed behavior:** When table data coverage is incomplete, schema binding is ambiguous, or storage fails, `StructuredTableStrategy` must fail closed with an explicit failure reason rather than silently swallowing errors or hallucinating results.
4. **Independent strategy execution:** Every strategy must be runnable in isolation with no dependence on earlier pipeline steps having populated chunks or embeddings.
5. **Report and benchmark integrity:** The evaluation harness must measure evidence retrieval only (no LLM generation), questions with no evidence must be evaluated separately from Recall@k denominators, and the strategy comparison matrix must include every question exactly once per strategy.

---

### Task 1: Shared Evidence Contract & Retained Citation Validator (Phase 1)

**Files:**
- Create: `backend/src/services/retrieval/evidence_contract.py`
- Modify: `backend/src/services/evaluation/benchmark_scorer.py:742-835`
- Test: `backend/tests/test_evidence_contract.py`

**Interfaces:**
- Produces: `EvidenceItem`, `RetrievalStrategyResult`, `RetrievalLimits`, `evidence_item_from_chunk`, `evidence_item_from_structured_result`, `evidence_item_from_kg`, `evidence_item_from_okf`
- Modifies: `validate_citations(citations, context_chunk_ids, ground_truth_chunk_ids, retained_structured_hashes, retained_evidence_items)`

- [ ] **Step 1: Write tests for EvidenceItem, RetrievalStrategyResult, and strict citation validation**
- [ ] **Step 2: Run tests to verify failure**
- [ ] **Step 3: Implement `backend/src/services/retrieval/evidence_contract.py`**
- [ ] **Step 4: Update `validate_citations` in `benchmark_scorer.py` to require every citation to match a retained `EvidenceItem`**
- [ ] **Step 5: Run tests to verify pass**

---

### Task 2: Strategy Interface Protocol (Phase 2)

**Files:**
- Create: `backend/src/services/retrieval/strategies/base.py`
- Modify: `backend/src/services/retrieval/strategies/__init__.py`
- Test: `backend/tests/test_strategy_protocol.py`

**Interfaces:**
- Produces: `RetrievalStrategy` protocol with `async def retrieve(query, workspace_id, plan, limits, telemetry) -> RetrievalStrategyResult`

- [ ] **Step 1: Write test verifying strategy protocol compliance**
- [ ] **Step 2: Run test to verify failure**
- [ ] **Step 3: Implement `RetrievalStrategy` in `backend/src/services/retrieval/strategies/base.py`**
- [ ] **Step 4: Run test to verify pass**

---

### Task 3: Implement the 6 Retrieval Strategies (Phase 3)

**Files:**
- Create: `backend/src/services/retrieval/strategies/vector_rerank.py`
- Create: `backend/src/services/retrieval/strategies/bm25.py`
- Create: `backend/src/services/retrieval/strategies/structured_table.py`
- Create: `backend/src/services/retrieval/strategies/page_index.py`
- Create: `backend/src/services/retrieval/strategies/knowledge_graph.py`
- Create: `backend/src/services/retrieval/strategies/okf.py`
- Test: `backend/tests/test_retrieval_strategies.py`

**Interfaces:**
- Produces: 
  - `VectorRerankStrategy`: Embeds with Titan (or fast BGE), queries Qdrant, reranks, returns chunk evidence with debug trace (raw candidates, reranked, retained IDs, call counts).
  - `LexicalBM25Strategy`: Pure BM25, 0 remote calls, returns chunk evidence with matched terms, scores, doc distribution.
  - `StructuredTableStrategy`: Deterministic TableStore execution, 0 LLM / 0 embedding calls, fails closed on incomplete data, ambiguous binding, or storage failure. Returns `table_row_set` evidence.
  - `PageIndexStrategy`: Page/chunk structural weighting, records retrieval LLM calls if used. Returns `page_region` or `chunk` evidence.
  - `KnowledgeGraphStrategy`: Expands entities and relations, extracts linked chunk evidence, returns `kg_node`, `kg_edge`, or linked chunk evidence with graph provenance.
  - `OKFStrategy`: Queries OKF-compatible runtime storage for facts/properties, returns `okf_fact` evidence with provenance.

- [ ] **Step 1: Write comprehensive unit and integration tests in `backend/tests/test_retrieval_strategies.py`**
- [ ] **Step 2: Run tests to verify failures**
- [ ] **Step 3: Implement `VectorRerankStrategy`**
- [ ] **Step 4: Implement `LexicalBM25Strategy`**
- [ ] **Step 5: Implement `StructuredTableStrategy`**
- [ ] **Step 6: Implement `PageIndexStrategy`**
- [ ] **Step 7: Implement `KnowledgeGraphStrategy`**
- [ ] **Step 8: Implement `OKFStrategy`**
- [ ] **Step 9: Run tests to verify all 6 strategies pass**

---

### Task 4: Independent Strategy Evaluation Harness (Phase 4)

**Files:**
- Create: `backend/scripts/evaluate_retrieval_strategies.py`
- Test: `backend/tests/test_strategy_evaluation_harness.py`

**Interfaces:**
- Produces: CLI tool that loads benchmark questions, executes each strategy independently, resolves ground truth evidence, computes retrieval metrics (Recall@5, Precision@3, MRR@5, NDCG@5, latency, remote calls), detects refusal candidates, unsupported evidence, and false premise correction support, and outputs JSON metrics. Does NOT call answer generation.

- [ ] **Step 1: Write test for evaluation harness logic in `backend/tests/test_strategy_evaluation_harness.py`**
- [ ] **Step 2: Run test to verify failure**
- [ ] **Step 3: Implement `backend/scripts/evaluate_retrieval_strategies.py`**
- [ ] **Step 4: Run test to verify pass**

---

### Task 5: Run Independent Strategy Evaluation & Generate Comparison Report (Phase 5)

**Files:**
- Run: `backend/scripts/evaluate_retrieval_strategies.py` against `ws_fresh_benchmark`
- Generate: `backend/reports/strategy_evaluation/<commit>/<timestamp>/strategy_matrix.md`
- Test: `backend/tests/test_strategy_report.py`

**Deliverables:**
- Strategy evaluation matrix table by Category (Best Strategy, Recall@5, Precision@3, MRR@5, Latency p95, Notes)
- Per-question winner table (Question, Category, Winning Strategy, Found Evidence, Missed Evidence)
- Analysis answering the 5 required questions (factual, table aggregation, multi-hop, false premise, failure attribution).

- [ ] **Step 1: Write test verifying report generation structure and single question count per strategy**
- [ ] **Step 2: Execute `scripts/evaluate_retrieval_strategies.py` on `ws_fresh_benchmark`**
- [ ] **Step 3: Inspect results and write `strategy_matrix.md` with complete analysis**

---

### Task 6: Strategy Router from Measurements (Phase 6)

**Files:**
- Create: `backend/src/services/retrieval/strategy_router.py`
- Test: `backend/tests/test_strategy_router.py`

**Interfaces:**
- Consumes: query, plan, workspace metadata, available indexed artifacts, measured capability map.
- Produces: `SelectedStrategies(primary: list[str], fallback: list[str], reason: str, max_parallel: int)`
- Generic rules:
  - structured table query → `StructuredTableStrategy`
  - exact phrase / identifier / code lookup → `LexicalBM25Strategy` + `VectorRerankStrategy`
  - semantic factual query → `VectorRerankStrategy`
  - multi-hop entity relation query → `KnowledgeGraphStrategy` + `VectorRerankStrategy`
  - page layout / visual document question → `PageIndexStrategy`
  - OKF fact/property query → `OKFStrategy`
  - false premise question → retrieve correction evidence from at least two eligible strategies when possible
- NO benchmark question IDs, filenames, or expected answers.

- [ ] **Step 1: Write tests for `StrategyRouter` in `backend/tests/test_strategy_router.py`**
- [ ] **Step 2: Run test to verify failure**
- [ ] **Step 3: Implement `StrategyRouter` in `backend/src/services/retrieval/strategy_router.py`**
- [ ] **Step 4: Run test to verify pass**

---

### Task 7: LangGraph Pipeline Strategy Router Integration (Phase 7)

**Files:**
- Modify: `backend/src/services/langgraph_pipeline.py`
- Modify: `backend/src/modules/query/query_service.py`
- Test: `backend/tests/test_pipeline_strategy_integration.py`

**Interfaces:**
- Pipeline flow:
  1. Plan query
  2. Select strategies via `StrategyRouter`
  3. Run selected retrieval strategies
  4. Merge evidence (preserving strategy source, no silent overwrites)
  5. Validate retained evidence
  6. Optional deterministic executor
  7. Optional answer generation
  8. Validate final citations against retained evidence (preserving strategy in citation payload)

- [ ] **Step 1: Write integration tests in `backend/tests/test_pipeline_strategy_integration.py`**
- [ ] **Step 2: Run test to verify failure**
- [ ] **Step 3: Update `langgraph_pipeline.py` and `query_service.py` to route through `StrategyRouter` and preserve retained `EvidenceItem`s**
- [ ] **Step 4: Run integration tests to verify pass**

---

### Task 8: Verification of Required 15 Tests (Phase 8 & 9)

**Files:**
- `backend/tests/test_retrieval_strategies.py`
- `backend/tests/test_strategy_router.py`
- `backend/tests/test_strategy_evaluation_harness.py`
- `backend/tests/test_pipeline_strategy_integration.py`

**Checks:**
1. Each strategy can run independently and returns `RetrievalStrategyResult`.
2. Vector strategy records embedding and reranker calls.
3. BM25 strategy uses zero remote calls.
4. Structured strategy uses zero LLM and zero embedding calls.
5. Structured strategy fails closed on incomplete table coverage.
6. PageIndex strategy records retrieval LLM calls if used.
7. KG strategy returns linked evidence with graph provenance.
8. OKF strategy returns OKF-compatible evidence with provenance.
9. Router never references question IDs, expected answers, or benchmark filenames.
10. Citation validator rejects citations not present in retained evidence.
11. Strategy evaluation harness does not call final answer generation.
12. Strategy comparison report includes every benchmark question exactly once per strategy.
13. Router integration preserves strategy name in final citations.
14. Missing telemetry is reported as unknown, not zero.
15. Infrastructure failure is scored as incorrect or unmeasured, never as a correct refusal.

- [ ] **Step 1: Execute all pytest test files individually and the entire suite (`pytest tests/ -q`)**
- [ ] **Step 2: Fix any regressions or edge cases**

---

### Task 9: Canonical 60 Benchmark Run & Final Report (Phase 9 & Required Output)

**Files:**
- Run: `backend/scripts/run_canonical_60_benchmark.py --workspace-id ws_fresh_benchmark --benchmark-type cold`
- Analyze and prepare the final report with all 10 required deliverables.

- [ ] **Step 1: Execute `run_canonical_60_benchmark.py`**
- [ ] **Step 2: Compile the per-strategy, per-category, router rules, and comparison details**
- [ ] **Step 3: Output the full final report**
