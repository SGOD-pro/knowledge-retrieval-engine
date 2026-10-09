# Scope: Knowledge Retrieval Engine Refactor

Source grounded document intelligence engine refactoring existing components into verified, deterministic execution slices based on the authoritative architecture specification.

**Build approach:** Tracer Bullet (vertical end to end slices, thin but complete through every layer).
**Workflow:** Beta (after develop: check verify, then test). The project default level of rigor. /architect is the recommended first stop for a feature with a real decision, but skippable when you already know the build. Any feature can carry its own tag to do more or less.

_These are recommendations to keep your build orderly, not requirements. Skip anything that does not fit: if you already know how to build a feature, use /develop and skip /architect. You decide when a feature is done._

## At a glance

| # | Feature | Phase | Status |
|---|---------|-------|--------|
| A | Storage and database repositories | Existing | existing |
| B | File format adapters | Existing | existing |
| C | Model providers and environment settings | Existing | existing |
| D | Web application and document viewer shell | Existing | existing |
| E | Benchmark suite and test assets | Existing | existing |
| 1 | Contracts and identity envelope | Foundation | done |
| 2 | Workspace authorization and boundary isolation | Foundation | done |
| 3 | Snapshot registry, publication, and atomic deletion | Foundation | planned |
| 4 | Core walking skeleton | Slice 1 | planned |
| 5 | Baseline ingestion pipeline | Slice 2 | planned |
| 6 | Deterministic structured execution | Slice 3 | planned |
| 7 | Baseline discovery retrieval and fusion | Slice 4 | planned |
| 8 | Knowledge Map and requirement router | Slice 5 | planned |
| 9 | Evidence verification and answer assembly | Slice 6 | planned |
| 10 | End to end interface integration | Slice 7 | planned |
| 11 | Production authentication, workspace membership, and session persistence | Slice 8 | planned |

## Brownfield enrollment

Existing components are candidate blocks for reuse, not verified functionality. Each module must be inspected, adapted to new contracts, and tested before acceptance.

### A. Storage and database repositories · existing
Existing DynamoDB database access (`backend/src/db/database.py`), Redis caching (`backend/src/db/redis_cache.py`), SQLite and in memory TableStore implementations (`backend/src/db/table_store/`). Candidate for data layer reuse. code in `backend/src/db/`

### B. File format adapters · existing
Existing document extraction adapters for PDF, DOCX, PPTX, CSV, and XLSX (`backend/src/ingestion/adapters/`). Candidate for parser reuse. code in `backend/src/ingestion/adapters/`

### C. Model providers and environment settings · existing
Existing AWS Bedrock client, OpenRouter reranker connection, and Pydantic configuration settings (`backend/src/providers/` and `backend/src/config.py`). Candidate for model caller reuse. code in `backend/src/providers/` and `backend/src/config.py`

### D. Web application and document viewer shell · existing
Existing React 19 and Vite frontend with workspace state, document viewer, and multi pane chat shell (`frontend/src/`). Candidate for UI reuse. code in `frontend/src/`

### E. Benchmark suite and test assets · existing
Existing ground truth evaluation datasets, defect reproductions, and test cases (`backend/tests/` and `backend/evaluation_assets/`). Candidate for regression and benchmark validation. code in `backend/tests/` and `backend/evaluation_assets/`

## Foundations

### 1. Contracts and identity envelope · done
Define cross module message envelopes, typed CanonicalEvidence, CapabilityArtifact, and QueryContract with nested requirements so every module shares strict types. (basis: Architecture Bible §0, §1, §4, §16; docs/MIGRATION_MAP.md Traceability Matrix; docs/TECHNICAL_SPEC.md §1, §2, §3, §4)
**Done when:** shared Pydantic models validate identity envelopes, canonical evidence attributes, capability records, and nested requirement arrays with zero ambient types.
- [x] Design it (spec): `/architect contracts and identity envelope`
- [x] Build it: `/develop contracts and identity envelope`
   - [x] Implement IdentityEnvelope with flat serialization and lifecycle validation (AC-1)
   - [x] Implement immutable Location and ContentPayload unions with bounded table rows (AC-3, AC-4, AC-5)
   - [x] Implement CanonicalEvidence with extraction quality and provenance (AC-2, AC-6)
   - [x] Implement CapabilityArtifact with typed coverage and failure states (AC-7)
   - [x] Implement QueryContract with typed selection predicates and requirement decomposition (AC-8)
   - [x] Export package and implement serialization and immutability test suites (AC-1..8)
