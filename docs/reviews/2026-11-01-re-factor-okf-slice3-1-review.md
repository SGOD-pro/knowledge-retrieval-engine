# Review, re-factor, 2026-11-01 (OKF Slice 3.1)

**Reviewed by**: Senior Code Reviewer
**Scope**: 6 files, commit [`8331d03fd61ab5ead4bd9195ba864a39ba29769d`](file:///home/swyra/projects/knowledge-retrieval-engine)
**Verdict**: Changes Requested (Operational Gaps and Cloud Verification Owed)

## Summary

This review inspects commit [`8331d03fd61ab5ead4bd9195ba864a39ba29769d`](file:///home/swyra/projects/knowledge-retrieval-engine) on branch `re-factor`. The change introduces a durable extraction budget ledger across memory, SQLite, and DynamoDB stores, monotonic fence tokens, 15 second lease expiry, stale worker assertion suppression, and authoritative request token accounting via AWS Bedrock `count_tokens`.

The offline suite of 94 regression tests passes cleanly, including multi process contention against a live disk backed SQLite store. However, code inspection reveals two notable operational discrepancies:
1. Throttling retries reuse the reservation slot on the durable ledger without incrementing `reserved_calls`. In a multi process ingestion environment, this permits concurrent workers to exceed the two call ceiling across processes.
2. Bedrock `count_tokens` and DynamoDB atomic updates have been verified only with mocks (`MagicMock` and `botocore.stub.Stubber`), not against live AWS endpoints.

Marking specification 0006 acceptance criterion AC-3 and specification 0007 dependencies unconditionally complete is premature until these items are addressed.

## Detailed Findings by Focus Area

### 1. Bedrock Invocation Attempts and the Two Call Ceiling
* In [`OkfBuilder._call_bedrock_converse`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1529), [`reserve_extraction_budget`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L978) is called once before the attempt loop.
* Inside the retry loop, [`DocumentExtractionBudget.reserve_call`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L127) increments `calls_made` in memory on each attempt. Within a single process, attempt 0 plus throttling retry attempt 1 reaches the ceiling of 2 calls, preventing subsequent batches in that process.
* However, on the durable repository ledger, `reserved_calls` remains at 1. If another process inspects or reserves against the durable ledger, it sees `reserved_calls = 1 < 2` and receives a reservation. Across multiple processes, 3 physical Bedrock calls can be dispatched for a single document version.
* **Recommendation**: Require retries to acquire a distinct reservation on the durable ledger, or reserve the maximum potential attempt allowance (2 calls) upfront when retries are permitted.

### 2. Concurrency, Lease Expiry, Fencing, and Crash Recovery
* **Multi process contention**: Verified on live disk backed SQLite in [`test_separate_processes_contention_sqlite`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_okf_slice3_1.py#L324). Exactly 2 processes acquired permits, and 4 were denied.
* **Lease expiry and fencing**: When a lease expires after 15 seconds and another worker advances the monotonic fence token, [`reconcile_extraction_budget`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L1190) returns `fenced_out = True`. In [`OkfBuilder._call_bedrock_converse`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1678), stale assertions are discarded and suppressed from publication, while spent tokens are reconciled into the ledger.
* **Crash recovery**: On restart, [`get_or_create_document_budget`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L220) performs a strongly consistent read. If `reserved_calls >= max_calls` or the read fails, the budget fails closed.
* **Timeouts and unknown outcomes**: When a caller times out while a provider call is in flight, [`fail_extraction_budget`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L1298) retains the reservation. Output text is set to empty, preventing unverified assertions from being stored.

### 3. Provider Token Accounting: Mocked versus Live Evidence
* **API match**: In [`OkfBuilder._call_bedrock_converse`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1500), `client.count_tokens` receives `modelId` and `input={"converse": {"messages": messages, "system": system}}`. This matches the messages and system prompts dispatched to `client.converse`.
* **Model configuration**: Configured model is `anthropic.claude-3-haiku-20240307-v1:0` in `us-east-1`. Boto3 client definition confirms `CountTokens` exists in the SDK service model.
* **Evidence distinction**: In the test suite, [`test_authoritative_request_token_bounding_fail_closed`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_okf_slice3_1.py#L534) and [`test_authoritative_request_token_count_failure_fails_closed`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_okf_slice3_1.py#L555) rely on `MagicMock`. No live AWS Bedrock network calls were executed. Live IAM permissions and endpoint responsiveness remain unverified against real AWS infrastructure.

### 4. Specification Conformance Verdict (AC-3 and 0007 Dependencies)
* Marking 0006 AC-3 and 0007 dependencies as completely closed is premature.
* While the multi process SQLite implementation and fail closed guards operate correctly, the cross process retry ledger accounting gap and unverified live AWS cloud behavior (IAM permissions, live CountTokens, live DynamoDB) must be documented as open operational limitations rather than fully closed.

## Proposed Live End to End Test Plan

A minimal live test exercising the current engine without re ingestion or re embedding:
* Workspace: `ws_canonical_comparison_0004` (contains 8 ready documents).
* Test queries:
  1. Supported factual narrative query: `"What are the key sectors identified in the National Strategy for Artificial Intelligence?"` (verifies embedding, hybrid retrieval, Cohere reranker, Claude 3 Haiku generation, and citation binding).
  2. Supported structured aggregate query: `"What is the total number of bills passed in 2014 in the Rajya Sabha bill records?"` (verifies TableStore deterministic scanning and aggregation without model cost).
  3. Unsupported question: `"What is the quantum teleportation protocol described in SEC Form 10-Q?"` (verifies clean refusal and zero citations).
* Estimated provider cost: Less than $0.01 total across 3 embedding calls, 2 reranker calls, and 2 LLM generation calls.
