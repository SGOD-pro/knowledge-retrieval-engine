# KRE Storage/Data Contract

## Storage Roles
- **Object Storage (S3 / Blob):** Immutable raw source files, extracted full tabular dumps, large multi-page parser outputs, and durable execution lineage archives.
- **DynamoDB:** Workspace metadata, source/version registry, canonical evidence metadata, capability manifests, snapshot pointers, structured execution records, checkpoint states, and OKF/graph adjacency records.
- **QdrantDB:** Dense vector collections for compatible narrative prose retrieval. Dual named vectors or dedicated collections per model family.
- **Redis / ElastiCache:** Version-aware and authorization-aware query cache and transient rate/budget tracking.

## Logical Entities & Identity
- Entities: `Workspace`, `Source`, `SourceVersion`, `CanonicalEvidence`, `CapabilityArtifact`, `CapabilityManifest`, `KnowledgeMapEntry`, `Snapshot`, `Requirement`, `Query`, `DiscoveryCandidate`, `ExecutionRecord`, `Claim`, `Job`, `Checkpoint`, `CacheEntry`, `Tombstone`.
- Source identity carries `(workspace_id, source_id, source_version)`.
- Query execution identity carries `(workspace_id, query_id, snapshot_id, requirement_id)`.

## Publication Safety & Generation Contracts (ADR-005)
1. **Four Generation Counters:**
   - `manifest_generation`: Workspace-level generation integer.
   - `source_version`: Monotonic source version number.
   - `baseline_generation`: Generation of baseline indexes for that source version.
   - `enrichment_generation`: Generation of optional enrichment for that source version.
2. **Conditional CAS Updates:**
   - Updates to workspace manifests in DynamoDB use conditional expressions: `manifest_generation = :expected_manifest_gen`.
3. **Atomic Delta Merge for Concurrent Uploads:**
   - Simultaneous uploads of distinct documents merge localized source entries into the active manifest. On CAS failure, workers re-read the updated manifest, merge their delta, and retry.
4. **Tombstone Records & Immediate Access Barrier:**
   - Deletion writes a `Tombstone` item with `deleted_at` timestamp and revokes access immediately.
   - Stale background jobs checking `expected_source_generation` abort when encountering a tombstone. Resurrection of deleted documents is strictly prevented.

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
- **Model Compatibility:** Vectors from different models (e.g. BGE-small 384 vs Titan 1024) are stored in separate named vectors or distinct collections. Query vectors are never compared against incompatible model dimensions.

## Graph & Relationship Storage
- All graph relation edges stored in DynamoDB (`okf_relations`) require source provenance (`source_id`, `source_version`, `locator`).
- Cross-source graph edges store all supporting source version IDs. If any supporting source version is updated or tombstoned, the edge is invalidated until re-audited.