- [x] Verify it: `/check verify contracts and identity envelope`
- [x] Test it: `/test contracts and identity envelope`
Spec [0001](../specs/0001-contracts-and-identity-envelope.md) · code in `backend/src/schemas/contracts/`

### 2. Workspace authorization and boundary isolation · done · GA
Build core identity contracts and workspace boundary enforcement using explicit test workspace and test principal contexts through intended production interfaces. Enforce principal scoping, cache isolation, immediate deletion barriers, and cross workspace leakage tests without requiring full account management or authentication infrastructure up front. (basis: Architecture Bible §3, §46; docs/reference/SECURITY.md; docs/reference/TESTING.md)
**Done when:** requests through production interfaces validate workspace and principal identifiers, cache hits never cross workspace boundaries, deletion tombstones block access immediately, and cross workspace leakage test suites pass cleanly.
- [x] Design it (spec): `/architect workspace authorization and boundary isolation`
- [x] Build it: `/develop workspace authorization and boundary isolation`
   - [x] Core boundary models, errors, and test fixtures (AC-1, AC-5, AC-7, AC-8, AC-10, AC-14)
   - [x] Test authentication provider with production startup barrier (AC-1, AC-2, AC-5)
   - [x] API routes and service layer workspace isolation guards (AC-3, AC-4, AC-6)
   - [x] Atomic manifest generation tombstones and CAS publication retries (AC-8, AC-9)
   - [x] Cache key derivation, delivery gate verification, and fallback recomputation (AC-10, AC-11, AC-12, AC-13)
- [x] Verify it: `/check verify workspace authorization and boundary isolation`
- [x] Test it: `/test workspace authorization and boundary isolation`
- [x] Review it (fresh model): `/check review workspace authorization and boundary isolation`
- [x] Document it: `/document workspace authorization and boundary isolation`
Spec [0002](../specs/0002-workspace-authorization-and-boundary-isolation.md) · code in `backend/src/`

### 3. Snapshot registry, publication, and atomic deletion · needs a decision · GA
Implement CAS publication, baseline generation counters, active source version tracking, bounded manifest layout under DynamoDB limits, and immediate tombstone deletion barriers. (basis: Architecture Bible §3, §45, §47, §48, §65, §66, §67; docs/reference/DATABASE.md; docs/MIGRATION_MAP.md Traceability Matrix)
**Done when:** multi item atomic publication updates manifests with generation checks, reads never observe incomplete publications, and deletions block access immediately.
- [ ] Design it (spec): `/architect snapshot registry, publication, and atomic deletion`

## Slice 1: Core walking skeleton

### 4. Core walking skeleton · needs a decision
Prove the complete path works end to end on a single structured dataset using a small production compatible subset of later slices with explicit test identities through intended production interfaces. It exercises workspace boundary isolation, uses shared identity contracts, performs restricted AST execution against table storage, persists durable provenance, and renders verified results in the web client, without requiring authentication screens, workspace management UI, or throwaway parallel pipelines. (basis: Tracer Bullet principle: validate the entire pipeline connects before expanding breadth; docs/MIGRATION_MAP.md Replace/Refactor First)
**Done when:** a query with explicit test workspace and principal contexts uploads a structured file, registers a baseline snapshot, executes deterministically via restricted AST, passes boundary isolation checks, and displays verified results with durable provenance in the web client.
- [ ] Design it (spec): `/architect core walking skeleton`

## Slice 2: Baseline ingestion pipeline

### 5. Baseline ingestion pipeline · needs a decision
Harden document ingestion across PDF, DOCX, PPTX, CSV, and XLSX into typed CanonicalEvidence, with bounded batching, checkpoints, cancellation, and incomplete coverage handling, publishing a usable baseline snapshot without waiting for optional enrichment. (basis: Architecture Bible §4, §5, §6, §7, §8, §11, §13; docs/WORKFLOW.md; docs/MIGRATION_MAP.md Traceability Matrix)
**Done when:** multi format documents ingest asynchronously with extraction QA and locators, checkpoints and cancellation function cleanly, incomplete coverage is flagged, and baseline snapshots publish independently of enrichment.
- [ ] Design it (spec): `/architect baseline ingestion pipeline`

