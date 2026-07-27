# RULES.md — Engineering Rules + Test Cases

## RULES.md — Engineering Rules + Test Cases (rev 4)

## The Rules (changes from rev 3 marked with →)

1.  Never call an LLM if deterministic code can solve it.
2.  Maximum ONE LLM call per query, end to end.
    → Rule 2 governs online query path only. Ingestion-time
      LLM calls (Nova Micro for OKF) are not query-time calls.
3.  Never send raw document text to the LLM.
    → CLARIFIED: this rule applies to the LLM provider only.
      Embedding and reranker API calls DO receive raw chunk text —
      that is inherent to using external embedding/reranking APIs.
      This is a documented boundary change (see BOUNDARIES.md),
      not a rule violation.
4.  Context to LLM: max 1200 tokens, always compressed.
5.  BM25 runs before PageIndex. PageIndex scopes Vector. Always.
6.  Reranker runs before compression. Always.
7.  Statistics and numbers are extracted from text, not by LLM.
8.  LLM explains retrieved facts. It does not compute or infer.
9.  Cache deterministic outputs. Key: sha256(query + doc_scope).
10. Every module logs latency_ms and confidence_score. No exceptions.
11. Modules communicate through defined interfaces. No internal imports.
12. Benchmark every change to planner, reranker, compressor, or graph.
13. Graph: MAX_HOPS=2, MAX_NODES=40. Hardcoded constants.
14. Fidelity check runs before every LLM call. Blocks on failure.
15. LLM never scores its own confidence. Schema rejects it.
16. Graph activates only on relationship_flag=True.
17. OKF property lookup always runs in full path.
→18. Nova Micro is NEVER called during query execution.
     Ingestion-time only. Violation = blocked PR.
→19. Fast path uses pgvector-backed embeddings via provider layer.
     Full path Stage 3 uses the same provider-based embedding path.
     Mixing providers in the same corpus is a correctness bug.
→20. bounding_box may be null for non-PDF sources.
     Every citation must have a non-null location reference
     (bounding_box OR sheet/row/slide fallback). Never null location.
→27. No local model weights, ML framework binaries, or GPU-dependent
     libraries in the Lambda deployment package. CI enforces package
     size <50MB zipped. Any PR that adds torch/onnxruntime/transformers
     as a non-dev dependency is blocked automatically.
