# DECISION.md — Planner Rules and System Decisions

## Three-Way Dev Environment Split

Dev environment is NOT fully local or fully cloud. It is a strict three-way split:
1. **FLOCI-EMULATED (dev only):** RDS PostgreSQL and ElastiCache Redis are emulated locally via `floci`.
2. **REAL CLOUD (Bedrock/OpenRouter):** All LLM and API-based embedding/reranker calls hit real cloud endpoints using real API keys/credentials in every environment.
3. **REAL CLOUD (Lambda):** Cross-Lambda invocations (specifically `odl-parser-lambda`) use real AWS Lambda via boto3 with dev AWS credentials holding `lambda:InvokeFunction`.
4. **PDF Parsing (Dev):** Uses direct Python import of `odl/main.py` (bypasses AWS Lambda and S3). The `odl/main.py` handler is modified to accept a `local_file_path` fallback.

## Provider Routing

`provider_client.py` reads `MODEL_PROVIDER` env var (dev|prod).
- dev: OpenRouter free-tier models + `floci` for infrastructure + real AWS Lambda.
- prod: Bedrock managed models + real RDS/ElastiCache + real AWS Lambda.

## Embedding Model — Dual Path Restored

Fast path: **Local BGE-small-en-v1.5 (ONNX)**
- Runs purely local inside the Query Lambda. Zero network calls. Faster p95 latency.

Full path & Ingestion: **API-based**
- Dev: nvidia/nemotron-3-embed-1b (OpenRouter).
- Prod: amazon.titan-embed-text-v2 (Bedrock).

`embed_service.py` contains TWO distinct code paths to handle this split. Local inference is ONLY allowed for the fast path query.

## Lambda Packaging Limits

The functional deployment limits are:
- Zip deployment: **<250MB unzipped**.
- Container image: **10GB limit** via ECR.
(50MB is merely the direct upload limit, not the execution limit).

Query Lambda defaults to Zip (bundling the ~130MB BGE-small weights + ONNX runtime). Ingestion Lambda defaults to Zip (pending a build-time size check). PDF Extraction Lambda uses a Container Image (JRE required).

## PDF Extraction Invocation Contract

`odl-parser-lambda` is invoked synchronously via boto3 from `pdf_adapter.py`. 
- Payload IN: S3 object reference to the PDF.
- Payload OUT: *Pending verification against live function.* (Inline JSON vs S3-reference).

## Query Complexity Score

Computed deterministically in `preprocess.py`. No LLM call. No ML model.  
Pure keyword pattern matching + entity counting.

```python
complexity_score = (
    min(entity_count, 3) * 0.25        # cap at 3 to avoid over-weighting
    + multi_entity_flag     * 0.20      # >1 named entity detected
    + temporal_flag         * 0.15      # "Q1", "between", "during", "since"
    + comparison_flag       * 0.15      # "vs", "compare", "difference", "higher"
    + negation_flag         * 0.10      # "not", "except", "without", "other than"
    + relationship_flag     * 0.15      # "cause", "affect", "depend", "because"
)
# Range: 0.0 – 1.0
```

## Retrieval Planner Rules

Evaluated top-to-bottom. First match wins. Hardcoded keyword lists. No ML classifier.

### Rule 1 — FAST PATH
```text
IF complexity_score < 0.30
AND entity_count <= 1
AND relationship_flag == False
AND temporal_flag == False
AND comparison_flag == False
→ fast_path = True, use_graph = False
  → Uses fast path (BM25 -> PageIndex -> Vector via Local BGE-small).
  → No LLM call. Return top-3 vector results with citations.
```

### Rule 2 — RELATIONSHIP PATH
```text
IF relationship_flag == True
→ fast_path = False, use_graph = True
→ stages = [BM25, PageIndex, Vector (API), OKF, Graph, Rerank, FidelityCheck, Compress, LLM]
```

### Rule 3 — ANALYTICAL PATH
```text
IF temporal_flag OR comparison_flag
AND relationship_flag == False
→ fast_path = False, use_graph = False
→ stages = [BM25, PageIndex, Vector (API), OKF, Rerank, FidelityCheck, Compress, LLM]
```

### Rule 4 — FULL PATH (default)
```text
ELSE (complex_score >= 0.30, no specific flag match)
→ fast_path = False, use_graph = False
→ stages = [BM25, PageIndex, Vector (API), OKF, Rerank, FidelityCheck, Compress, LLM]
```

## Graph Activation Decision

Graph is NOT a default pipeline stage. It activates ONLY under **Rule 2** (`relationship_flag` detected).

## OKF vs Graph — When to Use Which

### OKF Property Lookup (always runs in full path)
Use for: typed factual properties on known entities.

### Graph Traversal (only Rule 2)
Use for: multi-hop relationship reasoning.

## Graph Hard Limits (enforced as constants, not config values)

```python
MAX_GRAPH_HOPS  = 2
MAX_GRAPH_NODES = 40
MIN_EDGE_WEIGHT = 0.3  # prune below this before traversal
```

## Entity Normalization Thresholds

```text
cosine_sim >= 0.92  → auto-merge (same entity)
0.85 <= sim < 0.92  → merge, set low_confidence = True
sim < 0.85          → separate nodes, add to manual review queue
```

## Confidence Score (deterministic, post-LLM)

- `>= 0.75` → **HIGH**
- `0.50 – 0.74` → **MEDIUM**
- `< 0.50` → **LOW**

## LLM Prompt Contract (immutable in v1)

```text
Answer using ONLY the provided context fragments. For each factual claim in your answer, cite the source_id of the fragment it came from. If the context does not contain sufficient information to answer, respond exactly with: NOT_FOUND. Do not use prior knowledge. Do not infer beyond what is stated.
```

## NetworkX Decision
NetworkX: **rejected for production.** Implemented via dict-based adjacency list backed by Postgres `relations` table.
