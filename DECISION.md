# DECISION.md — Planner Rules and System Decisions

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
→ stages = [BM25, PageIndex, Vector]
→ No LLM call. Return top-3 BGE results with citations.
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

```python
confidence = (reranker_score_avg * 0.6) + (coverage_ratio * 0.4)
```

- `reranker_score_avg`: mean `bge-reranker-base` score across top-6 chunks.
- `coverage_ratio`: `query_entities_in_context / total_query_entities`.

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

