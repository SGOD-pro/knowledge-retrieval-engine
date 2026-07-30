# RULES.md — Engineering Rules + Test Cases (rev 5)

## The Rules

1.  Never call an LLM if deterministic code can solve it.
2.  Maximum ONE LLM call per query, end to end. (Nova Micro during ingestion is exempt).
3.  Never send raw document text to the LLM. (Embedding/reranker APIs do receive raw chunk text).
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
18. Nova Micro is NEVER called during query execution.
19. Fast path uses LOCAL BGE-small-en-v1.5 embeddings. Full path uses API embeddings.
20. bounding_box may be null for non-PDF sources. Every citation must have a non-null location reference.
27. No local model weights, ML framework binaries, or GPU-dependent libraries in the deployment package EXCEPT for `onnxruntime` and ONNX-exported BGE-small weights scoped exclusively to the query Lambda's fast path. `torch`, `transformers`, etc., remain strictly forbidden. Lambda zip limit is 250MB unzipped; container limit is 10GB.
28. All embedding (full-path), reranking, and LLM calls route through providers/*.py.
29. MODEL_PROVIDER=dev is prohibited in any environment tagged "production".
30. Query embeddings and chunk embeddings compared in a similarity search must come from the SAME provider.

> **CRITICAL NOTE ON PROVIDER TESTS:**
> Tests that assert provider-call behavior require verified live provider configuration before they can be considered meaningful — a green test against an unconfigured provider is not evidence of correctness. Always run the Provider Configuration Checkpoint (Step 0) in Phase 3.

---

## Test Cases

### Multi-format ingestion tests
[Same as rev 4]

### Nova Micro quality gate test
[Same as rev 4]

### Titan V2 embedding dimension test
[Same as rev 4]

### RULE 19 & RULE 27 — Network Request Assertions (NEW)

```python
def test_fast_path_embedding_makes_zero_network_calls():
    # Assert no HTTP call to any embedding provider during a fast-path query
    query = "What is the refund policy?"
    plan = planner.route(query)
    ASSERT plan.fast_path == True
    with capture_provider_calls() as calls:
        pipeline.run(query)
    embedding_calls = [c for c in calls if c.module == "providers.embedding_provider"]
    ASSERT len(embedding_calls) == 0

def test_full_path_embedding_makes_exactly_one_network_call():
    query = "Why did sales drop?"
    with capture_provider_calls() as calls:
        pipeline.run(query)
    embedding_calls = [c for c in calls if c.module == "providers.embedding_provider"]
    ASSERT len(embedding_calls) == 1
```

### RULE 1 — No unnecessary LLM calls
[Same as rev 4]

### RULE 2 — Single LLM call maximum
[Same as rev 4]

### RULE 3 — Never send raw document text to the LLM
[Same as rev 4]

### RULE 4 — 1200 token hard limit
[Same as rev 4]

### RULE 5 — Stage ordering enforced
[Same as rev 4]

### RULE 6 — Reranker before compression
[Same as rev 4]

### RULE 10 — All modules log latency + confidence
[Same as rev 4]

### RULE 13 — Graph hard caps
[Same as rev 4]

### RULE 14 — Fidelity check blocks LLM on low coverage
[Same as rev 4]

### RULE 15 — LLM never self-scores confidence
[Same as rev 4]

### RULE 16 — Graph only on relationship queries
[Same as rev 4]

### HALLUCINATION CHECK
[Same as rev 4]

### LATENCY BENCHMARKS
[Same as rev 4]

### RULE 18 — Nova Micro never called at query time
[Same as rev 4]

### RULE 27 — Deployment package constraints

```python
def test_r27_query_lambda_package_under_250mb():
    package_size = get_unzipped_lambda_size_mb()
    ASSERT package_size < 250

def test_r27_no_forbidden_dependencies():
    forbidden = ["torch", "onnxruntime-gpu", "transformers", "faiss"]
    installed = get_requirements_txt_packages()
    for pkg in forbidden:
        ASSERT pkg not in installed, f"{pkg} must not ship in Lambda package"
    ASSERT "onnxruntime" in installed # Explicitly allowed exception
```

### RULE 28 — Provider routing enforced
[Same as rev 4]

### RULE 29 — Prod cannot use dev provider
[Same as rev 4]

### RULE 30 — No cross-provider embedding comparison
[Same as rev 4]

```python
def test_r30_schema_level_routing_isolation():
    # Assert that a fast-path query's SQL ONLY references embedding_fast
    fast_query = "What is the refund policy?"
    fast_plan = planner.route(fast_query)
    fast_sql = vector_retriever.build_query_sql(fast_query, fast_plan)
    ASSERT "embedding_fast" in fast_sql
    ASSERT "embedding_full" not in fast_sql

    # Assert that a full-path query's SQL ONLY references embedding_full
    full_query = "Why did sales drop?"
    full_plan = planner.route(full_query)
    full_sql = vector_retriever.build_query_sql(full_query, full_plan)
    ASSERT "embedding_full" in full_sql
    ASSERT "embedding_fast" not in full_sql
```

### Cold start latency tracking
[Same as rev 4]

### Reranker text-only mode verification
[Same as rev 4]

### RULE 20 — No null location reference in citations
[Same as rev 4]