# ADR-005 — Generation-Checked Atomic Publication & Concurrency Safety

## Decision
All workspace lifecycle state updates (baseline activation, enrichment extension, replacement, and deletion) use generation-checked Compare-And-Swap (CAS) atomic commits over immutable artifacts.

## Implementation Contract

### 1. Separate Generation Identities
The storage model maintains four distinct generation/version identities:
- **`manifest_generation`**: Monotonically increasing workspace manifest version.
- **`source_version`**: Immutable monotonically increasing integer identifying a specific uploaded version of a source document.
- **`baseline_generation`**: Generation of baseline stores (lexical/vector/structured) materializing a given `source_version`.
- **`enrichment_generation`**: Generation of optional enrichment (PageIndex, OKF, Graph) derived from a specific `baseline_generation`.

### 2. Expected-Generation CAS Checks
Every publication transaction must supply:
```text
expected_manifest_generation: <int>
expected_source_generation:   <int>
```
Writes are committed via database conditional updates (e.g. DynamoDB `attribute_exists` / `manifest_generation = :expected`).

### 3. Atomic Delta Merge
A publishing worker never blindly overwrites the entire workspace manifest:
1. Worker reads current manifest at generation $G$.
2. Validates that the active source version matches its expected input.
3. Computes the localized delta (e.g., adding/updating entries for `source_id`).
4. Commits the delta with condition `manifest_generation == G`, incrementing to $G+1$.

### 4. Simultaneous Uploads & Conflict Handling
When multiple uploads or enrichment jobs run concurrently:
- **Scenario:** Worker A (ingesting Doc 1) and Worker B (ingesting Doc 2) read manifest generation $G$.
- Worker A successfully commits first at $G+1$.
- Worker B’s commit fails the CAS check (`manifest_generation != G`).
- **Conflict Resolution:** Worker B catches the conditional check failure, triggers a bounded re-read of the manifest at $G+1$ (preserving Doc 1's committed delta), merges Doc 2's delta, and re-executes CAS with `expected_manifest_generation = G + 1`. Both documents are safely published without data loss.

### 5. Tombstone Checks and Deletion Barrier
- **Immediate Barrier:** Source deletion or revocation writes an atomic tombstone record into the source version set and increments `manifest_generation`.
- **Stale Job Discard:** Any in-flight background enrichment or staging job verifying expected generations detects the tombstone or source-version mismatch and immediately discards its artifacts.
- **Resurrection Prevention:** A stale job cannot overwrite a newer baseline, revert an active source version, or resurrect a tombstoned document.
- **Query Overrides:** While normal replacement allows pinned snapshots to complete, deletion/revocation acts as an immediate hard authorization barrier: final delivery checks tombstones and aborts delivery.
- **Cross-Source Graphs:** Cross-source graph edges record supporting source versions. Deleting a source invalidates dependent cross-source edges until rebuilt.

