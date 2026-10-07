# KRE API Contract

## Base Path
`/api/v1`

## Authentication & Authorization
- **Authentication Authority:** Protected endpoints require a valid bearer credential (`Authorization: Bearer <token>`) or an authenticated server session backed by a defined authentication mechanism (e.g. OAuth2/JWT or cloud IAM).
- **Context vs. Authority:** `session_id`, `workspace_id`, and `document_id` / `source_id` represent resource scoping and conversation context only, never authentication authority. Supplying these identifiers grants zero authorization.
- **Principal Scope Validation:** Every operation verifies that the authenticated principal:
  1. Possesses active tenant membership and access rights to `{workspace_id}`.
  2. Owns or is authorized to access the specified `{session_id}` within that workspace.
  3. Holds access permissions for all selected documents, specific file versions, background `{job_id}` tasks, and execution lineage records.
- **Mandatory Final Delivery & Tombstone Barriers:** Authorization and tombstone states are re-checked at final answer delivery and raw file access. If a document is deleted/revoked or principal authorization is withdrawn, delivery fails immediately (returning `401 Unauthorized`, `403 Forbidden`, `404 Not Found`, or `410 Gone`), even if historical snapshots are pinned.

## Entity Naming & Translation: `source_id` vs. `document_id`
- **Canonical Identity:** `source_id` is the canonical internal entity identifier for a registered document across storage, evidence records, citations, and execution lineage.
- **API Alias Translation:** `document_id` is supported as an external API alias in request routes, query parameters, and JSON payloads. The API layer translates `document_id` 1:1 to `source_id` upon ingestion. All responses provide canonical `source_id` (and may echo `document_id` for client convenience).

## 1. Workspace Endpoints
- `GET /workspaces` — List workspaces accessible to the authenticated principal.
- `POST /workspaces` — Create workspace (`{"name": "Finance Q3"}`).

## 2. Document Lifecycle & Upload Readiness

### Ingest Source File
- `POST /workspaces/{workspace_id}/documents` (`multipart/form-data`)
  - **Synchronous Completion (Small File / Immediate Baseline Indexing):**
    - **Status:** `201 Created`
    - **Response:**
      ```json
      {
        "workspace_id": "ws_1",
        "source_id": "doc_1",
        "document_id": "doc_1",
        "source_version": 1,
        "manifest_generation": 4,
        "filename": "q3_report.pdf",
        "content_hash": "sha256:...",
        "baseline_status": "READY",
        "enrichment_status": "PENDING",
        "enrichment_job_id": "job_enrich_123"
      }
      ```
    - *Baseline Independence:* Baseline retrieval is `READY` immediately. Optional enrichment executes asynchronously under `job_enrich_123`.
  - **Asynchronous Acceptance (Large File / Deferred Baseline Indexing):**
    - **Status:** `202 Accepted`
    - **Response:**
      ```json
      {
        "workspace_id": "ws_1",
        "source_id": "doc_2",
        "document_id": "doc_2",
        "source_version": 1,
        "manifest_generation": 4,
        "filename": "annual_filings_2024.pdf",
        "content_hash": "sha256:...",
        "baseline_status": "PENDING",
        "baseline_job_id": "job_base_456",
        "enrichment_status": "PENDING",
        "enrichment_job_id": null
      }
      ```
    - Baseline retrieval becomes queryable only after `job_base_456` completes baseline QA and CAS publication.

### Poll Job Status & Failure/Partial-Coverage Reporting
- `GET /workspaces/{workspace_id}/jobs/{job_id}`
  - **Readiness States:** `PENDING`, `RUNNING`, `COMPLETE`, `PARTIAL_COVERAGE`, `FAILED`, `CANCELLED`.
  - **Response (Partial Coverage / Partial Failure):**
    ```json
    {
      "job_id": "job_base_456",
      "workspace_id": "ws_1",
      "source_id": "doc_2",
      "source_version": 1,
      "job_type": "baseline_indexing",
      "status": "PARTIAL_COVERAGE",
      "progress": {
        "stages_completed": ["text_extraction", "chunking"],
        "stages_pending": ["vector_indexing"],
        "pages_examined": 100,
        "pages_indexed": 85,
        "pages_failed": 15
      },
      "coverage": {
        "status": "PARTIAL",
        "indexed_ranges": ["pages_1_85"],
        "excluded_ranges": ["pages_86_100"]
      },
      "errors": [
        {
          "code": "CORRUPT_EMBEDDED_IMAGE",
          "message": "Pages 86-100 contained unreadable visual content; baseline text indexed for pages 1-85 only.",
          "page_range": [86, 100]
        }
      ]
    }
    ```

