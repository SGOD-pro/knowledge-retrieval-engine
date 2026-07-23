# MEMORY.md — State and Caching Strategy

## Redis Cache

```yaml
Key:   sha256(normalize(query) + doc_scope_hash)
Value: {answer, citations, retrieval_path, confidence, latency_breakdown}
TTL:   24 hours
Write: only when confidence >= MEDIUM (>= 0.50)
Read:  checked before any retrieval stage runs
```

### Do NOT Cache:
- Responses with `confidence < 0.50` (LOW)
- Responses where LLM returned `NOT_FOUND`
- Responses with `coverage_ratio < 0.6`

### Invalidation:
- On re-ingestion of document `D`: `DELETE` all cache keys where `doc_scope_hash` includes `D`.
- Implemented via: store `doc_ids` in cache value metadata, scan on re-ingestion. Not a full cache flush.

---

## Semantic Cache — pgvector-backed (replaces FAISS cache_key_index)

Table: cache_entries
  query_embedding  vector(2048)  -- or 1024 if prod/Titan
  redis_key        text
  doc_scope_hash   text
  provider         text   -- which embedding provider generated this,
                           -- prevents dev/prod embedding mixing
  created_at       timestamptz

Lookup query:
  SELECT redis_key, query_embedding <=> %(query_vec)s AS distance
  FROM cache_entries
  WHERE doc_scope_hash = %(scope)s
    AND provider = %(current_provider)s
  ORDER BY distance ASC LIMIT 1;

  Threshold: distance corresponding to cosine_sim >= 0.95 (unchanged
  logic from rev 3, now a WHERE/ORDER BY instead of FAISS search).

HNSW index on cache_entries.query_embedding for fast lookup at scale
(matters once cache_entries exceeds a few thousand rows).

Invalidation: unchanged logic (purge on re-ingestion by doc_scope),
now a DELETE statement instead of a FAISS index rebuild.

---

## Vector Store — pgvector (replaces "FAISS Index" section entirely)

All chunk embeddings: single `chunks.embedding vector(N)` column.
  N = 2048 (Nemotron, dev) or 1024 (Titan, prod) — see DECISION.md
  provider-mismatch warning. Two Postgres columns if both providers
  are ever live simultaneously (e.g. during a migration):
  `embedding_dev vector(2048)`, `embedding_prod vector(1024)`.

HNSW index built on ingestion, incrementally maintained by Postgres
as new chunks are inserted — no manual rebuild step required, unlike
FAISS's load-and-rebuild pattern.

---

## PageIndex
- **Storage:** PostgreSQL (persistent).
- **Load pattern:** queried per-request, indexed on `doc_id + keyword_hash`.
- **Update:** on re-ingestion, `DELETE` old entries for `doc_id`, `INSERT` new.

---

## OKF Graph — Postgres only (replaces "in-memory adjacency dict")

No startup load step exists in this architecture. `relations` table
is queried directly via recursive CTE per request (see ARCHITECTURE.md).
Add an index on `relations(from_concept_id, relation_weight)` to keep
the recursive CTE fast at the 5000-node / 50000-edge ceiling.

---

## Redis — ElastiCache Serverless (deployment detail, logic unchanged)

Same TTL, same write-guard conditions as rev 3. Only the hosting
changes: ElastiCache Serverless instead of a self-managed Redis,
to match the fully-managed, scale-to-Lambda-traffic deployment model.

---

## What Is Not Cached
- Retrieval plans (fast to compute, query-specific).
- Reranker scores (model-dependent, invalidated on model change).
- LOW confidence responses.
- `NOT_FOUND` responses.

---

## Feedback Storage (write now, act on it in v2)

### Schema:
```sql
CREATE TABLE feedback (
  id UUID PRIMARY KEY,
  query_hash TEXT,
  response_id UUID,
  rating SMALLINT CHECK (rating IN (-1, 1)),
  timestamp TIMESTAMPTZ DEFAULT NOW()
);
```

- **v1:** Collect only. Do not wire to reranking weights.
- **v2 trigger:** When feedback table reaches 500+ rated responses, run offline analysis:
  - Which retrieval paths (fast/graph/analytical/full) correlate with positive ratings?
  - Which OKF concept types appear in highly-rated answers?
  - Which queries consistently route to wrong planner rule?
- Use findings to update planner keyword lists and confidence thresholds. Validate offline before touching production routing.

### Why Not Live Feedback-to-Weights in v1:
- Live feedback → retrieval weights = popularity bias.
- New documents get disadvantaged (cold start).
- Regressions are invisible until benchmark run.
- Collect data first. Tune offline. Ship validated changes only.

---

## No Persistent User State in v1
- No session store.
- No conversation history.
- No user preference store.
- No query logging to persistent storage (privacy default).

*Reason:* v1 targets regulated industry deployment. Data retention is a compliance question, not a feature decision.



