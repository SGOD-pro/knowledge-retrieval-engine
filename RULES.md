# RULES.md — Engineering Rules + Test Cases

## The Rules

1. Never call an LLM if deterministic code can solve it.
2. Maximum **ONE** LLM call per query, end to end.
3. Never send raw document text to the LLM.
4. Context to LLM: max 1200 tokens, always compressed.
5. BM25 runs before PageIndex. PageIndex scopes Vector. Always.
6. Reranker runs before compression. Always.
7. Statistics and numbers are extracted from text, not computed by LLM.
8. LLM explains retrieved facts. It does not compute or infer them.
9. Cache deterministic outputs. Key: `sha256(query + doc_scope)`.
10. Every module logs `latency_ms` and `confidence_score`. No exceptions.
11. Modules communicate through defined interfaces. No internal imports.
12. Benchmark every change to planner, reranker, compressor, or graph.
13. Graph hard limits: `MAX_HOPS=2`, `MAX_NODES=40`. Hardcoded constants.
14. Fidelity check runs before every LLM call. Blocks on failure.
15. LLM never scores its own confidence. Output schema has no confidence field.
16. Graph activates only on `relationship_flag=True`. Not by default.
17. OKF property lookup always runs in full path (cheap, always worth it).

---

## Test Cases

### RULE 1 — No unnecessary LLM calls

```python
def test_r01_fast_path_zero_llm_calls():
    query = "What is the return policy?"
    # complexity_score will be < 0.30, entity_count = 0
    plan = planner.route(query)
    ASSERT plan.fast_path == True
    with mock.patch("kre.llm.llm_service.call") as mock_llm:
        response = pipeline.run(query)
        ASSERT mock_llm.call_count == 0
    ASSERT len(response.citations) > 0

def test_r01_numerical_facts_not_computed_by_llm():
    # If answer contains "12%" it must come from a chunk, not LLM inference
    query = "What is the battery failure rate?"
    response = pipeline.run(query)
    cited_chunks = [get_chunk(c) for c in response.citations]
    combined_text = " ".join(c.text for c in cited_chunks)
    for number in extract_numbers(response.answer):
        ASSERT str(number) in combined_text  # number must be in source
```

### RULE 2 — Single LLM call maximum

```python
def test_r02_full_path_exactly_one_llm_call():
    query = "Why did revenue decrease in Q3?"
    with count_calls("kre.llm.llm_service.call") as counter:
        response = pipeline.run(query)
    ASSERT counter.count == 1

def test_r02_planner_zero_llm_calls():
    for query in FULL_TEST_QUERY_SET:
        with count_calls("kre.llm.llm_service.call") as counter:
            planner.route(query)
        ASSERT counter.count == 0

def test_r02_okf_lookup_zero_llm_calls():
    with count_calls("kre.llm.llm_service.call") as counter:
        okf_retriever.lookup(["Battery Model X", "Q2 2024"])
    ASSERT counter.count == 0
```

### RULE 3 — Never send raw chunks to LLM

```python
def test_r03_llm_receives_compressed_context():
    query = "What caused the overheating issue?"
    top_6_chunks = run_pipeline_to_stage(query, stop_at="reranker")
    raw_token_count = sum(count_tokens(c.text) for c in top_6_chunks)
    response = pipeline.run(query)
    compressed_token_count = count_tokens(response._llm_input.context)
    ASSERT compressed_token_count < raw_token_count
    ASSERT compressed_token_count <= 1200
```

### RULE 4 — 1200 token hard limit

```python
def test_r04_token_limit_never_exceeded():
    for query in FULL_TEST_QUERY_SET:
        response = pipeline.run(query)
        ASSERT count_tokens(response._llm_input.context) <= 1200
```

### RULE 5 — Stage ordering enforced

```python
def test_r05_bm25_before_pageindex_before_vector():
    query = "What is the refund deadline?"
    plan = planner.route(query)
    stages = plan.stages
    ASSERT stages.index("bm25") < stages.index("page_index")
    ASSERT stages.index("page_index") < stages.index("vector")

def test_r05_vector_search_only_within_candidate_pages():
    query = "battery failure analysis"
    candidate_page_ids = run_stage(query, "page_index").page_ids
    vector_chunk_ids = run_stage(query, "vector").chunk_ids
    all_vector_pages = {get_chunk(c).page_id for c in vector_chunk_ids}
    ASSERT all_vector_pages.issubset(set(candidate_page_ids))
```

### RULE 6 — Reranker before compression

```python
def test_r06_reranker_before_compressor():
    plan = planner.route("Why did sales drop?")
    stages = plan.stages
    ASSERT stages.index("reranker") < stages.index("compressor")

def test_r06_compressor_only_receives_top_6_by_reranker():
    query = "What are the main product defects?"
    candidates = run_stage(query, stop_at="reranker")  # 30 chunks, scored
    compressor_input = run_stage(query, stop_at="compressor")  # 6 chunks
    min_score_in_top_6 = min(c.reranker_score for c in compressor_input)
    for rejected in [c for c in candidates if c not in compressor_input]:
        ASSERT rejected.reranker_score <= min_score_in_top_6
```

