# KRE Technical Specification

## 1. Identity Envelope
Cross-module messages carry, where applicable:
```json
{
  "workspace_id": "ws_1",
  "query_id": "q_1",
  "source_id": "src_1",
  "source_version": 7,
  "snapshot_id": "snap_42",
  "requirement_id": "r1"
}
```

## 2. Canonical Evidence
```json
{
  "workspace_id": "ws_1",
  "evidence_id": "ev_1",
  "source_id": "doc_1",
  "source_version": 7,
  "type": "table_cell",
  "location": {
    "page": 12,
    "table_id": "t1",
    "row_id": "Europe",
    "column_id": "2024"
  },
  "content": 184.2,
  "unit": "USD_million",
  "qualifiers": {"region": "Europe", "year": 2024},
  "support_status": "ASSERTED",
  "provenance": {
    "content_hash": "sha256:...",
    "pipeline_version": "parse-v1"
  }
}
```

## 3. Capability Artifact
```json
{
  "workspace_id": "ws_1",
  "source_id": "doc_1",
  "source_version": 7,
  "artifact_id": "bm25_doc_1_v7",
  "capability": "text_search",
  "ready": true,
  "coverage": {},
  "exclusions": [],
  "artifact_version": "bm25-v7",
  "pipeline_version": "index-v3",
  "failure_state": null
}
```

## 4. Query Contract
```json
{
  "query_id": "q_1",
  "workspace_id": "ws_1",
  "snapshot_id": "snap_42",
  "requirements": [
    {
      "requirement_id": "r1",
      "intent": "aggregate",
      "operation": "sum",
      "target": {"dataset": "sales", "metric": "revenue"},
      "selection": {"region": "Europe", "year": 2024},
      "required_evidence": [{"kind": "structured_records", "locator": null}]
    }
  ],
  "answer_mode": "deterministic",
  "budget_policy_id": "query-default-v3"
}
```

## 5. Structured Execution
Uses registered immutable file/dataset references and restricted operations. Output contains selected IDs, predicates, operands, operation/formula, result/units, source/schema/backend/policy versions and durable lineage.

## 6. Discovery Candidate
```json
{
  "workspace_id": "ws_1",
  "query_id": "q_1",
  "snapshot_id": "snap_42",
  "requirement_ids": ["r2"],
  "candidate_id": "cand_1",
  "source_id": "doc_1",
  "source_version": 7,
  "locator": {"chunk_id": "ch_1", "page": 73},
  "retriever": "bm25",
  "rank": 1,
  "score": 12.42,
  "branch_status": "COMPLETE"
}
```

## 7. RRF
\[
RRF(d)=\sum_irac{1}{k+r_i(d)}
\]
Only compatible ranked discovery lists participate.

## 8. Verification
Mechanical: identity/access/version/location/schema/units/operands/selection/citation.
Semantic: actual support, qualifiers, contradictions, causal meaning, final wording.

## 9. Terminal States
`COMPLETE`, `PARTIAL`, `NO_SUPPORT`, `RETRIEVAL_INCOMPLETE`, `CLARIFICATION_REQUIRED`, `FAILED`.

## 10. LAYA
Typed decision only; code validates and executes. Budget, deadline, candidate limits and no-progress limits apply.

## 11. Large Data
No silent truncation. Complete selection semantics for calculations; pagination/streaming for large results.
