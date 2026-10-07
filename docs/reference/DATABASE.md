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
1. **Explicit Version & Generation Fields:**
   - `input_source_version`: Immutable source version number used to build the artifacts in staging.
   - `expected_active_source_version`: Active version expected before publication (`null` for initial upload; e.g. `7` during v8 staging).
   - `target_source_version`: Version being activated into the manifest (`input_source_version`).
   - `expected_manifest_generation`: Workspace manifest generation integer expected at publication commit time.
   - `baseline_generation`: Generation integer of baseline stores materializing a given `source_version`.
   - `enrichment_generation`: Generation integer of optional enrichment derived from `(source_version, baseline_generation)`.
   *(Note: The alias "source generation" is prohibited.)*
2. **Baseline Staging and Replacement:**
   - Staging a replacement (e.g. v8) generates baseline artifacts while the previous version (e.g. v7) remains fully active and queryable.
   - Pre-publication checks do not require `active_source_version == input_source_version`. The activation transaction verifies `active_source_version == expected_active_source_version` (v7).
   - Atomic CAS commits verify: manifest generation matches `expected_manifest_generation`, target source is registered, authorized, and not tombstoned, and artifacts match `target_source_version` and passed baseline extraction QA.
3. **Enrichment Binding:**
   - Optional enrichment binds strictly to the exact `active_source_version` AND `baseline_generation` it was built against.
   - Enrichment publication extends the active baseline; it cannot change `active_source_version` or overwrite newer incompatible enrichment.
4. **Conditional CAS Transactions & Delta Merges:**
   - Manifest record in DynamoDB: `PK: WORKSPACE#<workspace_id>`, `SK: MANIFEST`, with `manifest_generation` and a `sources` map.
   - Baseline Activation Condition:
     `manifest_generation = :expected_manifest_generation AND (attribute_not_exists(sources.#src.active_source_version) OR sources.#src.active_source_version = :expected_active_source_version) AND attribute_not_exists(sources.#src.tombstoned_at)`
   - Enrichment Publication Condition:
     `manifest_generation = :expected_manifest_generation AND sources.#src.active_source_version = :target_source_version AND sources.#src.baseline_generation = :based_on_baseline_generation AND (attribute_not_exists(sources.#src.enrichment_generation) OR sources.#src.enrichment_generation < :new_enrichment_generation) AND attribute_not_exists(sources.#src.tombstoned_at)`
   - On CAS conflict, workers re-read the updated manifest, re-validate dependencies (target registered, not tombstoned, expected active version still valid, baseline generation not advanced), and merge localized source deltas, preserving concurrent updates from other documents. Bounded retries stop immediately if the job is stale, deleted, or incompatible.
5. **Tombstone Records & Immediate Access Barrier:**
   - Deletion writes an immediate atomic `tombstoned_at` timestamp and increments `manifest_generation`.
   - In-flight jobs detecting a tombstone immediately abort. Tombstoned sources can never be resurrected by stale jobs.

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
