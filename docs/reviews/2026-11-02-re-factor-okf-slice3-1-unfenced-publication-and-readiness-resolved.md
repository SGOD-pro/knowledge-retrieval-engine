# Review, re-factor, 2026-11-02

**Reviewed by**: Staff Software Engineer
**Scope**: OKF Slice 3.1 Unfenced Publication and False Readiness Resolution
**Verdict**: Approve

## Summary

This review records the resolution and verification of unfenced publication vulnerabilities and false readiness defects in Open Knowledge Framework (OKF) Slice 3.1. It establishes authoritative readiness definitions and proves that unauthorized or failed workers cannot overwrite active or ready manifests in DynamoDB and SQLite.

## Previously Reported Results

Prior rounds established and verified:
* Bounded Bedrock extraction with maximum 2 calls, 4000 input tokens, and 1200 output tokens per document version.
* Atomic call and token ceiling enforcement across absent ledger and subsequent reservation attempts.
* Separate prompt and output ceilings preserved across restarts without integer division heuristics.
* Authoritative request token bounding via Bedrock Runtime `count_tokens` failing closed before dispatch.
* Deterministic publication race fencing preventing stale workers from exposing assertions after validation.
* DynamoDB Local container integration and multi process SQLite WAL execution.

## New Test Evidence and Verifications

This change resolves the unfenced publication vulnerabilities and false readiness defects:

1. **Protection against unfenced overwrites**:
   - In [`OkfBuilder._extract_pdf`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1411), workers without an authorized reservation fence skip calling `save_manifest` and log a warning, returning the in memory manifest without modifying durable storage.
   - In [`OkfBuilder.extract_and_persist`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1080), top level exception handling only attempts manifest writes when `fence_token` is present.
   - In [`DynamoDBOkfRepository.save_manifest`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L1832), unfenced writes (`fence_token is None`) enforce atomic conditions:
     * When incoming status is failed or empty, a `ConditionCheck` on `BUDGET#DOC#{doc}#VER#{ver}` requires `attribute_not_exists(PK)`, preventing unauthorized workers from writing over Bedrock tracked documents.
     * When incoming status is ready (such as CSV table extraction), `ConditionCheck` requires `attribute_not_exists(PK) OR attribute_not_exists(lease_expires_at) OR lease_expires_at < :now_epoch`, protecting active worker leases.
     * The manifest Put action enforces `attribute_not_exists(PK) OR :incoming_status = :ready_status OR #status <> :ready_status`, preventing any unfenced write from clobbering an existing ready manifest.
   - In [`SQLiteOkfRepository.save_manifest`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L901) and [`InMemoryOkfRepository.save_manifest`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L278), identical checks atomically verify that no active lease is held, no budget ledger exists for unfenced failed writes, and ready manifests are not overwritten.
   - Verified in `test_ordering_denied_worker_b_cannot_overwrite_published_worker_a_sqlite`, `test_ordering_denied_worker_b_cannot_overwrite_published_worker_a_dynamodb_local`, `test_worker_b_fails_before_reservation_cannot_overwrite_active_lease_sqlite`, and `test_worker_b_fails_before_reservation_cannot_overwrite_active_lease_dynamodb_local`.

2. **Authoritative readiness definition in has_ready_okf**:
   - In [`SQLiteOkfRepository.has_ready_okf`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L1080), readiness checks the exact authoritative manifest row (`pk = WORKSPACE#{ws}`, `sk = MANIFEST#DOC#{doc}#VER#{ver}`), returning True only when `status == "ready"`.
   - In [`DynamoDBOkfRepository.has_ready_okf`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L2015), readiness performs a strongly consistent base table `get_item` on `PK = WORKSPACE#{ws}`, `SK = MANIFEST#DOC#{doc}#VER#{ver}`, eliminating eventual consistency lag and avoiding GSI2 prefix collisions where non ready manifests or stale assertions triggered false readiness.
   - In [`InMemoryOkfRepository.has_ready_okf`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/okf_repo.py#L346), readiness evaluates only the authoritative manifest record, eliminating legacy iteration over loose assertions.
   - Verified in `test_stale_assertions_coexisting_with_failed_or_newer_manifest` and `test_cross_workspace_and_version_readiness_isolation`.

3. **Publication boundary verification (99 items)**:
   - Verified against DynamoDB Local in `test_dynamodb_local_publication_99_item_boundary_and_readiness`.
   - At exactly 99 items (1 manifest + 1 entity + 97 assertions), the transaction commits atomically within DynamoDB's 100 action limit, and `has_ready_okf` returns True.
   - At exactly 100 items (1 manifest + 1 entity + 98 assertions), `save_manifest` raises `StorageUnavailableError`, caught under ADR-001 fail open rules. Zero items are committed to DynamoDB, and `has_ready_okf` returns False. Baseline ingestion proceeds without interruption.

4. **Specification Status**:
   - Specification 0007 remains Proposed and blocked on full completion of specification 0006.
   - Specification 0003 remains Proposed and deferred.
