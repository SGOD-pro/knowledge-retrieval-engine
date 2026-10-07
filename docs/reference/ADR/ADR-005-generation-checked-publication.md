# ADR-005 — Generation-Checked Atomic Publication & Concurrency Safety

## Decision
All workspace lifecycle state updates (baseline activation, same-version baseline rebuild, enrichment extension, replacement, and deletion) use generation-checked Compare-And-Swap (CAS) atomic commits over immutable artifacts.

To strictly adhere to Amazon DynamoDB's **400 KB item limit**, the storage layout decouples the workspace into a compact root manifest item and bounded per-source items, coordinating atomic publication across both items using DynamoDB `TransactWriteItems`.

## Implementation Contract

### 1. Distinct Version and Generation Identities
The storage model strictly distinguishes and maintains the following identities (the undefined alias "source generation" is prohibited):
- **`input_source_version`**: The immutable source document version number used to extract and build the artifact set in staging.
- **`expected_active_source_version`**: The currently active version of the document expected in the source record immediately before publication (`null` / non-existent for initial document upload; e.g. `7` when replacing v7 with v8, or `7` during a same-version rebuild of v7).
- **`target_source_version`**: The immutable source version being activated in the manifest (equals `input_source_version` for baseline activation).
- **`expected_manifest_generation`**: Monotonically increasing workspace manifest generation integer expected at publication commit time.
- **`expected_baseline_generation`**: The baseline generation integer expected before publication (`null` / non-existent for initial upload; matches active source baseline generation for replacement or same-version rebuilds).
- **`baseline_generation`**: Monotonically increasing generation integer of baseline stores (lexical/vector/structured) materializing a given `source_version`.
- **`enrichment_generation`**: Monotonically increasing generation integer of optional enrichment artifacts (PageIndex, OKF, Graph) derived from a specific `(source_version, baseline_generation)` pair.

### 2. Baseline Staging, Replacement, and Same-Version Rebuild Guards
- **Staging Isolation:** Ingesting a new document version (e.g. v8) creates an immutable source record and stages its baseline indexes independently while the previous version (e.g. v7) remains active and queryable.
- **Pre-Activation Invariant:** Activation does **not** require `active_source_version == input_source_version`. Prior to publication of v8, `active_source_version` is v7.
- **Same-Version Rebuild Stale-Job Guard:** When background jobs rebuild baseline indexes for an already active source version (e.g. two jobs concurrently re-indexing v7), verifying `active_source_version` alone is insufficient to prevent race conditions. The publishing transaction and conflict retry validation **must verify `baseline_generation == expected_baseline_generation`**.
  - If a newer rebuild job completes and publishes generation $B+1$, an older in-flight rebuild job targeting generation $B$ will fail the condition `baseline_generation == B`.
  - The older job detects the advance, immediately stops retries, aborts, and discards its stale artifacts, preventing an older rebuild from silently overwriting a newer baseline.
- **Atomic Pre-Commit Checks:** When publishing a baseline (replacement or rebuild), the transaction atomically verifies:
  1. Workspace root manifest generation matches `expected_manifest_generation`.
  2. Source item `active_source_version` matches `expected_active_source_version` (or does not exist for initial upload).
  3. Source item `baseline_generation` matches `expected_baseline_generation` (or does not exist for initial upload).
  4. The target source remains registered, authorized, and undeleted (has no `tombstoned_at` attribute).
  5. Artifacts match `target_source_version` and passed baseline extraction QA audits.

### 3. Enrichment Publication Contract
- **Strict Baseline Binding:** Optional enrichment jobs are built against an explicit snapshot of baseline data. Enrichment publication must bind to the **exact active source version AND baseline generation** it was built against (`active_source_version == input_source_version` and `active_baseline_generation == based_on_baseline_generation`).
- **No Version Switching:** Enrichment publication extends an existing active baseline; it can **never** switch or bump `active_source_version`.
- **Incompatible Enrichment Guard:** Enrichment publication cannot overwrite newer or incompatible enrichment (`enrichment_generation > existing_enrichment_generation`).
- If baseline rebuild or replacement occurred while enrichment was running (e.g., v8 became active or `baseline_generation` advanced while enrichment was running against an older baseline), the enrichment publication CAS check fails and the stale enrichment is discarded.

### 4. Bounded Manifest Layout (DynamoDB 400 KB Limit Protection)
Embedding all workspace sources within a single manifest item creates an unbounded item that exceeds DynamoDB's 400 KB limit for large corpora. The physical storage model separates global pointers from per-source states:

1. **Workspace Root Manifest Item (`PK: WORKSPACE#<workspace_id>, SK: MANIFEST`):**
   - Compact, fixed-size record (< 1 KB) storing only workspace-level metadata:
     * `manifest_generation`: `<int>` (monotonic generation integer incremented on every publication/deletion)
     * `active_source_count`: `<int>`
     * `updated_at`: `<ISO-8601 timestamp>`
2. **Per-Source State Items (`PK: WORKSPACE#<workspace_id>, SK: SOURCE#<source_id>`):**
   - Independent, bounded records (~1 KB each) storing document lifecycle state:
     * `source_id`: `<string>`
     * `active_source_version`: `<int>`
     * `baseline_generation`: `<int>`
     * `enrichment_generation`: `<int>`
     * `status`: `"ACTIVE"` | `"STAGING"`
     * `content_hash`: `<string>`
     * `registered_file_id`: `<string>`
     * `updated_at`: `<ISO-8601 timestamp>`

