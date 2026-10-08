# 0003 Snapshot registry, publication, and atomic deletion

**Status**: Proposed

## Summary
The snapshot registry tracks the active versions of data inside a workspace. Publication atomically swaps the active snapshot pointer after all items are ready. Deletion uses a tombstone that blocks access instantly across all storage backends.

## Context
The knowledge retrieval engine builds on DynamoDB, Redis, and Qdrant. We need a way to publish new data without readers seeing partial updates. Deleting data must also block access instantly, even if the physical cleanup takes time.

## Requirements
- `AC-1`: Publication is atomic via a DynamoDB transaction using a generation counter for optimistic locking.
- `AC-2`: Immutable versioned artifacts are staged first, and marked ready by workers after physical writes. Artifact readiness is verified in bounded batches outside the publication transaction to seal the candidate. The final compare and swap transaction verifies the seal, expected generation, and commits the pointer.
- `AC-3`: Reads pin a specific committed snapshot. Retrieval from Redis or Qdrant validates candidates against the pinned source, version, and artifact bindings.
- `AC-4`: Missing referenced artifacts on read trigger at most 3 retries with backoff. If unavailable, return a HTTP 503 ARTIFACT_UNAVAILABLE error. Snapshots are never mixed. Explicit incomplete results apply only to insufficient coverage, not infrastructure failure.
- `AC-5`: Authoritative authorization and tombstones always override pinned snapshots. Return HTTP 404 for deleted or unauthorized resources.
- `AC-6`: Deletion atomically commits a durable tombstone and increments the authoritative manifest generation before returning success.
- `AC-7`: Authoritative deletion checks are enforced at repository access and final delivery, overriding cached or pinned states. Stale Redis entries cannot authorize access.
- `AC-8`: A source tombstone blocks all versions and artifacts for that source, while other sources remain usable. Workspace deletion blocks all children.
- `AC-9`: Physical cleanup runs asynchronously. The garbage collection job sweeps abandoned staging records and explicitly deletes their physical assets in S3, Redis, and Qdrant. Publications cannot resurrect tombstoned data.

## Decision
We will reuse and extend the existing workspace and tombstone records. We will add a snapshot record to store the immutable bindings, partitioning them across multiple DynamoDB items to avoid the 400KB size limit. The workspace record acts as the root manifest holding the generation counter and the active snapshot pointer.

**Implementation skills:** None added, using existing FastAPI and DynamoDB patterns.

## Options considered
- Use a single massive JSON blob for the snapshot manifest. Rejected because it risks hitting the DynamoDB item size limits for large workspaces.
- Publish items and update a log, letting reads resolve the latest version. Rejected because reads become too complex and slow.
- The chosen path of a root manifest with partitioned snapshot records balances size limits and atomic publication.

## Rationale
Reusing the workspace record as the root manifest avoids creating a second generation counter. Partitioning the bindings into separate snapshot records prevents DynamoDB size limit issues while keeping the data immutable and versioned.

## Feature design

### Value sourcing
| Value | Source |
|---|---|
| `workspace_id` | Path parameter |
| `manifest_generation` | Provided by the client for the compare and swap check |
| `snapshot_id` | Generated securely by the backend during the staging phase |
| `artifacts` | Extracted from the staging payload or internal worker output |

### Data model
**WorkspaceRecord (Root manifest extension)**
- `workspace_id` (Partition key)
- `manifest_generation` (The authoritative counter)
- `active_snapshot_id`
- `schema_version`, `created_at`, `updated_at`

**SnapshotRecord (Immutable bindings)**
- `workspace_id` (Partition key)
- `sort_key` (Combines `snapshot_id`, `source_id`, and a partition index for scaling)
- `artifacts` (References to the staged items)

**SnapshotArtifact**
- `workspace_id` (Partition key)
- `artifact_id` (Sort key)
- `storage_references` (Pointers to Redis, Qdrant, object storage)
- `readiness_state` (Marks if it is staging, ready, sealed, or committed)

**TombstoneRecord**
- Reuses existing models and deletion states: `workspace_id`, `resource_type` (source or workspace), `resource_id`.

### API surface
- `POST /workspaces/{workspace_id}/snapshots`: Idempotently create a staging record. Restricted to internal ingestion workers.
- `PATCH /workspaces/{workspace_id}/snapshots/{snapshot_id}/artifacts/{artifact_id}/ready`: A trusted worker only operation. The server verifies artifact identity, checksum, and readability before recording READY via a conditional update. Seals the candidate once all required artifacts are ready.
- `PUT /workspaces/{workspace_id}/snapshots/{snapshot_id}/publish`: Atomically commit the sealed snapshot. Requires the expected `manifest_generation` and verifies the candidate seal. Returns a HTTP 409 conflict on a generation mismatch. Restricted to internal workers.
- `GET /workspaces/{workspace_id}/snapshots/active`: Return the compact manifest and the committed `snapshot_id`. Requires workspace authorization.
- `GET /workspaces/{workspace_id}/snapshots/{snapshot_id}`: Read authorized metadata and support pinned queries.

### Security and edge cases
On a HTTP 409 conflict during publication, the worker reloads the latest committed manifest and rebuilds its proposal. It revalidates authorization, tombstones, and readiness before retrying. Bindings that change result in a new candidate snapshot; an already sealed snapshot is never modified. Retries are capped with backoff and jitter. Staging records that remain unpublished after a timeout are swept by an asynchronous garbage collection job, which deletes both the records and physical assets. Writing a source tombstone atomically increments the workspace root generation. Repeated deletion returns the existing tombstone without incrementing.

## Build plan
1. Update `WorkspaceRecord` with `manifest_generation` and `active_snapshot_id`.
2. Implement `SnapshotRecord` and `SnapshotArtifact` DynamoDB models, handling pagination for large source artifact lists.
3. Build the backend staging endpoint for creating snapshots idempotently.
4. Implement the readiness PATCH endpoint to verify physical writes and seal the candidate snapshot.
5. Build the atomic publication endpoint with a small final transaction (verifies seal, checks generation, updates pointer).
6. Update the query path to read the active snapshot, enforce pinned boundaries, and retry up to 3 times on missing artifacts (returning HTTP 503).
7. Implement the immediate tombstone check across all repository accesses, including the generation increment on source deletion.
8. Implement the background garbage collection job for stale staging records and their physical assets.

## Consequences
Reads must fetch the manifest first, adding one DynamoDB query to the critical path. Ingestion workers must handle HTTP 409 conflicts gracefully and rebuild their candidate snapshots. Garbage collection requires tracking and deleting distributed physical assets.

## References
(basis: Architecture Bible section 3, docs/reference/DATABASE.md, docs/MIGRATION_MAP.md)
