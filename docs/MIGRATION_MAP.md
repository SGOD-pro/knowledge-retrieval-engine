# KRE Refactor Migration Map

## Authoritative Architecture Bible Pin
- **Source Document:** `KRE_ARCHITECTURE_BIBLE_REFACTOR_READY_2026-10-06.md`
- **Version:** `2026-10-06 — v3`
- **SHA-256 Hash:** `4e37baf68909bd41e7e8ef0bcc4cc67bc9dec6713cb6f50766671b0f77eaf3ca`
- **Rule:** Future edits cannot alter or drop architecture decisions without an explicit ADR update and traceability record.

## Architecture Bible → Modular Documentation Traceability Matrix

| Bible Section & Topic | Primary Refactored Doc(s) | Key Architectural Decision / Contract Preserved |
|---|---|---|
| **§0, §1, §2, §71, §72** Canonical Overview & Planes | `ARCHITECTURE.md`, `WORKFLOW.md`, `diagrams/01_SYSTEM_OVERVIEW.md` | Independent baseline vs asynchronous enrichment; 4 architectural planes; master execution flow. |
| **§4, §5, §6, §7, §8, §9, §49** Ingestion & Canonical Evidence | `TECHNICAL_SPEC.md §2`, `diagrams/02_INGESTION_PUBLICATION.md`, `diagrams/03_CANONICAL_EVIDENCE.md` | Format adapters; typed CanonicalEvidence with `extraction_pipeline_version` & `quality`; extraction QA. |
| **§10, §12, §14, §15, §50** Capabilities & Knowledge Map | `TECHNICAL_SPEC.md §3`, `ADR-003`, `diagrams/04_KNOWLEDGE_MAP.md` | CapabilityManifest with typed coverage; Knowledge Map as routing hint rather than factual proof. |
| **§13, ADR-001** Baseline vs Enrichment Decoupling | `ADR-001`, `WORKFLOW.md`, `DATABASE.md` | Baseline queryable immediately; enrichment failure isolation; prepared lexical/canonical fallback. |
| **§16, §18, §19, §51** Query Contract & Routing | `TECHNICAL_SPEC.md §4`, `API.md`, `diagrams/05_QUERY_ROUTING.md` | Explicit nested `requirements` array; per-requirement capability routing; clarification on material ambiguity; authentication vs context separation; async upload & job polling contracts. |
| **§17, ADR-004** Query Model Budgets | `ADR-004`, `COST.md`, `WORKFLOW.md` | `query-default-v3` limits (generation 2, reranker 2, encoder-verify 1, embed 2, LAYA 2, expansion 1, attempts 3); answer-mode-aware reservations (0 calls for exact deterministic templates, 1 call for planning + deterministic execution, 2 calls for narrative synthesis + semantic verification; 3 calls disallowed under default); transition finishing reserve; no document embedding during Q&A. |
| **§20, §21, §29, §30, §52, §55** Discovery Fan-In & Ranking | `diagrams/06_RETRIEVAL_EXECUTION_JOIN.md`, `TECHNICAL_SPEC.md §6, §7`, `ADR-002` | Pre-fusion discovery fan-in barrier; branch status payload; deadline handling; early-stop rules; RRF only for ranked discovery; fixed bounded reranking. |
| **§26, §27, §28, §40, §52** Structured Execution & CSV/XLSX | `TECHNICAL_SPEC.md §5`, `ADR-002` | Registered dataset references; allowlisted AST; value policies (nulls, cached Excel formulas, currencies); empty/all-null vs zero; zero denominators; missing operands prohibited; durable lineage. |
| **§31, §32, §33, §38, §39, §53, §54, §60** Evidence Verification & Answer | `WORKFLOW.md`, `diagrams/07_VERIFICATION_ANSWER.md`, `TECHNICAL_SPEC.md §8, §9` | Mechanical + semantic validation; `PartialAnswerReady → AnswerMode → FinalValidation`; safe-fallback verified template/excerpts or failure; terminal exits. |
| **§34, §35, §36** LAYA Bounded Escalation | `diagrams/08_LAYA.md`, `TECHNICAL_SPEC.md §10` | Typed decision-only escalation; code validates and executes; bounded candidate and step limits. |
| **§3, §45, §47, §48, §65, §66, §67** Lifecycle & CAS Publication | `ADR-005`, `DATABASE.md`, `diagrams/09_LIFECYCLE.md` | Distinct identities (`input_source_version`, `expected_active_source_version`, `target_source_version`, `expected_manifest_generation`, `baseline_generation`, `enrichment_generation`; no source generation alias); staging v8 while v7 active; enrichment bound to exact active source version & baseline generation; CAS delta merge preserving concurrent uploads; bounded retry abort on stale/tombstoned; idempotent `JobKey`. |
| **§46** Version-Aware Cache | `MEMORY.md`, `DATABASE.md` | CacheKey includes workspace, principal scope, session hash, query hash, snapshot, versions; cache hit never bypasses access/tombstones; session persistence. |
| **§8, §11, §37, §56, §57, §58, §61, §68** Corpus Controls | `diagrams/10_LARGE_CORPUS.md`, `TECHNICAL_SPEC.md §11`, `PERFORMANCE.md` | Bounded batches, backpressure, checkpoints, retries, partitioning, deadlines, cancellation, cursor pagination for exhaustive lists, hierarchical whole-doc summaries. |
| **§69, §70, §73** Refactoring Guardrails & Gates | `MIGRATION_MAP.md`, `DEVELOPMENT.md` | 7-stage refactoring order; mandatory invariant checks; acceptance gates require executable tests before claims. |

## Existing Repository Reviewed
The current repository includes root design/evaluation docs, FastAPI routers, a unified repository facade, DynamoDB/Qdrant/Redis, ingestion adapters, providers, `modules/`, schemas, retrieval services, LangGraph orchestration, frontend workspace/chat/library/viewer/graph pages and benchmark/evaluation assets.

Current implementation also contains BM25, vector, PageIndex, OKF, graph, reranking, compression/fidelity and deterministic execution.

## Known Legacy Conflicts
1. Current query orchestration assumes staged BM25/PageIndex/vector behavior; target architecture is per-requirement capability routing.
2. Current LangGraph `_rrf_merge()` combines BM25/vector/graph chunks; target architecture keeps structured execution out of RRF and treats graph/OKF as optional enrichment/baseline fallback where ready.
3. Current OKF can participate as a pre-retrieval signal; target architecture does not require OKF for baseline readiness.
4. Existing docs contain historical phase/benchmark claims that require re-verification; do not import those claims as new acceptance evidence.
5. Existing configuration contains older provider/threshold assumptions; target contracts make readiness/coverage/budget explicit.
6. Existing direct ingestion is the current v1 path; target large-corpus workflow adds durable/bounded asynchronous processing without requiring every ordinary upload to become synchronous.

## Reuse First
FastAPI route families, repository/storage abstractions, compatible adapters, provider integrations, frontend components, benchmark utilities.

## Replace/Refactor First
Pipeline state/orchestration, planner contract, capability/snapshot registry, publication logic, structured/RRF boundary, verification interfaces, model-budget scheduler and ingestion/enrichment publication flow.

