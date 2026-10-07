# KRE Security Contract

## Authentication Authority vs. Request Context
- **Defined Authentication Mechanism:** Protected endpoints strictly require a valid bearer credential (e.g., `Authorization: Bearer <token>`) or an authenticated server session backed by a defined authentication mechanism (such as cryptographic session tokens or cloud IAM).
- **Context vs. Authority:** Request parameters—including `session_id`, `workspace_id`, `document_id` / `source_id`, `job_id`, and `execution_id`—represent resource and execution context only, never authentication authority. Supplying an identifier grants zero access authority.
- **Principal Scope Validation:** Every incoming request resolves the caller's authenticated principal identity and validates:
  1. Principal membership and authorization to access the specified `workspace_id`.
  2. Principal ownership and access scope of the supplied chat `session_id` within that workspace.
  3. Principal access rights to all selected `document_ids` / `source_ids` and specific file versions.
  4. Authorization to view or poll referenced `job_id` tasks and historical `execution_record_ref` lineage.

## Mandatory Final Delivery & Tombstone Barriers
- **Enforcement Points:** Authorization is enforced at API gateway entry, document registration, snapshot pinning, retrieval/execution dispatch, exact evidence fetch, and final answer delivery.
- **Final Delivery Recheck:** Before delivering any answer, summary, evidence excerpt, or citation to the client, the delivery pipeline re-evaluates principal authorization and tombstone state across all supporting sources.
- **Immediate Deletion/Revocation Barrier:** Document deletion or access revocation acts as an immediate hard barrier. In-flight requests, pinned historical snapshots, and cached answer lookups cannot deliver content from deleted or revoked sources.
- **Cache Invalidation:** Cache hits never bypass access, permissions, or tombstone checks.

## Untrusted Data & Execution Guardrails
- **Untrusted Input:** Uploaded file text, table cell values, sheet headers, file paths, URLs, and model-generated outputs are untrusted data.
- **Zero Arbitrary Execution:** No arbitrary Python, shell scripts, `eval()`, spreadsheet macros, unrestricted SQL queries, or generated filesystem/network access is permitted.
- **Safe Structured AST:** Structured formulas and aggregations execute exclusively through a strictly allowlisted AST of verified arithmetic and aggregation operators.
- **Immutability of Policies:** Source content and prompt inputs can never modify system routing policy, budget ledgers, authorization scopes, or tool permissions.
