# KRE Testing Contract

## Layers
Unit, schema/contract, integration, workflow/state, security, regression, evaluation, load/scale, provider smoke.

## Required Contract Tests
- identity/version propagation
- baseline publication without enrichment
- enrichment failure preserving baseline
- structured execution outside RRF
- requirement joins
- partial/no-support/incomplete semantics
- global model budget reservation
- final semantic validation
- deletion/publication races
- cache isolation

## Adversarial Cases
Prompt injection, malformed tables, invalid units, stale formula caches, duplicate rows, empty selections, contradictions, timeouts, repeated jobs.

No test should mark a requirement passed only because a diagram or document says so.
