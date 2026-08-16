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
## Hybrid Architecture Decision (FastAPI Engine + Auxiliary Lambdas)

The system adopts a **Hybrid Architecture**:
1. **Core Engine:** The query pipeline, LangGraph state machine, and document ingestion are consolidated into a unified FastAPI application (`backend/src/main.py`), with an AWS Lambda adapter (`backend/src/lambda.py` via Mangum). This eliminates network latency between retrieval stages while supporting both standalone container and serverless deployments.
2. **Auxiliary Lambdas:** Heavy external runtimes (Java JRE for `odl-parser-lambda`, and isolated BGE ONNX embedding in `bge_microservice`) remain separate deployables.
3. **Resilient Local Fallback:** When auxiliary Lambdas are unprovisioned, `embed_service.py` falls back gracefully to in-process ONNX execution (`bge-onnx/model.onnx`).

## Embedding Model — Dual Path

Fast path: **BGE-small-en-v1.5 (Lambda / Local ONNX Fallback)**
- 384-dim normalized vectors.
- Prod: invokes `bge-small-en-v1-5-lambda-prod` via boto3 (falling back to local ONNX if unprovisioned).
- Dev/Test: runs local ONNX or deterministic vector.

Full path & Ingestion: **amazon.titan-embed-text-v2 (Bedrock)**
- 1024-dim vectors via Bedrock API.

`embed_service.py` and `vector_retriever.py` maintain strict schema-level isolation (Rule 30): fast-path only searches `embedding_fast`, full-path only searches `embedding_full`.

## BGE-Small Microservice / Lambda

- Deployed as a dedicated AWS Lambda function (`bge_microservice/main.py:lambda_handler`).
- Model files: `bge-onnx/` (`model.onnx`, `tokenizer.json`).
- Core backend delegates embedding to this Lambda or loads the ONNX weights locally as a fallback.

## Lambda Packaging Limits

The functional deployment limits are:
- Zip deployment: **<250MB unzipped** (FastAPI Core Engine via Mangum).
- Container image: **10GB limit** via ECR (`odl-parser-lambda` bundling Java JRE).

## PDF Extraction Invocation Contract

`odl-parser-lambda` is invoked synchronously via boto3 from `pdf_adapter.py`. 
- Payload IN: S3 object reference to the PDF.
- Payload OUT: Parsed chunks as JSON.
- **Dev:** Direct Python import of `odl/main.py` with `{"local_file_path": "..."}` or local parser adapter.
- **Prod:** `boto3.client('lambda').invoke(FunctionName='odl-parser-lambda-prod', ...)`.

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
- Fast path: `confidence = vector_similarity_avg`
- Full path: `confidence = reranker_score_avg`

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

## OKF Architecture & Spec Scope Decision (DynamoDB-Only)

KRE's Open Knowledge Framework (OKF) implementation is **DynamoDB-only**. It is inspired by Google's Open Knowledge Format vocabulary (concepts, properties, typed relations) but **does NOT implement** the file-based markdown bundle, YAML frontmatter schema, or `index.md` manifest from either v0.1 or v0.2 of the Google OKF specification.

This is a **deliberate architectural scope decision**, not an oversight or partial implementation.

### Accepted Tradeoffs
1. **Disaster Recovery:** No point-in-time disaster recovery of the knowledge graph without re-running Bedrock Nova Micro extraction over the raw corpus.
2. **File Export / Human Inspection:** No human-inspectable filesystem bundle export (`.okf/` folder hierarchy or `.md` concept files).
3. **Governance Metadata:** No governance metadata (such as `verified`, `status`, `stale_after`, `confidence`, or formal hierarchical type taxonomy) is currently persisted or tracked in DynamoDB.

### Scoped Future Roadmap
File-based markdown bundle serialization and governance schemas are categorized as scoped future enhancements if disaster recovery, offline audits, or human-curated knowledge editing become product priorities in later versions — not as a currently broken or missing component of the v1 system.