### Document Replacement (New Immutable Version)
- `POST /workspaces/{workspace_id}/documents/{source_id}/versions` (`multipart/form-data`)
  - **Behavior:** Ingests a new file version (e.g. v8) while preserving the existing version (v7) as active and queryable. Baseline indexing for v8 builds in staging. When v8 passes baseline QA, atomic CAS activates v8 and increments `manifest_generation`.
  - **Status:** `202 Accepted`
  - **Response:**
    ```json
    {
      "workspace_id": "ws_1",
      "source_id": "doc_1",
      "target_source_version": 8,
      "active_source_version": 7,
      "manifest_generation": 4,
      "baseline_status": "STAGING",
      "baseline_job_id": "job_base_789",
      "message": "Version 8 staged. Version 7 remains active and queryable until version 8 passes baseline QA and CAS publication."
    }
    ```

### Authorized Version-Aware Source File Fetch
- `GET /workspaces/{workspace_id}/documents/{source_id}/file?version={source_version}`
  - **Query Parameters:** `version` (optional integer; defaults to currently active source version).
  - **Behavior:** Streams the raw immutable source file with appropriate MIME headers for viewing, citation inspection, or bounding box verification.
  - **Access & Deletion Barrier:** If the document has been tombstoned/deleted or the principal's access revoked, the endpoint immediately returns `404 Not Found` (or `410 Gone` / `403 Forbidden`), even if a historical version was requested.

### List Documents & Delete Source
- `GET /workspaces/{workspace_id}/documents` — List active sources with active version, baseline status, and enrichment status.
- `DELETE /workspaces/{workspace_id}/documents/{source_id}` — Deletes source.
  - **Behavior:** Atomically sets `tombstoned_at`, increments `manifest_generation`, revokes cached answers, cancels in-flight jobs, and immediately blocks query delivery and file access.
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

### Example A: Exact Deterministic Calculation (`status: "COMPLETE"`)
- **Generation Path:** **0 generation calls** (bound exact AST calculation + approved deterministic template). Generative synthesis and generative verification are not reserved. Citations and calculations are mechanically verified.

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
      "supporting_evidence_ids": ["execution:ws_1:exec_7"]
    }
  ],
  "claims": [
    {
      "claim_id": "c1",
      "text": "Europe revenue in 2024 was 184.2 million USD.",
      "support": ["execution:ws_1:exec_7"],
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

### Example B: Planning + Deterministic Partial Response (`status: "PARTIAL"`)
- **Generation Path:** **1 generation call** (validated planning proposal + deterministic execution and template). One generation call was consumed during planning/formulation; the answer wording combines deterministic calculation with an approved template, consuming zero synthesis generation calls.

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
      "supporting_evidence_ids": ["execution:ws_1:exec_7"]
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
      "support": ["execution:ws_1:exec_7"],
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
      "execution_record_ref": "execution:ws_1:exec_7"
    }
  ],
  "usage": {
    "llm_generation_calls": 1,
    "encoder_rerank_calls": 0,
    "embed_calls": 0,
    "tool_attempts": 1
  },
  "timings": {
    "total_ms": 820.0
  }
}
```

### Example C: Narrative Synthesis & Verification (`status: "COMPLETE"`)
- **Generation Path:** **2 generation calls** (synthesis + final semantic verification). 1 generation call synthesized the narrative from retrieved evidence; 1 generation call performed final semantic verification against cited source evidence `ev_1`. Both slots were reserved prior to optional work.

```json
{
  "query_id": "q_3",
  "snapshot_id": "snap_42",
  "status": "COMPLETE",
  "answer": "For Q3 2024, the European division reported 184.2 million USD in revenue, meeting operating targets.",
  "requirements": [
    {
      "requirement_id": "r1",
      "status": "SUPPORTED",
      "supporting_evidence_ids": ["ev_1"]
    }
  ],
  "claims": [
    {
      "claim_id": "c1",
      "text": "The European division reported 184.2 million USD in revenue for Q3 2024.",
      "support": ["ev_1"],
      "status": "SUPPORTED"
    }
  ],
  "citations": [
    {
      "citation_id": "cit_1",
      "source_id": "doc_1",
      "source_version": 7,
      "type": "table_cell",
      "location": {
        "page": 12,
        "table_id": "t1"
      },
      "text_snippet": "Europe 2024: 184.2 USD_million"
    }
  ],
  "usage": {
    "llm_generation_calls": 2,
    "encoder_rerank_calls": 1,
    "embed_calls": 1,
    "tool_attempts": 1
  },
  "timings": {
    "total_ms": 1450.0
  }
}
```

### Clarification Required (`status: "CLARIFICATION_REQUIRED"`)
Returned when material ambiguity cannot be resolved safely:
```json
{
  "query_id": "q_4",
  "status": "CLARIFICATION_REQUIRED",
  "clarification_prompt": "Did you mean gross revenue or operating profit for Europe in 2024?",
  "ambiguous_requirement_id": "r1",
  "options": ["gross_revenue", "operating_profit"]
}
```

### Scoped Insufficiency (`status: "NO_SUPPORT"` or `"RETRIEVAL_INCOMPLETE"`)
- `NO_SUPPORT`: Search completed over target scope without locating required supporting facts.
- `RETRIEVAL_INCOMPLETE`: Execution timed out, exceeded deadline, or hit unavailable dependency.
```json
{
  "query_id": "q_5",
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
