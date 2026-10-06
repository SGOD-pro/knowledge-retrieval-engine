# KRE API Contract

## Base Path
`/api/v1`

## Authentication & Authorization
Protected routes require Bearer tokens or workspace session identity. Every operation validates principal workspace tenancy. Deletion or access revocation acts as an immediate barrier even for in-flight requests.

## 1. Workspace Endpoints
- `GET /workspaces` — List workspaces accessible to principal.
- `POST /workspaces` — Create workspace (`{"name": "Finance Q3"}`).

## 2. Document Lifecycle & Upload Readiness
- `POST /workspaces/{workspace_id}/documents` — Ingest source file (`multipart/form-data`).
  - **Response (201 Created):**
    ```json
    {
      "workspace_id": "ws_1",
      "source_id": "doc_1",
      "source_version": 1,
      "manifest_generation": 4,
      "filename": "q3_report.pdf",
      "content_hash": "sha256:...",
      "baseline_status": "READY",
      "enrichment_status": "PENDING",
      "enrichment_job_id": "job_enrich_123"
    }
    ```
  - *Baseline Independence:* Baseline retrieval is `READY` immediately after baseline indexing. Optional enrichment runs asynchronously under `job_enrich_123`.
- `GET /workspaces/{workspace_id}/documents` — List active sources with active version, baseline status, and enrichment status.
- `GET /workspaces/{workspace_id}/jobs/{job_id}` — Poll background enrichment or re-indexing status (`RUNNING`, `COMPLETE`, `FAILED`, `CANCELLED`).
- `DELETE /workspaces/{workspace_id}/documents/{source_id}` — Deletes source.
  - **Behavior:** Writes an immediate atomic tombstone, increments `manifest_generation`, revokes cached answers, and cancels running background jobs.
  - **Response (200 OK):**
    ```json
    {
      "source_id": "doc_1",
      "status": "TOMBSTONED",
      "manifest_generation": 5
    }
    ```

## 3. Persistent Sessions
- `POST /workspaces/{workspace_id}/sessions` — Initialize chat session.
  - **Request:** `{"selected_document_ids": ["doc_1", "sheet_1"]}`
  - **Response (201 Created):**
    ```json
    {
      "session_id": "sess_42",
      "workspace_id": "ws_1",
      "selected_document_ids": ["doc_1", "sheet_1"],
      "created_at": "2026-10-07T00:00:00Z"
    }
    ```
- `GET /workspaces/{workspace_id}/sessions/{session_id}` — Retrieve conversation history, session variables, and context.

## 4. Query Contract (`POST /query`)

### Request Payload
```json
{
  "workspace_id": "ws_1",
  "session_id": "sess_42",
  "query": "What was Europe revenue in 2024?",
  "document_ids": ["sheet_1"],
  "budget_policy_id": "query-default-v3"
}
```

### Response Payload: Complete Success (`status: "COMPLETE"`)
```json
{
  "query_id": "q_1",
  "snapshot_id": "snap_42",
  "status": "COMPLETE",
  "answer": "Europe revenue in 2024 was 184.2 million USD.",
  "requirements": [
    {
      "requirement_id": "r1",
      "status": "SUPPORTED",
      "intent": "aggregate",
      "operation": "sum",
      "supporting_evidence_ids": ["ev_1"]
    }
  ],
  "claims": [
    {
      "claim_id": "c1",
      "text": "Europe revenue in 2024 was 184.2 million USD.",
      "support": ["ev_1"],
      "status": "SUPPORTED"
    }
  ],
  "citations": [
    {
      "citation_id": "cit_1",
      "source_id": "sheet_1",
      "source_version": 3,
      "type": "aggregate_execution",
      "location": {
        "sheet": "Sales",
        "row_filter": "region=Europe AND year=2024",
        "column": "revenue"
      },
      "execution_record_ref": "execution:ws_1:exec_7",
      "text_snippet": "Sum of revenue for Europe (2024)"
    }
  ],
  "usage": {
    "llm_generation_calls": 0,
    "encoder_rerank_calls": 0,
    "embed_calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "tool_attempts": 1
  },
  "timings": {
    "total_ms": 142.5,
    "planning_ms": 12.0,
    "execution_ms": 110.2,
    "verification_ms": 20.3
  }
}
```

### Response Payload: Supported Partial (`status: "PARTIAL"`)
Used when some requirements are independently verified while others timed out, failed, or lacked data:
```json
{
  "query_id": "q_2",
  "snapshot_id": "snap_42",
  "status": "PARTIAL",
  "answer": "Europe revenue in 2024 was 184.2 million USD. Information regarding 2025 forecasts was not found.",
  "requirements": [
    {
      "requirement_id": "r1",
      "status": "SUPPORTED",
      "supporting_evidence_ids": ["ev_1"]
    },
    {
      "requirement_id": "r2",
      "status": "NO_SUPPORT",
      "reason": "No forecast records present in searched dataset."
    }
  ],
  "claims": [
    {
      "claim_id": "c1",
      "text": "Europe revenue in 2024 was 184.2 million USD.",
      "support": ["ev_1"],
      "status": "SUPPORTED"
    }
  ],
  "citations": [
    {
      "citation_id": "cit_1",
      "source_id": "sheet_1",
      "source_version": 3,
      "type": "aggregate_execution",
      "location": {"sheet": "Sales"},
      "execution_record_ref": "execution:ws_1:exec_7"
    }
  ],
  "usage": {"llm_generation_calls": 1, "tool_attempts": 1},
  "timings": {"total_ms": 820.0}
}
```

### Response Payload: Clarification Required (`status: "CLARIFICATION_REQUIRED"`)
Returned when ambiguity cannot be resolved safely:
```json
{
  "query_id": "q_3",
  "status": "CLARIFICATION_REQUIRED",
  "clarification_prompt": "Did you mean gross revenue or operating profit for Europe in 2024?",
  "ambiguous_requirement_id": "r1",
  "options": ["gross_revenue", "operating_profit"]
}
```

### Response Payload: Scoped Insufficiency (`status: "NO_SUPPORT"` or `"RETRIEVAL_INCOMPLETE"`)
- `NO_SUPPORT`: Search completed over target scope without locating required supporting facts.
- `RETRIEVAL_INCOMPLETE`: Execution timed out, exceeded deadline, or hit unavailable dependency.
```json
{
  "query_id": "q_4",
  "snapshot_id": "snap_42",
  "status": "RETRIEVAL_INCOMPLETE",
  "answer": null,
  "error": {
    "code": "DEADLINE_EXCEEDED",
    "message": "Required discovery branch timed out before completing coverage."
  },
  "requirements": [
    {
      "requirement_id": "r1",
      "status": "TIMED_OUT"
    }
  ]
}
```