## Slice 3: Deterministic structured execution

### 6. Deterministic structured execution · needs a decision · GA
Execute tabular operations over registered CSV and XLSX datasets using an allowlisted abstract syntax tree with strict null policies and durable lineage, running outside of reciprocal rank fusion. (basis: Architecture Bible §26, §27, §28, §40, §52; docs/TECHNICAL_SPEC.md §5; docs/MIGRATION_MAP.md Traceability Matrix)
**Done when:** structured data queries execute deterministically against table stores without language model hallucination, handle empty or zero values safely, and produce mandatory execution evidence outside of reciprocal rank fusion.
- [ ] Design it (spec): `/architect deterministic structured execution`

## Slice 4: Baseline discovery retrieval and fusion

### 7. Baseline discovery retrieval and fusion · needs a decision
Retrieve lexical and dense candidates from baseline indexes, enforce a pre fusion barrier, apply cursor pagination for exhaustive lists, handle deadlines, and merge compatible discovery candidates using reciprocal rank fusion. (basis: Architecture Bible §19, §20, §21, §29, §30, §52, §55; docs/TECHNICAL_SPEC.md §6, §7; docs/MIGRATION_MAP.md Traceability Matrix)
**Done when:** queries over text chunks retrieve BM25 and vector candidates, apply deadlines and cursor pagination, merge ranked lists through reciprocal rank fusion, and apply bounded reranking without including structured evidence.
- [ ] Design it (spec): `/architect baseline discovery retrieval and fusion`

## Slice 5: Knowledge Map and requirement router

### 8. Knowledge Map and requirement router · needs a decision
Decompose user queries into explicit requirement arrays, inspect capability readiness across sources, and route each requirement to the least expensive ready path. (basis: Architecture Bible §10, §12, §14, §15, §16, §18, §19, §50, §51; docs/TECHNICAL_SPEC.md §3, §4; docs/MIGRATION_MAP.md Traceability Matrix)
**Done when:** the planner creates verifiable requirement items, routes structured requirements to execution and semantic requirements to discovery, and requests clarification whenever material ambiguity exists.
- [ ] Design it (spec): `/architect Knowledge Map and requirement router`

## Slice 6: Evidence verification and answer assembly

### 9. Evidence verification and answer assembly · needs a decision · GA
Join retrieved evidence by requirement, enforce mechanical and semantic validation under a strict query budget, and assemble grounded answers. (basis: Architecture Bible §17, §31, §32, §33, §38, §39, §53, §54, §60; docs/WORKFLOW.md; docs/TECHNICAL_SPEC.md §8, §9; docs/MIGRATION_MAP.md Traceability Matrix)
**Done when:** evidence joins by requirement identifier, passes locator and support checks, respects the query model budget ledger, and emits validated answers or explicit refusal notifications.
- [ ] Design it (spec): `/architect evidence verification and answer assembly`

## Slice 7: End to end interface integration

### 10. End to end interface integration
Connect the refactored backend execution pipeline to the React web application in test workspace mode, updating the query input, citation links, and document viewer highlights. (basis: Architecture Bible §71, §72; docs/MIGRATION_MAP.md Reuse First; frontend/AGENTS.md)
**Done when:** users in the web interface can view live ingestion status, ask multi part queries in test workspace mode, click citations to jump to exact document locations, and see clean error or refusal messages.
- [ ] Build it: `/develop end to end interface integration`

## Slice 8: Production authentication and multi user workspaces

### 11. Production authentication, workspace membership, and session persistence · needs a decision · GA
Implement real authentication provider integration, user account management, workspace ownership and membership access controls, and persistent chat sessions prior to multi user release. (basis: Architecture Bible §3, §16, §46; docs/reference/SECURITY.md; docs/reference/API.md)
**Done when:** users can authenticate through real identity providers, create and manage workspaces with explicit membership permissions, and resume persistent chat sessions with full authorization enforcement.
- [ ] Design it (spec): `/architect production authentication, workspace membership, and session persistence`