→28. All embedding, reranking, and LLM calls route through
     providers/*.py. No module imports an OpenRouter or Bedrock SDK
     directly. Enforced by import-linter or equivalent static check.
→29. MODEL_PROVIDER=dev is prohibited in any environment tagged
     "production" in deployment config. CI blocks deploy if violated.
→30. Query embeddings and chunk embeddings compared in a similarity
     search must come from the SAME provider. A dev-embedded query
     against prod-embedded chunks (or vice versa) is a silent
     correctness bug, not just a performance issue — enforced by
     tagging every embedding row with its provider and filtering on it.

---

## Test Cases

### Multi-format ingestion tests

def test_docx_ingestion_produces_valid_chunks():
    doc_id = ingest_file("test_document.docx")
    chunks = get_chunks(doc_id)
    ASSERT len(chunks) > 0
    ASSERT all(c.source_format == "docx" for c in chunks)
    ASSERT all(c.bounding_box is None for c in chunks)
    heading_chunks = [c for c in chunks if c.element_type == "heading"]
    ASSERT len(heading_chunks) > 0

def test_xlsx_ingestion_preserves_computed_values():
    doc_id = ingest_file("test_spreadsheet.xlsx")
    chunks = get_chunks(doc_id)
    cell_chunks = [c for c in chunks if c.element_type == "cell"]
    ASSERT not any("=" in c.text for c in cell_chunks)  # no raw formulas

def test_pptx_speaker_notes_ingested_as_caption():
    doc_id = ingest_file("test_presentation.pptx")
    chunks = get_chunks(doc_id)
    note_chunks = [c for c in chunks if c.element_type == "caption"]
    ASSERT len(note_chunks) > 0

### Nova Micro quality gate test

def test_nova_micro_property_precision_gate():
    # Run on 10 validation documents with known ground-truth properties
    extracted = []
    for doc in VALIDATION_DOCS_10:
        props = concept_service.extract_properties_nova_micro(doc.chunks)
        extracted.extend(props)
    correct = sum(
        1 for p in extracted
        if ground_truth_contains(p, VALIDATION_GROUND_TRUTH)
    )
    precision = correct / len(extracted)
    ASSERT precision >= 0.85, (
        f"Nova Micro precision {precision:.2f} below 0.85 gate. "
        "Do not ship OKF. Remove typed-property claims from PROJECT.md."
    )

### Titan V2 embedding dimension test

def test_titan_embeddings_are_1024_dim():
    chunks = [get_chunk(c) for c in SAMPLE_CHUNK_IDS[:5]]
    embeddings = embed_service.embed_titan(chunks)
    for emb in embeddings:
        ASSERT len(emb) == 1024

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

### RULE 3 — Never send raw document text to the LLM

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
def test_fast_path_p95_under_400ms():
    latencies = []
    for query in FAST_PATH_QUERY_SET:  # 40 simple factual queries
        for _ in range(5):  # 200 total runs
            t0 = time.perf_counter()
            pipeline.run(query)
            latencies.append((time.perf_counter() - t0) * 1000)
    ASSERT numpy.percentile(latencies, 95) < 400

def test_full_pipeline_p95_under_4000ms():
    latencies = []
    for query in FULL_PATH_QUERY_SET:  # 40 complex queries
        for _ in range(5):
            t0 = time.perf_counter()
            pipeline.run(query)
            latencies.append((time.perf_counter() - t0) * 1000)
    ASSERT numpy.percentile(latencies, 95) < 4000
```

### RULE 18 — Nova Micro never called at query time

def test_r18_nova_micro_zero_query_time_calls():
    for query in FULL_TEST_QUERY_SET:
        with count_calls("kre.ingestion.concept_service.nova_micro_extract") as c:
            pipeline.run(query)
        ASSERT c.count == 0

### RULE 19 — Provider-based embedding path

def test_r19_embeddings_go_through_provider_layer():
    with capture_provider_calls() as calls:
        vector_retriever.search(query="test query", doc_scope=["test_doc"])
    ASSERT any(call.module == "providers.embedding_provider" for call in calls)

### RULE 27 — Deployment package constraints

def test_r27_lambda_package_under_50mb():
    package_size = get_built_lambda_zip_size_mb()
    ASSERT package_size < 50

def test_r27_no_forbidden_dependencies():
    forbidden = ["torch", "onnxruntime", "transformers", "faiss"]
    installed = get_requirements_txt_packages()
    for pkg in forbidden:
        ASSERT pkg not in installed, f"{pkg} must not ship in Lambda package"

### RULE 28 — Provider routing enforced

def test_r28_no_direct_sdk_imports_in_retrieval():
    retrieval_files = glob("kre/retrieval/*.py")
    for f in retrieval_files:
        content = read_file(f)
        ASSERT "openrouter" not in content.lower()
        ASSERT "boto3.client('bedrock" not in content
        # only providers/*.py may import these SDKs directly

### RULE 29 — Prod cannot use dev provider

def test_r29_prod_env_blocks_dev_provider():
    with mock_env(ENVIRONMENT="production", MODEL_PROVIDER="dev"):
        with ASSERT_RAISES(ConfigurationError):
            provider_client.get_active_provider()

### RULE 30 — No cross-provider embedding comparison

def test_r30_query_and_corpus_provider_must_match():
    ingest_with_provider("test_doc.pdf", provider="dev")
    with mock_env(MODEL_PROVIDER="prod"):
        with ASSERT_RAISES(ProviderMismatchError):
            vector_retriever.search(query="test query", doc_scope=["test_doc"])

### Cold start latency tracking (NEW, not a pass/fail gate — observability)
def test_cold_start_latency_logged_separately():
    invoke_lambda_cold()  # force new execution environment
    logs = capture_lambda_logs()
    ASSERT "cold_start_latency_ms" in logs
    ASSERT "cold_start" not in logs["fast_path_p95_measurement"]
    # cold start must not silently blend into the warm p95 metric

### Reranker text-only mode verification

def test_reranker_vl_model_called_text_only():
    with capture_reranker_api_calls() as calls:
        reranker.rerank(query="test", candidates=SAMPLE_CHUNKS)
    for call in calls:
        ASSERT call.payload.get("image") is None
        ASSERT call.payload.get("text") is not None

### RULE 20 — No null location reference in citations

def test_r20_all_citations_have_location():
    for query in FULL_TEST_QUERY_SET:
        response = pipeline.run(query)
        for citation in response.citations:
            chunk = get_chunk(citation.chunk_id)
            if chunk.source_format == "pdf":
                ASSERT citation.bounding_box is not None
            else:
                ASSERT citation.location_reference is not None
                ASSERT citation.location_reference != ""