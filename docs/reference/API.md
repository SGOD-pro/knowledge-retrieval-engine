# API Contract

## Base
`/api/v1`

## Authentication
Protected routes require bearer/OAuth authorization according to deployment policy.

## Workspace
- `GET /workspaces`
- `POST /workspaces`

## Documents
- `POST /workspaces/{workspace_id}/documents`
- `GET /workspaces/{workspace_id}/documents`
- `GET /documents/{document_id}/file`

Upload response should include document/source identity, baseline readiness and enrichment status.

## Query
`POST /query`

```json
{
  "workspace_id": "ws_1",
  "session_id": "sess_1",
  "query": "What was Europe revenue in 2024?",
  "document_ids": ["doc_1"]
}
```

Response:
```json
{
  "query_id": "q_1",
  "snapshot_id": "snap_42",
  "status": "COMPLETE",
  "answer": "...",
  "requirements": [],
  "claims": [],
  "citations": [],
  "usage": {},
  "timings": {}
}
```

## Compatibility
Reuse current repository route families where practical, but migrate request/response bodies to `TECHNICAL_SPEC.md`.
