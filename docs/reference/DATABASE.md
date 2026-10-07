# KRE Storage/Data Contract

## Storage Roles
- **Object Storage (S3 / Blob):** Immutable raw source files, extracted full tabular dumps, large multi-page parser outputs, and durable execution lineage archives.
- **DynamoDB:** Workspace metadata, source/version registry, canonical evidence metadata, capability manifests, snapshot pointers, structured execution records, checkpoint states, and OKF/graph adjacency records.
- **QdrantDB:** Dense vector storage for compatible narrative prose retrieval. Different embedding models use separate collections or separate named-vector configurations within one collection (e.g. `embedding_fast` and `embedding_full`).
- **Redis / ElastiCache:** Version-aware and authorization-aware query cache and transient rate/budget tracking.

## Logical Entities & Identity
- Entities: `Workspace`, `Source`, `SourceVersion`, `CanonicalEvidence`, `CapabilityArtifact`, `CapabilityManifest`, `KnowledgeMapEntry`, `Snapshot`, `Requirement`, `Query`, `DiscoveryCandidate`, `ExecutionRecord`, `Claim`, `Job`, `Checkpoint`, `CacheEntry`, `Tombstone`.
- Source identity carries `(workspace_id, source_id, source_version)`.
- Query execution identity carries `(workspace_id, query_id, snapshot_id, requirement_id)`.

## Publication Safety & Generation Contracts (ADR-005)

### 1. Explicit Version & Generation Fields
- `input_source_version`: Immutable source version number used to build the artifacts in staging.
- `expected_active_source_version`: Active version expected before publication (`null` for initial upload; e.g. `7` during v8 staging, or `7` during v7 rebuild).
- `target_source_version`: Version being activated into the manifest (`input_source_version`).
- `expected_manifest_generation`: Workspace manifest generation integer expected at publication commit time.
- `expected_baseline_generation`: Baseline generation integer expected before publication (`null` for initial upload; matches active source baseline generation for replacement or same-version rebuilds).
- `baseline_generation`: Monotonically increasing generation integer of baseline stores materializing a given `source_version`.
- `enrichment_generation`: Monotonically increasing generation integer of optional enrichment derived from `(source_version, baseline_generation)`.
*(Note: The alias "source generation" is prohibited.)*

### 2. Bounded Storage Layout (DynamoDB 400 KB Limit Protection)
Embedding all workspace sources within a single item is unbounded and violates DynamoDB's 400 KB limit for large corpora. Storage is partitioned into:
1. **Workspace Root Manifest Item (`PK: WORKSPACE#<workspace_id>, SK: MANIFEST`):**
   - Fixed-size record (< 1 KB) storing workspace-level counters:
     * `manifest_generation`: `<int>`
     * `active_source_count`: `<int>`
     * `updated_at`: `<timestamp>`
2. **Per-Source State Items (`PK: WORKSPACE#<workspace_id>, SK: SOURCE#<source_id>`):**
   - Independent, bounded records (~1 KB each) storing document status:
     * `source_id`: `<string>`
     * `active_source_version`: `<int>`
     * `baseline_generation`: `<int>`
     * `enrichment_generation`: `<int>`
     * `status`: `"ACTIVE"` | `"STAGING"`
     * `updated_at`: `<timestamp>`

### 3. Tombstone Semantics
- **Absence of Attribute:** Undeleted sources have **no `tombstoned_at` attribute** (omitted completely from active/registered source records).
- **DynamoDB Null Semantics:** DynamoDB considers stored `NULL` attributes present (`attribute_exists` evaluates to true). Active source records **must not** store `tombstoned_at: null`. Deletion creates the attribute: `SET tombstoned_at = :now`.

### 4. Atomic Multi-Item Transactions (`TransactWriteItems`)
All publication operations coordinate atomically across the root manifest and per-source records via DynamoDB `TransactWriteItems`:
- **Initial Document Upload:**
  - Root Item Update: `SET manifest_generation = manifest_generation + :one` with condition `manifest_generation = :expected_manifest_generation`.
  - Source Item Put: Condition `attribute_not_exists(SK)`, initializing `active_source_version = 1`, `baseline_generation = 1`, `status = "ACTIVE"`.
- **Baseline Replacement (v7 $\rightarrow$ v8) & Same-Version Rebuild:**
  - Root Item Update: `SET manifest_generation = manifest_generation + :one` with condition `manifest_generation = :expected_manifest_generation`.
  - Source Item Update: Condition:
    ```text
    attribute_not_exists(tombstoned_at)
    AND active_source_version = :expected_active_source_version
    AND baseline_generation = :expected_baseline_generation
    ```
    Sets `active_source_version = :target_source_version`, `baseline_generation = :new_baseline_generation`.
- **Same-Version Rebuild Stale-Job Guard:**
  If two jobs concurrently rebuild the same source version (e.g. v7), the first to commit advances `baseline_generation` to $B+1$. The slower job fails `baseline_generation = :expected_baseline_generation` ($B$), halts retries immediately, and discards its stale artifacts.
- **Enrichment Publication:**
  - Root Item Update: `SET manifest_generation = manifest_generation + :one` with condition `manifest_generation = :expected_manifest_generation`.
  - Source Item Update: Condition:
    ```text
    attribute_not_exists(tombstoned_at)
    AND active_source_version = :target_source_version
    AND baseline_generation = :based_on_baseline_generation
    AND (attribute_not_exists(enrichment_generation) OR enrichment_generation < :new_enrichment_generation)
    ```
    Sets `enrichment_generation = :new_enrichment_generation`.

### 5. CAS Conflict Handling
On conflict (`TransactionCanceledException`), workers re-read the updated root manifest and target source item, re-validate dependencies (confirming source is undeleted and `active_source_version` / `baseline_generation` have not advanced), and retry. Bounded retries stop immediately if the job is stale, deleted, or incompatible.

## Checkpoint and Idempotency Identity
Ingestion and heavy transformation jobs are keyed by an immutable idempotency key:
\[
JobKey = H(\text{workspace}, \text{source\_id}, \text{source\_version}, \text{content\_hash}, \text{pipeline\_version}, \text{operation\_policy\_version})
\]
- When a job with an identical `JobKey` is submitted, the worker resumes from the last recorded checkpoint rather than duplicating artifacts.
- Checkpoint items record: `job_id`, `cursor` (page/row/partition), `completed_artifacts`, `input_hash`, `output_hashes`, `attempt_count`, and `last_progress_timestamp`.

## Vector Store Operational Rules
- **Selective Prose Chunking:** Only meaningful prose paragraphs and narrative headings are embedded. Large structured tables are indexed by schema/summary and queried via structured execution.
- **No Document Embedding During Q&A:** Documents are embedded only at ingestion. No document chunking or document embedding occurs during query time.
- **Embedding Model Compatibility:**
  - Different embedding models may use separate collections OR separate named-vector configurations within one collection.
  - Compatibility encompasses model identity/version, dimensions, normalization, and distance metric—not dimension alone.
  - A query vector is never compared against an incompatible model's vectors.
  - Compatible ranked candidate lists may be fused according to the RRF contract; raw incompatible vectors or similarity values are never mixed.

## Graph & Relationship Storage
- All graph relation edges stored in DynamoDB (`okf_relations`) require source provenance (`source_id`, `source_version`, `locator`).
- Cross-source graph edges store all supporting source version IDs. If any supporting source version is updated or tombstoned, the edge is invalidated until re-audited.
