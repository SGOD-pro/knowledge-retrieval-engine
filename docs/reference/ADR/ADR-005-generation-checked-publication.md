# ADR-005 — Generation-Checked Atomic Publication

## Decision
Publish immutable artifacts via generation/source-checked atomic updates.

## Rationale
Prevents stale enrichment, concurrent uploads and deletion races from rolling back active state or resurrecting deleted sources.
