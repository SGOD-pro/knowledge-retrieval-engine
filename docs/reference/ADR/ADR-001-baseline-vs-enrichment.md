# ADR-001 — Baseline Publication Independent of Enrichment

## Decision
Publish baseline source-backed retrieval (lexical, compatible vector, and structured dataset) as soon as baseline extraction, stores, locators, and minimal navigation metadata pass QA. Optional enrichment (PageIndex, OKF, and Graph) is decoupled and publishes asynchronously.

## Operational Rules & Fallback Behavior
- **Prepared Fallback Behavior:** Baseline retrieval serves as the guaranteed operational fallback. If optional enrichment or dense vector building fails or is pending, queries execute against prepared baseline lexical/canonical paths. Under no circumstances may an incomplete index or failing enrichment block baseline queries.
- **Source-Supported Graph Relations:** Graph and OKF relations are optional enrichment layers, never ungrounded factual authorities. Every graph edge must maintain source provenance (`source_id`, `source_version`, `locator`). Cross-source graph edges retain all supporting source versions; if any supporting source is updated or tombstoned, dependent edges are immediately marked unavailable until re-audited.
- **Enrichment Failure Isolation:** Failure of an enrichment job leaves the valid baseline snapshot active and queryable.

## Consequence
A source is queryable with graph/OKF in `PENDING`, `UNAVAILABLE`, or `FAILED` state without degrading core retrieval capability.

