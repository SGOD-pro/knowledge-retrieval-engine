# ADR-005 — Generation-Checked Atomic Publication & Concurrency Safety

## Decision
All workspace lifecycle state updates (baseline activation, enrichment extension, replacement, and deletion) use generation-checked Compare-And-Swap (CAS) atomic commits over immutable artifacts.

## Implementation Contract

### 1. Distinct Version and Generation Identities
The storage model strictly distinguishes and maintains the following identities (the undefined alias "source generation" is prohibited):
- **`input_source_version`**: The immutable source document version number used to extract and build the artifact set in staging.
- **`expected_active_source_version`**: The currently active version of the document expected in the workspace manifest immediately before publication (`null` / non-existent for the initial document upload; e.g. `7` when replacing v7 with v8).
- **`target_source_version`**: The immutable source version being activated in the manifest (equals `input_source_version` for baseline activation).
- **`expected_manifest_generation`**: Monotonically increasing workspace manifest generation integer expected at publication commit time.
- **`baseline_generation`**: Generation integer of baseline stores (lexical/vector/structured) materializing a given `source_version`.
- **`enrichment_generation`**: Generation integer of optional enrichment artifacts (PageIndex, OKF, Graph) derived from a specific `(source_version, baseline_generation)` pair.

### 2. Baseline Staging and Replacement Contract
- **Staging Isolation:** Ingesting a new document version (e.g. v8) creates an immutable source record and stages its baseline indexes independently while the previous version (e.g. v7) remains active and queryable.
- **Pre-Activation Invariant:** Activation does **not** require `active_source_version == input_source_version`. Prior to publication of v8, `active_source_version` is v7.
- **Atomic Pre-Commit Checks:** When publishing a baseline replacement, the transaction atomically verifies:
  1. Manifest generation matches `expected_manifest_generation`.
  2. Workspace `active_source_version` matches `expected_active_source_version` (v7).
  3. The target source version (v8) remains registered, authorized, and not tombstoned.
  4. Artifacts match `target_source_version` (v8) and passed baseline extraction QA audits.
- Upon successful CAS commit, `active_source_version` transitions to v8, `baseline_generation` is initialized/bumped, and `manifest_generation` increments by 1.

### 3. Enrichment Publication Contract
- **Strict Baseline Binding:** Optional enrichment jobs are built against an explicit snapshot of baseline data. Enrichment publication must bind to the **exact active source version AND baseline generation** it was built against (`active_source_version == input_source_version` and `active_baseline_generation == based_on_baseline_generation`).
- **No Version Switching:** Enrichment publication extends an existing active baseline; it can **never** switch or bump `active_source_version`.
- **Incompatible Enrichment Guard:** Enrichment publication cannot overwrite newer or incompatible enrichment (`enrichment_generation > existing_enrichment_generation`).
- If baseline rebuild or replacement occurred while enrichment was running (e.g., v8 became active while enrichment was running for v7), the enrichment publication CAS check fails and the stale enrichment is discarded.

### 4. CAS Conflict Handling & Bounded Retries
When multiple concurrent uploads or enrichment jobs run in the same workspace:
- **Scenario:** Worker A (publishing Doc 1) and Worker B (publishing Doc 2) read manifest generation $G$.
- Worker A commits first, incrementing the workspace manifest to $G+1$.
- Worker B's conditional update fails with a CAS conflict (`manifest_generation != G`).
- **Resolution Loop:**
  1. Worker B catches the conditional check failure and triggers a bounded retry.
  2. **Re-read & Revalidate:** Worker B fetches the updated manifest at $G+1$ and re-validates all dependencies:
     - Confirms target source remains registered, authorized, and not tombstoned.
     - Confirms `expected_active_source_version` remains compatible.
     - For enrichment, confirms active baseline generation has not advanced.
  3. **Early Abort:** If the revalidation reveals the source was deleted/tombstoned, replaced by an incompatible version, or the job is stale, Worker B immediately stops retries and aborts.
  4. **Delta Merge:** If still valid, Worker B computes its localized delta (updating only Doc 2's entry), preserving Worker A's committed updates for Doc 1.
  5. **Retry Commit:** Worker B executes CAS with `expected_manifest_generation = G+1`.

### 5. Stored Record Schema and Conditional Transactions
Workspace state is stored in DynamoDB using the following schema:
- **Workspace Manifest Item:**
  - `PK: WORKSPACE#<workspace_id>`
  - `SK: MANIFEST`
  - `manifest_generation`: `<int>`
  - `sources`: Map of `<source_id>` $\rightarrow$ Object:
    - `active_source_version`: `<int>`
    - `baseline_generation`: `<int>`
    - `enrichment_generation`: `<int>`
    - `status`: `"ACTIVE"` | `"STAGING"`
    - `tombstoned_at`: `<ISO-timestamp | null>`
- **Conditional Expression — Baseline Activation (Replacement v7 $\rightarrow$ v8):**
  ```text
  manifest_generation = :expected_manifest_generation
  AND sources.#src_id.active_source_version = :expected_active_source_version
  AND attribute_not_exists(sources.#src_id.tombstoned_at)
  ```
  *(For initial upload, replace the active version check with `attribute_not_exists(sources.#src_id)`).*
- **Conditional Expression — Enrichment Publication:**
  ```text
  manifest_generation = :expected_manifest_generation
  AND sources.#src_id.active_source_version = :target_source_version
  AND sources.#src_id.baseline_generation = :based_on_baseline_generation
  AND (attribute_not_exists(sources.#src_id.enrichment_generation) OR sources.#src_id.enrichment_generation < :new_enrichment_generation)
  AND attribute_not_exists(sources.#src_id.tombstoned_at)
  ```

### 6. Tombstones and Deletion Barriers
- Deleting or revoking a document writes `tombstoned_at = :timestamp` into the source record and increments `manifest_generation`.
- In-flight background jobs checking `expected_active_source_version` or tombstone attributes immediately abort and discard artifacts.
- Resurrection of tombstoned sources is strictly prevented.
- Deletion acts as an immediate hard barrier for query delivery and document viewing, overriding pinned historical snapshots.