### 5. Atomic Multi-Item Transactions (`TransactWriteItems`)
All publication and lifecycle operations coordinate atomically across the root manifest item and the target source item using DynamoDB `TransactWriteItems`.

#### Tombstone Representation
- **Absence of Attribute:** Undeleted sources have **no `tombstoned_at` attribute** (the attribute is completely omitted from active/registered source records).
- **DynamoDB Null Semantics:** DynamoDB considers a stored `NULL` attribute to be present (`attribute_exists` evaluates to true). Therefore, active records **must not** store `tombstoned_at: null`. Deletion creates the attribute: `SET tombstoned_at = :now`.

#### A. Initial Upload Publication
```text
TransactWriteItems:
1. Update Root Manifest (PK: WORKSPACE#<ws_id>, SK: MANIFEST):
   Condition: manifest_generation = :expected_manifest_generation
   Update:    SET manifest_generation = manifest_generation + :one, updated_at = :now

2. Put Source Item (PK: WORKSPACE#<ws_id>, SK: SOURCE#<source_id>):
   Condition: attribute_not_exists(SK)
   Item:      {
     "source_id": :source_id,
     "active_source_version": 1,
     "baseline_generation": 1,
     "status": "ACTIVE",
     "updated_at": :now
   }
```

#### B. Baseline Replacement (v7 $\rightarrow$ v8) or Same-Version Rebuild
```text
TransactWriteItems:
1. Update Root Manifest (PK: WORKSPACE#<ws_id>, SK: MANIFEST):
   Condition: manifest_generation = :expected_manifest_generation
   Update:    SET manifest_generation = manifest_generation + :one, updated_at = :now

2. Update Source Item (PK: WORKSPACE#<ws_id>, SK: SOURCE#<source_id>):
   Condition: attribute_not_exists(tombstoned_at)
              AND active_source_version = :expected_active_source_version
              AND baseline_generation = :expected_baseline_generation
   Update:    SET active_source_version = :target_source_version,
                  baseline_generation = :new_baseline_generation,
                  #status = :active_status,
                  updated_at = :now
```
*(For a same-version rebuild, `:target_source_version` equals `:expected_active_source_version`, and `:new_baseline_generation = expected_baseline_generation + 1`).*

#### C. Enrichment Publication
```text
TransactWriteItems:
1. Update Root Manifest (PK: WORKSPACE#<ws_id>, SK: MANIFEST):
   Condition: manifest_generation = :expected_manifest_generation
   Update:    SET manifest_generation = manifest_generation + :one, updated_at = :now

2. Update Source Item (PK: WORKSPACE#<ws_id>, SK: SOURCE#<source_id>):
   Condition: attribute_not_exists(tombstoned_at)
              AND active_source_version = :target_source_version
              AND baseline_generation = :based_on_baseline_generation
              AND (attribute_not_exists(enrichment_generation) OR enrichment_generation < :new_enrichment_generation)
   Update:    SET enrichment_generation = :new_enrichment_generation,
                  updated_at = :now
```

#### D. Deletion / Tombstone Barrier
```text
TransactWriteItems:
1. Update Root Manifest (PK: WORKSPACE#<ws_id>, SK: MANIFEST):
   Condition: manifest_generation = :expected_manifest_generation
   Update:    SET manifest_generation = manifest_generation + :one, updated_at = :now

2. Update Source Item (PK: WORKSPACE#<ws_id>, SK: SOURCE#<source_id>):
   Condition: attribute_not_exists(tombstoned_at)
   Update:    SET tombstoned_at = :now,
                  #status = :tombstoned_status,
                  updated_at = :now
```

### 6. CAS Conflict Handling & Bounded Retries
When multiple concurrent uploads or enrichment jobs run in the same workspace:
- **Scenario:** Worker A (publishing Doc 1) and Worker B (publishing Doc 2) read root manifest generation $G$.
- Worker A commits first, incrementing the workspace manifest to $G+1$.
- Worker B's transaction fails with `TransactionCanceledException` because `manifest_generation != G`.
- **Resolution Loop:**
  1. Worker B catches the conditional check failure and triggers a bounded retry.
  2. **Re-read & Revalidate:** Worker B fetches the root manifest at $G+1$ and re-reads the source item `SK: SOURCE#doc_2`:
     - Confirms `attribute_not_exists(tombstoned_at)`.
     - Confirms `active_source_version == expected_active_source_version`.
     - Confirms `baseline_generation == expected_baseline_generation`.
     - For enrichment, confirms active baseline generation has not advanced.
  3. **Early Abort:** If revalidation reveals the source was deleted/tombstoned, replaced by an incompatible version, or that a newer rebuild committed (`baseline_generation != expected_baseline_generation`), Worker B immediately stops retries and aborts.
  4. **Retry Commit:** If still valid, Worker B re-executes `TransactWriteItems` with `expected_manifest_generation = G+1`. Since Doc 1 and Doc 2 update distinct source items, neither document overwrites the other.

### 7. Tombstones and Deletion Barriers
- Deleting or revoking a document writes `tombstoned_at = :timestamp` into the source item and increments `manifest_generation`.
- In-flight background jobs checking `attribute_not_exists(tombstoned_at)` immediately abort and discard artifacts.
- Resurrection of tombstoned sources is strictly prevented.
- Deletion acts as an immediate hard barrier for query delivery and document viewing, overriding pinned historical snapshots.
