# ADR-001 — Baseline Publication Independent of Enrichment

## Decision
Publish baseline source-backed retrieval as soon as baseline extraction, stores, locators and minimal navigation metadata are valid. Enrichment publishes later.

## Rationale
Graph/OKF/tree enrichment improves navigation and relationship reasoning but should not gate ordinary evidence availability.

## Consequence
A source can be queryable with graph/OKF `PENDING` or `FAILED`.
