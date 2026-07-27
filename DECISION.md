# DECISION.md — Planner Rules and System Decisions

## Provider Routing (NEW)

provider_client.py reads MODEL_PROVIDER env var (dev|prod).
  dev:  OpenRouter free-tier models (see Model Provider Matrix)
  prod: Bedrock managed models (see Model Provider Matrix)

RULE: No retrieval or LLM module ever imports an OpenRouter or
Bedrock SDK directly. All calls route through providers/*.py.
This is what makes the free-tier deprecation risk containable —
if OpenRouter kills a free model, only provider config changes,
not application logic.

Retry/fallback behavior:
  If dev provider call fails (rate limit, deprecation, timeout):
    1. Retry once with exponential backoff (max 2s wait).
    2. On second failure: fall back to prod provider for THIS call
       only, log a warning with cost implication.
    3. Never silently fail a query — degrade to prod cost before
       returning an error to the user.

## Reranker Model Choice — documented compromise (NEW)

Dev: nvidia/llama-nemotron-rerank-vl-1b-v2 (free, OpenRouter)
  This is a vision-language reranker (Eagle VLM architecture,
  SigLIP2 + Llama 3.2 1B) built primarily for ViDoRe-style visual
  document retrieval (screenshots of pages with charts/tables).
  KRE uses it in TEXT-ONLY mode (query + text passage, no image
  input) — a supported but not primary use case for this model.
  Accepted because: free, and 10,240 token context is sufficient
  for reranking 40 candidate chunks at ~250 tokens each.

Prod: cohere.rerank-v3-5 (Bedrock)
  Purpose-built text cross-encoder, no VL mismatch, managed API,
  no deprecation risk under Bedrock's SLA.

If dev-mode reranker quality (measured via BENCHMARK.md Precision@3)
falls more than 5% below prod-mode reranker on the same test set:
- **Constraint:** Cohere Rerank 3.5 / NVIDIA API (API-based)
  Switch dev default to a Cohere Rerank 3.5 / NVIDIA API fallback for
  non-prod environments. (e.g., local Docker Compose dev
  loop), while keeping Lambda prod on Cohere Rerank. Document this
  as a known dev/prod parity gap, not a silent inconsistency.

## Single Embedding Model (UPDATED from rev 3's two-tier)

Both fast path and full path use the SAME embedding model:
  nvidia/nemotron-3-embed-1b (dev) / amazon.titan-embed-text-v2 (prod)

Fast path stays fast by SKIPPING stages (OKF, Graph, Reranker, LLM),
not by using a cheaper embedding model. This was the wrong lever
in rev 2/3 — dimensionality tradeoffs only matter for local inference
cost, and there is no local inference anymore.

Titan Embed v2 (Bedrock prod) dimension note: configurable at
256/512/1024. Use 1024 to stay close to Nemotron's 2048 in relative
semantic capacity without doubling storage cost. Document this as
a dev/prod embedding dimension mismatch — DO NOT mix dev-generated
and prod-generated embeddings in the same pgvector column. Ingestion
pipeline must tag which provider generated each embedding and the
query pipeline must use the matching provider for that corpus.

## OKF Extraction — Provider Update

Tier 3 dev: nvidia/nemotron-3-nano-30b-a3b (OpenRouter, free)
  MoE architecture, structured extraction task, same schema contract
  as previously specified for Nova Micro (rev 2/3).
Tier 3 prod: amazon.nova-micro-v1 (Bedrock), unchanged from rev 3.

Quality gate (unchanged): precision >= 85% on 10-doc validation set,
run separately for dev and prod models before either goes live.
Because these are two different models, run the gate TWICE —
passing on Nova Micro does not imply passing on Nemotron Nano.

## Lambda Cold Start Budget (NEW)

Lambda cold start adds ~200-800ms for Python runtime + dependency
import (psycopg2, requests, etc.) before the handler even begins.
This is a real cost not present in the always-on FastAPI deployment
assumed in rev 1-3.

Mitigation:
  - Provisioned concurrency for the query Lambda (keeps N warm
    instances ready) if traffic justifies the added cost.
  - Keep deployment package minimal (<50MB) — smaller package,
    faster cold start.
  - Do NOT import heavy unused libraries. Audit requirements.txt
    every phase — this is now a latency-relevant file, not just
    a dependency list.

Fast path p95 < 400ms target assumes WARM Lambda. Cold start p95
will exceed this — track cold start latency as a SEPARATE metric
in BENCHMARK.md, not blended into the main p95 figure.

---

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

---

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
  → Uses fast path (BM25 -> PageIndex -> Vector).
  → No LLM call. Return top-3 vector results with citations.
```
*Examples that route here:*
- "What is the refund policy?"
- "Where is the privacy notice?"
- "What are the supported file formats?"

### Rule 2 — RELATIONSHIP PATH
```text
IF relationship_flag == True
(keywords: "cause", "affect", "depend", "lead to", "because",
 "impact", "relation between", "why did", "result of", "due to")
→ fast_path = False, use_graph = True
→ stages = [BM25, PageIndex, Vector, OKF, Graph, Rerank, FidelityCheck, Compress, LLM]
```
*Examples:*
- "Why did revenue decrease in Q3?"
- "What caused the battery overheating issue?"
- "How does the refund rate affect margins?"

### Rule 3 — ANALYTICAL PATH
```text
IF temporal_flag OR comparison_flag
AND relationship_flag == False
→ fast_path = False, use_graph = False
→ stages = [BM25, PageIndex, Vector, OKF, Rerank, FidelityCheck, Compress, LLM]
```
*Note:* Graph skipped for analytical queries. Temporal/comparative queries need data, not edge traversal. OKF property lookup handles typed values (metrics, dates, rates).  
*Examples:*
- "Compare refund rates between Q1 and Q2."
- "What were the top complaints in the last quarter?"

### Rule 4 — FULL PATH (default)
```text
ELSE (complex_score >= 0.30, no specific flag match)
→ fast_path = False, use_graph = False
→ stages = [BM25, PageIndex, Vector, OKF, Rerank, FidelityCheck, Compress, LLM]
```
Graph is NOT default. It only activates on Rule 2.

---

## Graph Activation Decision

Graph is NOT a default pipeline stage. It activates ONLY under **Rule 2** (`relationship_flag` detected).

**Rationale:**
- *"What is the refund policy?"* → BM25 + reranker finds this.
- *"Why did revenue drop?"* → Graph traces `Revenue →[DEPENDS_ON]→ Orders`.

For factual lookups, BM25 + structural scoring is faster and equally accurate. The graph earns its complexity only when the query requires traversing entity relationships that aren't expressible as keywords.

---

## OKF vs Graph — When to Use Which

### OKF Property Lookup (always runs in full path)
Use for: typed factual properties on known entities.
- *"What is the failure rate of Battery Model X?"*
- `OKF: Battery_Model_X.failure_rate = "12%"` → retrieve source chunk.
- *Cost:* single dict lookup. Always worth it.

### Graph Traversal (only Rule 2)
Use for: multi-hop relationship reasoning.
- *"What causes high return rates?"*
- `Graph: Returns →[CAUSED_BY]→ Overheating →[CAUSED_BY]→ Battery_Defect`
- *Cost:* adjacency dict traversal. Max 2 hops. Worth it only for causal queries.

---

## Graph Hard Limits (enforced as constants, not config values)

```python
MAX_GRAPH_HOPS  = 2
MAX_GRAPH_NODES = 40
MIN_EDGE_WEIGHT = 0.3  # prune below this before traversal
```
If traversal hits `MAX_GRAPH_NODES` at hop 1: stop, do not expand hop 2.  
Violation of these limits is a test failure, not a warning.

---

## Entity Normalization Thresholds

```text
cosine_sim >= 0.92  → auto-merge (same entity)
0.85 <= sim < 0.92  → merge, set low_confidence = True
sim < 0.85          → separate nodes, add to manual review queue
```

Manual review queue: PostgreSQL table `flagged_normalizations`.  
Reviewed weekly. Not a blocking step for ingestion.

---

## Confidence Score (deterministic, post-LLM)

**Confidence Score Definition:**
- `reranker_score_avg`: mean API reranker score across top-6 chunks.
- `coverage_ratio`: (query entities in context) / (total query entities).

### Bands:
- `>= 0.75` → **HIGH** (green badge)
- `0.50 – 0.74` → **MEDIUM** (yellow badge)
- `< 0.50` → **LOW** (amber border, retrieval trace auto-expanded in UI)

LLM is NEVER asked to rate its own confidence. LLM output schema has no confidence field. Parser rejects it if present.

---

## LLM Prompt Contract (immutable in v1)

### System Prompt
```text
Answer using ONLY the provided context fragments. For each factual claim in your answer, cite the source_id of the fragment it came from. If the context does not contain sufficient information to answer, respond exactly with: NOT_FOUND. Do not use prior knowledge. Do not infer beyond what is stated.
```

### Context Format
```text
[source_id: {chunk_id}] {compressed_chunk_text}
```

### Required Output Format (parsed programmatically)
```json
{
  "answer": "...",
  "citations": ["{chunk_id}", "..."]
}
```

- **Max input context:** 1200 tokens (hard limit enforced before call).
- **Max output:** 512 tokens.
- **Temperature:** 0 (deterministic, repeatable).
- If LLM returns `NOT_FOUND`: do not cache. Return to user with `confidence = LOW` and `retrieval_trace` exposed.

---

## NetworkX Decision

NetworkX: **rejected for production.**  
*Reason:* grows to gigabytes on large corpora, not serializable efficiently, adds a heavy dependency.

### v1 Implementation: Dict-based Adjacency List
```python
graph: Dict[str, List[Relation]]
# where Relation = dataclass(to_id, relation_type, weight, source_chunk_id)
```

- **Persisted as:** PostgreSQL `relations` table.
- **Loaded at startup:** single `SELECT` into memory dict.
- **Refresh:** on re-ingestion of any document.
- **Max nodes:** 5000. **Max edges:** 50000. Enforced at build time. Above limits: log warning, partition by document cluster in v2.

