# DECISION.md — Planner Rules and System Decisions

## Dev Environment Split

Dev environment is NOT fully local or fully cloud. It is a strict split:
1. **LOCAL SERVICES (dev only):** Redis runs locally. QdrantDB runs locally (or Qdrant Cloud with dev API key). DynamoDB uses AWS dev credentials.
2. **REAL CLOUD (Bedrock):** All LLM, embedding, and reranker calls hit real Bedrock/NVIDIA NIM endpoints using real API keys/credentials in every environment.
3. **LOCAL LAMBDAS (dev):** `odl-parser-lambda` runs locally via direct Python import of `odl/main.py`. `bge_microservice` runs locally as a FastAPI app.
4. **CORS:** Dev allows `http://localhost:5173`. Prod allows ONLY the configured frontend URL — no localhost, no wildcard `*`.

## Provider Routing

`provider_client.py` reads `MODEL_PROVIDER` env var (dev|prod).
- dev: Bedrock models + local Redis + QdrantDB + DynamoDB (dev credentials) + local BGE-small microservice.
- prod: Bedrock models + ElastiCache + Qdrant Cloud + DynamoDB + BGE-small microservice (deployed).

## Embedding Model — Dual Path

Fast path: **BGE-small-en-v1.5 (ONNX Microservice)**
- Runs as a standalone FastAPI microservice. Zero Bedrock network calls. Fastest p95 latency.
- Query Lambda calls the microservice HTTP endpoint — does NOT bundle the ONNX weights.

Full path & Ingestion: **amazon.titan-embed-text-v2 (Bedrock)**
- 1024-dim vectors via Bedrock API.

`embedding_provider.py` contains TWO distinct code paths to handle this split. Microservice inference is ONLY allowed for the fast path query.

## BGE-Small Microservice

- Deployed as a separate service (can be Lambda, ECS, or local FastAPI).
- Exposes `/embed` endpoint accepting `{"text": "..."}` and returning `{"embedding": [...]}`.
- Model file: `model.onnx` (BGE-small-en-v1.5 exported to ONNX format).
- User provides the model file. Do NOT download or generate it.

## Lambda Packaging Limits

The functional deployment limits are:
- Zip deployment: **<250MB unzipped**.
- Container image: **10GB limit** via ECR.

Query Lambda uses Zip (NO BGE-small weights — calls microservice). Ingestion Lambda uses Zip. PDF Extraction Lambda uses Container Image (JRE required). BGE-small microservice is standalone.

## PDF Extraction Invocation Contract

`odl-parser-lambda` is invoked synchronously via boto3 from `pdf_adapter.py`. 
- Payload IN: S3 object reference to the PDF.
- Payload OUT: Parsed chunks as JSON.
- **Dev:** Direct Python import of `odl/main.py` with `{"local_file_path": "..."}`.
- **Prod:** `boto3.client('lambda').invoke(FunctionName='odl-parser-lambda', ...)`.

## Query Complexity Score

Computed deterministically in `planner.py`. No LLM call. No ML model.  
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
  → Uses fast path (BM25 -> PageIndex -> Vector via BGE-small microservice).
  → No LLM call. Return top-3 vector results with citations.
```

### Rule 2 — RELATIONSHIP PATH
```text
IF relationship_flag == True
→ fast_path = False, use_graph = True
→ stages = [BM25, PageIndex, Vector (Titan API), OKF, Graph, Rerank, FidelityCheck, Compress, LLM]
```

### Rule 3 — ANALYTICAL PATH
```text
IF temporal_flag OR comparison_flag
AND relationship_flag == False
→ fast_path = False, use_graph = False
→ stages = [BM25, PageIndex, Vector (Titan API), OKF, Rerank, FidelityCheck, Compress, LLM]
```

### Rule 4 — FULL PATH (default)
```text
ELSE (complex_score >= 0.30, no specific flag match)
→ fast_path = False, use_graph = False
→ stages = [BM25, PageIndex, Vector (Titan API), OKF, Rerank, FidelityCheck, Compress, LLM]
```

## Graph Activation Decision

Graph is NOT a default pipeline stage. It activates ONLY under **Rule 2** (`relationship_flag` detected).

## OKF vs Graph — When to Use Which

### OKF Property Lookup (always runs in full path)
Use for: typed factual properties on known entities. Zero LLM calls — pure DynamoDB lookup.
Examples: "What is the revenue for Q3?", "Which location had the most orders?" (CSV/Excel data).

### Graph Traversal (only Rule 2)
Use for: multi-hop relationship reasoning across the OKF knowledge graph.
Graph traversal executes against DynamoDB adjacency entries.

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

Formulas:
- Fast path: `confidence = (vector_similarity_avg * 0.6) + (coverage_ratio * 0.4)`
- Full path: `confidence = (reranker_score_avg * 0.6) + (coverage_ratio * 0.4)`

Bands:
- `>= 0.75` → **HIGH**
- `0.50 – 0.74` → **MEDIUM**
- `< 0.50` → **LOW**

## LLM Prompt Contract (immutable in v1)

```text
Answer using ONLY the provided context fragments. For each factual claim in your answer, cite the source_id of the fragment it came from. If the context does not contain sufficient information to answer, respond exactly with: NOT_FOUND. Do not use prior knowledge. Do not infer beyond what is stated.
```

## NetworkX Decision
NetworkX: **rejected for production.** Implemented via dict-based adjacency list backed by DynamoDB.

## Storage Decision
PostgreSQL/pgvector: **REMOVED.** All storage migrated to DynamoDB (metadata, OKF) + QdrantDB (vectors) + Redis (cache).
