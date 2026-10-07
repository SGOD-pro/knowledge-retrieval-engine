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
    "extraction_pipeline_version": "parse-v5"
  },
  "quality": {
    "status": "VALID",
    "confidence": 0.98
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
  "coverage": "chunks_1_620",
  "exclusions": [],
  "artifact_version": "bm25-v7",
  "pipeline_version": "index-v3",
  "failure_state": null
}
```

## 4. Query Contract & Requirement Mapping
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
**Requirement Mapping Contract:**
- `requirements` list decomposes user intent into discrete, verifiable sub-goals.
- In single-goal queries, a 1-element array directly maps the query intent.
- In multi-part or mixed queries, each element carries an independent `requirement_id`, target, selection predicates, and required evidence type. Each item routes independently and rejoins by `requirement_id` at the evidence join.

## 5. CSV/Excel Executor Contract

### Execution Tool Request
```json
{
  "workspace_id": "ws_1",
  "query_id": "q_42",
  "snapshot_id": "snap_42",
  "requirement_id": "r1",
  "dataset_ref": {
    "source_id": "sheet_1",
    "source_version": 3,
    "registered_file_id": "file_sales_v3",
    "sheet": "Sales",
    "schema_version": "schema-v3"
  },
  "operation": "aggregate",
  "filters": [
    {"column": "region", "operator": "eq", "value": "Europe"},
    {"column": "year", "operator": "eq", "value": 2024}
  ],
  "aggregation": {"operator": "sum", "column": "revenue"},
  "group_by": [],
  "value_policy": {
    "nulls": "error",
    "excel_formulas": "validated_cached_values",
    "currency": "USD_million"
  }
}
```

### Compact Execution Result
```json
{
  "workspace_id": "ws_1",
  "query_id": "q_42",
  "snapshot_id": "snap_42",
  "requirement_id": "r1",
  "execution_id": "exec_7",
  "evidence_id": "execution:ws_1:exec_7",
  "execution_record_ref": "execution:ws_1:exec_7",
  "source_id": "sheet_1",
  "source_version": 3,
  "schema_version": "schema-v3",
  "operation": "sum",
  "metric": "revenue",
  "selection": {"region": "Europe", "year": 2024},
  "selection_identity": {
    "records_examined": 5000,
    "records_matched": 2,
    "selection_digest": "sha256:...",
    "lineage_ref": "execution:ws_1:exec_7:lineage"
  },
  "result": {"value": 184.2, "unit": "USD_million"},
  "coverage_status": "COMPLETE_FOR_SELECTION",
  "qualifications": [],
  "status": "VERIFIED"
}
```

### Execution Policies & Error Semantics
- **Registered Dataset References:** Access only pre-registered immutable source files (`registered_file_id`, sheet, schema). User paths/URLs are never executed directly.
- **Restricted Formula AST:** Formulas use an allowlisted AST of basic arithmetic/aggregation operators and named source-backed column operands. Never execute arbitrary Python, `eval`, shell commands, or arbitrary SQL.
- **Empty and All-Null Selections:** Distinct from numeric zero. Count of empty selection is `0`. Sum/mean on empty or all-null selection returns `null` or explicit failure under `nulls: "error"`, never a fabricated zero.
- **Zero Denominators:** Division by zero is mathematically undefined. Percentage change with zero baseline is undefined unless an explicit domain policy applies; 0-to-0 is undefined.
- **Formula-Cache Handling:** Excel files evaluate against `validated_cached_values`. Stale or missing cached formulas require an approved recalculation engine or explicit failure; pandas is not an Excel formula evaluator. Never execute macros or external links.
- **Missing Operands:** Prohibit calculation completely. Missing operands never produce a partial calculation.
- **Completeness Checks:** Computations must evaluate over 100% of matching rows. Rejected or unreadable rows affecting selection prevent claiming complete aggregation. Exceeding scan/memory/time limits results in `INCOMPLETE_EXECUTION`, never silent sampling.
- **Durable Lineage:** Full provenance (all selected record IDs, operator AST, operand values/units, content hashes) is archived in durable storage (`lineage_ref`). Only compact references enter LLM context.

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
  "artifact_version": "bm25-v7",
  "locator": {"chunk_id": "ch_1", "page": 73},
  "retriever": "bm25",
  "rank": 1,
  "score": 12.42,
  "branch_status": "COMPLETE"
}
```

## 7. RRF
\[
RRF(d)=\sum_i\frac{1}{k+r_i(d)}
\]
Only compatible ranked discovery lists participate. Structured execution results are mandatory evidence and never participate in RRF.

## 8. Verification
Mechanical: identity/access/version/location/schema/units/operands/selection/citation.
Semantic: actual support, qualifiers, contradictions, causal meaning, final wording.

## 9. Terminal States
`COMPLETE`, `PARTIAL`, `NO_SUPPORT`, `RETRIEVAL_INCOMPLETE`, `CLARIFICATION_REQUIRED`, `FAILED`.

## 10. LAYA
Typed decision only; code validates and executes. Budget, deadline, candidate limits and no-progress limits apply.

## 11. Large Data & Operational Rules
- **Selective Prose Chunking & Embedding:** Only readable narrative prose blocks are chunked and embedded. Tabular datasets index schema/navigation text and route to structured execution.
- **No Document Embedding During Q&A:** Query embedding is restricted to the question or bounded query variants. No document chunking/embedding occurs at query time.
- **Embedding-Model Compatibility:** Different embedding models use separate collections or separate named-vector configurations within one collection. Compatibility encompasses model identity/version, dimensions, normalization, and distance metric—not dimension alone. A query vector is never compared against an incompatible model's vectors; compatible ranked candidate lists are fused via RRF, while raw vectors or similarity scores are never mixed across models.
- **Prepared Fallback Behavior:** If dense retrieval fails or is unavailable, use prepared baseline lexical/canonical fallbacks. Do not mark incomplete indexes as ready.
- **Source-Supported Graph Relations:** Graph relations require exact source locators. Cross-source edges retain all supporting source versions and invalidate if supporting sources change.
- **Exhaustive-List Coverage:** "List all X" requires deterministic cursor pagination and completeness verification, never a top-k similarity sample.
- **Whole-Document Summaries:** Bounded hierarchical merge over enumerated document structure with coverage retention. Partial summaries must not claim exhaustiveness.
- **Checkpoint / Resume Identity:** Ingestion jobs use `JobKey = H(workspace, source_id, source_version, content_hash, pipeline_version, operation_policy_version)`. Identical jobs resume from checkpoint rather than duplicating work.