### RULE 10 — All modules log latency + confidence

```python
def test_r10_all_modules_log_required_fields():
    required_modules = [
        "bm25", "page_index", "vector", "okf",
        "graph", "reranker", "fidelity_check", "compressor", "llm"
    ]
    query = "Why did revenue decrease?"
    logs = capture_logs(lambda: pipeline.run(query))
    for module in required_modules:
        ASSERT f"{module}.latency_ms" in logs
        ASSERT f"{module}.confidence_score" in logs
```

### RULE 13 — Graph hard caps

```python
def test_r13_graph_max_2_hops_enforced():
    # Build chain: A →[CAUSES]→ B →[CAUSES]→ C →[CAUSES]→ D
    build_test_graph(chain=["A", "B", "C", "D"])
    results = graph_retriever.expand(["A"])
    returned_ids = {n.concept_id for n in results}
    ASSERT "B" in returned_ids
    ASSERT "C" in returned_ids
    ASSERT "D" not in returned_ids  # 3 hops, should not appear

def test_r13_graph_max_40_nodes_enforced():
    # Entity A has 50 direct neighbors
    build_dense_test_graph(center="A", neighbors=50)
    results = graph_retriever.expand(["A"])
    ASSERT len(results) <= 40

def test_r13_low_weight_edges_pruned():
    build_test_graph_with_weights({"A→B": 0.25, "A→C": 0.5})
    results = graph_retriever.expand(["A"])
    ids = {n.concept_id for n in results}
    ASSERT "B" not in ids   # weight 0.25 < 0.30, pruned
    ASSERT "C" in ids       # weight 0.50 >= 0.30, kept
```

### RULE 14 — Fidelity check blocks LLM on low coverage

```python
def test_r14_entity_coverage_check_passes():
    query = "What caused battery overheating in Q2?"
    # query entities: ["battery", "overheating", "Q2"]
    compressed = run_pipeline_to_stage(query, stop_at="compressor")
    for entity in extract_query_entities(query):
        ASSERT entity.lower() in compressed.text.lower()

def test_r14_fidelity_failure_blocks_llm():
    # Force compressor to drop all entities
    with mock_compressor_that_drops_entities():
        with count_calls("kre.llm.llm_service.call") as counter:
            with ASSERT_RAISES(CoverageError):
                pipeline.run("What caused battery overheating in Q2?")
        ASSERT counter.count == 0
```

### RULE 15 — LLM never self-scores confidence

```python
def test_r15_llm_output_has_no_confidence_field():
    query = "What is the battery failure rate?"
    raw_llm_output = capture_llm_raw_output(query)
    parsed = json.loads(raw_llm_output)
    ASSERT "confidence" not in parsed
    ASSERT "certainty" not in parsed
    ASSERT "score" not in parsed

def test_r15_confidence_computed_from_deterministic_formula():
    query = "What caused overheating?"
    response = pipeline.run(query)
    expected = (response._reranker_avg * 0.6) + (response._coverage * 0.4)
    ASSERT abs(response.confidence_score - expected) < 0.001
```

### RULE 16 — Graph only on relationship queries

```python
def test_r16_graph_not_activated_for_factual_query():
    query = "What is the refund policy?"
    plan = planner.route(query)
    ASSERT plan.use_graph == False
    ASSERT "graph" not in plan.stages

def test_r16_graph_activated_for_relationship_query():
    query = "Why did revenue decrease?"
    plan = planner.route(query)
    ASSERT plan.use_graph == True
    ASSERT "graph" in plan.stages
```

### HALLUCINATION CHECK

```python
def test_hallucination_citation_mismatch_rate():
    results = []
    for query, expected in GROUND_TRUTH_TEST_SET:  # 120 queries
        response = pipeline.run(query)
        for citation_id in response.citations:
            chunk = get_chunk(citation_id)
            claims_supported = verify_claims_in_chunk(response.answer, chunk)
            results.append(claims_supported)
    hallucination_rate = 1 - (sum(results) / len(results))
    ASSERT hallucination_rate < 0.05
```

### LATENCY BENCHMARKS

```python
def test_fast_path_p95_under_200ms():
    latencies = []
    for query in FAST_PATH_QUERY_SET:  # 40 simple factual queries
        for _ in range(5):  # 200 total runs
            t0 = time.perf_counter()
            pipeline.run(query)
            latencies.append((time.perf_counter() - t0) * 1000)
    ASSERT numpy.percentile(latencies, 95) < 200

def test_full_pipeline_p95_under_3000ms():
    latencies = []
    for query in FULL_PATH_QUERY_SET:  # 40 complex queries
        for _ in range(5):
            t0 = time.perf_counter()
            pipeline.run(query)
            latencies.append((time.perf_counter() - t0) * 1000)
    ASSERT numpy.percentile(latencies, 95) < 3000
```

