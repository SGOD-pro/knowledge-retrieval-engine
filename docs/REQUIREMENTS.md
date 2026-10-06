# KRE Requirements

## Functional Requirements

### FR-1 Source Identity
Register each source with `workspace_id`, `source_id`, `source_version`, content hash, source type and access policy.

### FR-2 Immutable Versions
Replacement creates a new immutable version. Historical versions are not mutated.

### FR-3 Baseline Readiness
A source becomes queryable through applicable valid baseline capabilities without waiting for OKF/graph/tree enrichment.

### FR-4 Optional Enrichment
PageIndex/OKF/graph/summary enrichment may publish later only after its own validation and generation-compatibility checks.

### FR-5 Capability Manifest
Record availability, readiness, coverage, exclusions, artifact/model/pipeline versions and failure state.

### FR-6 Minimal Navigation Map
Baseline publication creates deterministic navigation metadata from source identities, schemas and locations.

### FR-7 Knowledge Map
Map concepts/entities/metrics/dimensions/structure/relationships to source-backed locations and ready capabilities.

### FR-8 Requirement Decomposition
Each user requirement gets a stable `requirement_id` and independent support/completeness status.

### FR-9 Per-Requirement Routing
Select the minimum sufficient authorized ready path for each requirement.

### FR-10 Structured Execution
Use registered immutable datasets, complete selections, schema/type/unit/null policies, restricted operations/formula ASTs, persistent lineage and typed mandatory execution evidence.

### FR-11 Discovery Retrieval
BM25, dense, structural/PageIndex, graph/OKF and visual retrieval return source-backed ranked candidates where applicable.

### FR-12 RRF
RRF applies only to compatible ranked discovery candidate lists.

### FR-13 Requirement Join
Join mandatory execution evidence, discovery evidence and branch status by `requirement_id`.

### FR-14 Verification
Provide mechanical and semantic verification, including validation of final generated wording.

### FR-15 Request Completeness
Independently determine whether required user elements were answered.

### FR-16 LAYA
LAYA proposes bounded routing/expansion decisions. Code validates authorization, readiness, compatibility, budgets and executes.

### FR-17 Model Budgets
All query-time model/tool work is pre-budgeted under a global ledger with typed counters.

### FR-18 Failure Semantics
Expose machine-readable invalid, ambiguous, unauthorized, unavailable, conflicted, incomplete, no-support, budget, no-progress and validation failures.

### FR-19 Lifecycle Safety
Replacement/deletion/recovery are version- and generation-aware. Stale jobs cannot roll back active state or resurrect deleted sources.

### FR-20 Cache Safety
Cache keys include relevant authorization, session, source-version, snapshot and policy identity.

## Non-Functional Requirements
- provenance and inspectability
- bounded work
- workspace isolation
- large-corpus batching/queues/backpressure/checkpoints/pagination
- measurable latency and cost
- deterministic structured operations/publication
- maintainable cross-module contracts

## Acceptance Rule
A checkbox/documentation claim is not a test result. Requirements are passed only when executable tests or benchmarks demonstrate them.