## Deferred
Out of scope for this core engine pass, kept so the plan stays honest.
* **LAYA bounded escalation**: Bounded heuristic and query expansion escalation when discovery finds no support (basis: Architecture Bible §22, §34, §35, §36; docs/MIGRATION_MAP.md Traceability Matrix) · needs a decision
* **Optional asynchronous OKF and graph enrichment**: Background PageIndex and knowledge graph construction running out of band without blocking baseline queries (basis: Architecture Bible §13, §16; docs/MIGRATION_MAP.md Traceability Matrix) · needs a decision
* **Advanced distributed scaling and optimization**: Multi node distributed partitioning, distributed worker clustering, and advanced cluster optimization (basis: Architecture Bible §8, §37, §56; docs/MIGRATION_MAP.md Traceability Matrix) · needs a decision · GA
* **Independent external security audit**: Formal third party compliance certification and independent external penetration testing (basis: docs/reference/SECURITY.md) · GA

## References

### Project sources
* `KRE_ARCHITECTURE_BIBLE_REFACTOR_READY_2026-10-06.md`: Authoritative Architecture Bible specification
* `docs/MIGRATION_MAP.md`: Traceability matrix, legacy conflicts, and refactoring order
* `docs/DEVELOPMENT.md`: Refactoring phase order and loading policies
* `docs/PROJECT.md`: Product goals, non negotiables, and documentation authority
* `docs/ARCHITECTURE.md`: Four architectural planes, canonical flow, and source of truth hierarchy
* `docs/TECHNICAL_SPEC.md`: Contracts for identity, evidence, capabilities, queries, and structured execution
* `docs/WORKFLOW.md`: Query execution lifecycle, budget policies, and evidence validation gates
* `docs/reference/SECURITY.md`: Security controls, principal scoping, and boundary rules
* `docs/reference/DATABASE.md`: Schema layouts, generation tracking, and atomic publication rules
* `docs/reference/TESTING.md`: Required contract test suites and adversarial checks

### Practices & standards
* **Tracer Bullet**: Prove a working vertical thread through every layer early, then thicken each segment
* **Foundations first**: Establish strict contracts, security boundaries, and storage state before building dependent capabilities
* **Structured execution outside ranking**: Deterministic calculations must never compete against fuzzy lexical or semantic retrieval
* **Baseline independence**: Baseline search must never wait for optional enrichment to complete

## Legend

**The decision box.** Every feature carries exactly one, the sub task whose label ends with `(spec)`. Its wording varies (`Design it (spec)` normally), so skills locate it by that `(spec)` suffix, never by an exact label. Every other box is an execution box and `/architect` never ticks one.

**Feature lifecycle**: the scope updates as a feature moves; each row is what it shows and who sets it:

| State | Set by | The feature shows |
|---|---|---|
| `planned` · needs a decision | `/scope` | one box: `Design it (spec): /architect <feature>` |
| `in-progress` (designed) | `/architect` at spec capture | `Design it` ticked; spec linked; `Build it: /develop <feature>` + 2 to 5 milestones; the tier closing boxes (`Verify it` Alpha+, `Test it` Beta+, `Review it` + `Document it` GA); any surfaced follow up enrolled |
| `in-progress` (building) | `/develop` | milestone sub boxes tick one by one; code pointer filled |
| `in-progress` (verified) | `/check verify` | `Build it` + milestones ticked; `Verify it` ticked |
| `done` | you, when you decide it is; `/sync` reconciles | boxes you ran ticked, skipped ones marked skipped; the tier last stage is the suggested point to call it done; `/sync` captures conventions |

* **Next step**: the first unticked box (always a command or a tracked milestone).
* **needs a decision**: run `/architect` first; otherwise straight to `/develop` (or `/audit` for standards & tooling). The tag drops once the spec is captured.
* **Atomic build tasks live in the spec ## Build plan, not here**: the scope carries only the milestone rollup.
* **Status**: `planned` → `in-progress` → `done`, plus `existing` (pre workflow) and `dropped` (de scoped, kept for history).
* **Approach tag**: beside a heading overrides the project default for that feature; no tag inherits it.
* **Workflow tier tag**: beside a heading sets that one feature rigor above or below the project default; no tag inherits the default. It decides the feature check boxes and each skill next suggestion.
* **Workflow**: (header line) is the project default, what runs after `/develop`: **Beta** = `/check verify` then `/test`.
* **Pointer line**: (`spec <n> · code in <path>`): the spec link added by `/architect`, the code path by `/develop`.
