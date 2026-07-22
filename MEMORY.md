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

## FAISS Index
- **Built:** offline, during ingestion.
- **Loaded:** at service startup into memory.
- **NOT rebuilt per query.**
- **NOT rebuilt on single document updates** (incremental add).
- **Full rebuild:** only on model change or corpus exceeding 10k pages.

---

## PageIndex
- **Storage:** PostgreSQL (persistent).
- **Load pattern:** queried per-request, indexed on `doc_id + keyword_hash`.
- **Update:** on re-ingestion, `DELETE` old entries for `doc_id`, `INSERT` new.

---

## OKF Knowledge Layer
- **Concepts + Properties + Relations:** PostgreSQL (persistent).
- **In-memory:** adjacency dict loaded at startup for graph traversal.
- **Refresh:** on any re-ingestion, reload affected concept subgraph. Do not reload full graph on every query.

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

