# Verify: Knowledge Graph enrichment (spec 0007 Slices 1, 2, and 3, updated 2026-11-18)
_Steps derived from spec 0007 acceptance criteria. /check verify runs these; /test locks the durable ones._

## Verification Metadata
* **Tested Commit**: `a084ed7f83f156e17ee4ffb16e5f1d4112117c3d` (working tree with exception fallback contract repair)
* **Branch**: `re-factor`
* **Repository**: `SGOD-pro/knowledge-retrieval-engine`
* **Execution Date**: 2026-11-18
* **Environment**: Linux x86_64, Python 3.12 via uv (host Python 3.14.7), SQLite 3 with WAL journal mode, DynamoDB Local (port 8000), local FastAPI and Uvicorn server (port 8001)
* **Provider Modes**: Mocked Bedrock runtime client fixtures for CountTokens and Converse, mocked OpenRouter reranker. Zero paid provider calls made. All local mocks are documented as local test doubles and not mischaracterized as live AWS cloud verification.
* **Status**: Slice 1, Slice 2, and Slice 3 fully verified and reviewed. Spec 0007 complete. Spec 0003 deferred. GRAPH_ENABLED=false preserved.

## Exact Test Commands and Results

## Exact Test Commands and Results

### 1. Reconciled Test Evidence and Execution Runs

#### A. Newly Executed Runs (DynamoDB Local Active on Port 8000)
When the local Docker DynamoDB Local daemon (`test_ddb_local` on port 8000) is running, all storage integration tests execute directly against the local container with isolated tables and clean teardown:

Knowledge Graph Core Suites:
```bash
cd backend && uv run pytest tests/test_knowledge_graph_slice1.py tests/test_knowledge_graph_slice2.py tests/test_knowledge_graph_ablation_slice3.py -q
```
Result: **163 passed, 0 skipped in 21.97s**
Breakdown:
* `tests/test_knowledge_graph_slice1.py`: **65 passed, 0 skipped** in 4.57s
* `tests/test_knowledge_graph_slice2.py`: **64 passed, 0 skipped** in 4.96s
* `tests/test_knowledge_graph_ablation_slice3.py`: **34 passed, 0 skipped** in 12.44s

Complementary Ingestion and Isolation Suites:
```bash
cd backend && uv run pytest tests/test_workspace_boundary_isolation.py tests/test_csv_ingestion_complete.py -q
```
Result: **32 passed, 0 skipped in 11.49s**
Total across Knowledge Graph, boundary isolation, and CSV ingestion: **195 passed, 0 skipped** (65 + 64 + 34 + 32).

Connected Framework Suites:
```bash
cd backend && uv run pytest tests/test_okf_slice3_1.py tests/test_table_store.py -q
```
Result: **63 passed, 0 skipped in 11.14s**
Total across all verified suites with DynamoDB Local active: **258 passed, 0 skipped** (195 + 63).

#### B. Historical Offline Runs (DynamoDB Local Inactive or Stopped)
In prior historical verification runs when the DynamoDB Local daemon was not started, test cases requiring live DynamoDB Local were safely skipped:

Combined Suite:
```bash
cd backend && uv run pytest \
  tests/test_knowledge_graph_slice1.py \
  tests/test_knowledge_graph_slice2.py \
  tests/test_knowledge_graph_ablation_slice3.py \
  tests/test_okf_slice3_1.py \
  tests/test_table_store.py -q
```
Historical result when Slice 3 had 28 tests: **186 passed, 34 skipped in 1038.5s**
Historical breakdown when daemon was offline:
* `tests/test_knowledge_graph_slice1.py`: 62 passed, 3 skipped
* `tests/test_knowledge_graph_slice2.py`: 59 passed, 5 skipped
* `tests/test_knowledge_graph_ablation_slice3.py`: 28 passed, 0 skipped
* `tests/test_okf_slice3_1.py` & `tests/test_table_store.py`: 37 passed, 26 skipped
Breakdown sum: 62 + 59 + 28 + 37 = 186 passed, 3 + 5 + 0 + 26 = 34 skipped.

When Slice 3 expanded to 34 tests, the offline breakdown sum became:
62 + 59 + 34 + 37 = **192 passed, 34 skipped**.

#### C. Reconciliation of Discrepancies
1. **129 Graph Passes versus 121 in Breakdown**:
   * `test_knowledge_graph_slice1.py` has 65 total tests; `test_knowledge_graph_slice2.py` has 64 total tests. The combined total items collected is 129.
   * In newly executed runs with DynamoDB Local active, all 129 passed (65 + 64 = 129 passed, 0 skipped).
   * In historical runs with DynamoDB Local offline, 8 DynamoDB Local tests were skipped (3 in Slice 1, 5 in Slice 2), producing 121 passed, 8 skipped (62 + 59 = 121).
   * The earlier text erroneously mixed the newly executed total (129) with the historical offline breakdown (62 + 59 = 121). Both figures are now explicitly reconciled.
2. **195 Total versus 187 in Listed Suites**:
   * In newly executed runs with DynamoDB Local active, the total across Knowledge Graph and Boundary/CSV suites is exactly 195 (65 in Slice 1 + 64 in Slice 2 + 34 in Slice 3 + 32 in Boundary/CSV = 195 passed, 0 skipped).
   * The 187 figure was the hybrid sum obtained by mistakenly combining newly executed Slice 3 (34) and Boundary/CSV (32) with the historical offline counts for Slice 1 (62) and Slice 2 (59): 62 + 59 + 34 + 32 = 187.
3. **Combined Verification 186 versus 192 in Breakdown**:
   * The 186 total was the original historical total when Slice 3 contained 28 tests (62 + 59 + 28 + 37 = 186 passed, 34 skipped).
   * When Slice 3 was expanded to 34 tests, the offline breakdown sum became 192 (62 + 59 + 34 + 37 = 192 passed, 34 skipped).
   * In newly executed runs with DynamoDB Local active, all 34 skipped cases run and pass, producing 226 passed, 0 skipped across the five suites (65 + 64 + 34 + 57 + 6 = 226).

#### D. Specific DynamoDB Local Test Cases
The test cases that interact with the local DynamoDB container (`http://localhost:8000`) are:
* **Slice 1**:
  * `test_dynamodb_local_staging_and_publication_if_available` (fenced staging and CAS visibility commit)
  * `test_dynamodb_fencing_atomic_validation_and_rejection` (rejection of stale worker fence tokens)
  * `test_tablestore_explicit_version_absent_dynamodb_local_if_available` (version isolation)
  * Note: `test_dynamodb_repository_manifest_concurrency_stubbed` is a unit test using a stubbed client that runs in all environments.
* **Slice 2**:
  * `test_dynamodb_local_budget_ledger_atomic_reservation_and_fencing` (atomic lease acquisition, 15s expiry, monotonic fence)
  * `test_dynamodb_local_publication_cleanup_never_clears_another_workers_lease` (lease ownership checks on commit)
  * `test_dynamodb_local_failure_cleanup_never_clears_another_workers_lease` (lease ownership checks on failure)
  * `test_dynamodb_local_atomic_idempotent_reconciliation` (idempotent token reconciliation)
  * `test_durable_token_limit_accounting_across_sqlite_and_dynamodb_local` (token limit accounting across stores)
* **Slice 3**:
  * `test_recovery_on_historical_stub_with_completed_calls_and_zero_tokens[dynamodb]` (fenced recovery of historical stubs)
  * `test_recovery_fails_on_competing_publication_cas_conflict[dynamodb]` (optimistic concurrency conflict on competing publication)

**Operational Boundary**: All DynamoDB tests were conducted against local containerized DynamoDB Local. Zero calls were dispatched to live AWS Bedrock or AWS cloud DynamoDB. Live cloud readiness is not inferred from local Docker results.

### 2. Local HTTP Live Verification Suites
Command:
```bash
cd backend && uv run python ../verify_0007_live.py
```
Result: **18 passed out of 18 (18/18)**
* Live workspace creation over HTTP (Alice workspace ws_dc1b39d06519) -> 201
* Real CSV document ingestion with provenance -> 201
* DocumentSnapshot graph metadata exposure -> 200
* Deterministic tabular relationship extraction from foreign keys -> 200 (4 entities, 2 relationships)
* Directional grounding validation rejecting reversed, negated, and co occurrence claims -> 200
* Pre commit staging isolation in SQLite WAL store -> 200
* Atomic visibility commit and publication manifest persistence -> 200
* Deeply immutable PinnedSnapshotContext document bindings -> 200
* Bounded 2 hop multi hop traversal with cycle suppression and 25 node ceiling -> 200
* Targeted chunk hydration calling get_chunks_by_ids with zero get_all_chunks calls -> 200
* Source tombstone fail closed halting during traversal and hydration -> 200
* Workspace tombstone fail closed rejection returning zero candidates -> 200
* GraphConcurrencyController admission bounds and capacity retention -> 200
* Multi tenant boundary isolation denying cross workspace query with HTTP 404 (0 tenant leakage) -> 404
* KnowledgeGraphRetriever cross tenant traversal returning zero candidates -> 200
* Absence of un specced administrative endpoints -> 404
* Partitioned storage routing exclusively via KG#{workspace_id} partition keys -> 200
* Benchmark comparator barrier blocking accuracy claims on non canonical workspaces -> 200

Unavailable Checks and Safe Skips:
* `verify_0004_live.py` (steps 7 & 10) and `verify_0006_live.py` (step 11) exercise live `/query` endpoints that dispatch general queries through unmocked Bedrock / cloud provider endpoints. Per prompt constraints prohibiting paid provider calls and unmocked cloud spend, these live general query executions were safely stopped. Deterministic CSV, table aggregation, and boundary checks in `verify_0004_live.py` passed cleanly (10 passed).

## Detailed Slice 2 Coverage Confirmation

### 1. Exact Chunk Workspace, Document, and Version Provenance
* Inspected chunk and parse_service source identity persistence across attributes and metadata dictionaries.
* Document version is validated from authoritative metadata, checking metadata["version"], metadata["document_version"], and chunk attributes.
* Conflicting identity fields and unverifiable version bindings fail closed before model dispatch.
* Old or unversioned chunks are never relabeled as the requested version.
* Full workspace and document boundary isolation is enforced using actual source bindings.
* Regressions in test_knowledge_graph_slice2.py cover stale metadata versions, conflicting fields, missing bindings, valid exact version chunks, and real Chunk objects produced through parse_file.

### 2. Source Context Preservation During Grounding
* Verbatim substring presence alone is rejected as insufficient.
* Candidate evidence is located within its surrounding source sentence or clause.
* Grounding validator rejects evidence that removes conditional, negated, hypothetical, reported speech, or disputed context.
* Counterexample verified: For source text "If Acme acquired Beta, costs would rise", claimed evidence "Acme acquired Beta" is rejected, resulting in zero accepted relationships.
* Repeated evidence strings across different contexts are disambiguated, and exact accepted evidence text and location are retained.

### 3. Syntactic Binding of Actor to Predicate
* The subject is never inferred solely from entity ordering or co occurrence.
* Counterexample verified: For source text "Acme said Gamma acquired Beta", claimed edge "Acme -> acquired -> Beta" is rejected.
* Nested clauses, reported speech, multiple actors, and mixed active and passive constructions are tested.
* Conservative supported syntax rules reject uncertain attachments while preserving valid direct active and passive relationships.
* Boundary aware sentence splitting preserves corporate abbreviations like Inc., Corp., and Ltd., and numeric decimals like 51.5% and $3.5M.
* Disambiguates calendar month May from modal auxiliary may, expands allowable intervening business role nouns and temporal adverbs, and supports modal passive voice with auxiliary be.

### 4. Mandatory Token Bounds and Durable Budget Accounting
* Enforces configured per call limits (maximum 2000 prompt tokens, maximum 800 output tokens) and cumulative document ceilings (4000 prompt tokens, 1600 output tokens, 5600 total tokens).
* Rejects outputTokens=801 when maxTokens=800.
* Rejects input usage exceeding reserved allowances or configured caps.
* Validates aggregate document limits across attempts and retries.
* On overrun, retains known actual spend durably on BUDGET#LEDGER, blocks publication, and prevents further calls exceeding budget.
* Fails closed with zero model calls on missing CountTokens capability, non positive token counts, or exceptions.
* Enforces 15 second worker leases with monotonic fence tokens, safe lease takeover, crash recovery fail closed semantics, and ready publication preservation across failed new attempts.
* Storage staging errors trigger conditional lease release across InMemory, SQLite WAL, and DynamoDB Local stores.

## Detailed Slice 3 Coverage Confirmation

### 1. Pipeline Fusion and Fast Path Bypass (AC-10)
* Wired `run_graph` node in `backend/src/services/langgraph_pipeline.py` after OKF router post and before reranking.
* Enforces `GRAPH_ENABLED` setting with default `False`. When disabled, bypasses graph retrieval with zero latency and zero model tokens.
* Supports query plan routing bypass: fast path plans bypass graph lookup entirely with zero overhead.
* Calibrated reciprocal rank fusion score computation: initial graph weight set to 1.10, halved to 0.55 when entity ambiguity or edge conflicts are detected.
* Graph candidate chunks pool softly into `_rrf_merge` without displacing baseline retrieval chunks.

### 2. Decoupled Baseline Ingestion and Snapshot Metadata (AC-3, AC-11, AC-12)
* Wired graph extraction in `backend/src/modules/documents/documents_service.py` as an asynchronous background enrichment pass per ADR-001.
* Ingestion updates `DocumentSnapshot` with `graph_status`, `graph_entities_count`, and `graph_relations_count`.
* Extraction failures or timeouts log gracefully and record `graph_status="failed"` without blocking baseline document availability or core query execution.

### 3. Retrieval Candidate Telemetry Trace (AC-12)
* Exposed graph telemetry fields in `QueryResponse`: `graph_candidate_ids`, `graph_match_count`, `graph_latency_ms`, `graph_traversed_nodes`, `graph_matched_edges`, `graph_was_truncated`, `graph_truncation_reason`, `graph_entity_state`, and `graph_relationship_state`.
* `query_service.py` maps graph telemetry attributes into query response dictionaries.

### 4. Offline Ablation Comparison Suite and Counterexample Safety (AC-10, AC-13)
* Implemented comprehensive test suite in `backend/tests/test_knowledge_graph_ablation_slice3.py` (12 tests passing).
* Evaluates recall, precision, and latency comparisons with graph enabled versus disabled across canonical evaluation documents.
* Counterexample safety verified: an incorrect graph expansion on Page 2 never displaces a factual answer chunk on Page 18 in reciprocal rank fusion.
* Validated dynamic source and workspace tombstones fail closed, halting graph retrieval immediately.
* Validated client deadline and concurrency limits drop graph retrieval cleanly on caller timeout without corrupting query results.

### 5. Benchmark Comparison Precondition Barrier (AC-13)
* `validate_benchmark_comparison_preconditions` strictly enforces comparison rules: blocks benchmark accuracy claims on non canonical workspaces and requires explicit approval before executing paid benchmarks.

### 6. Production Ingestion Wiring and Provider Resolution (AC-1, AC-3, AC-5, AC-11)
* Verified CSV schema relationship resolution: `DocumentsService.background_ingest` resolves verified directional relationships from authoritative exact version `TableStore` schema metadata and passes them through to `extract_and_persist_graph`.
* Reject conflicting schema relations: cross workspace or cross version mismatches in schema metadata, manifests, or table records are rejected fail closed without creating spurious relationships.
* Ingested TableStore persistence: `ingest_csv_to_table_store` preserves foreign keys, directional schema relations, and metadata on versioned schema entries without overwriting them during header inference.
* Bedrock provider boundary wiring: `KnowledgeGraphBuilder` resolves configured Bedrock runtime client through `aws.infra.get_client("bedrock-runtime")` with support for explicit injection in tests.
* Bedrock call governance: enforces mandatory CountTokens validation, durable token budget reservations, retry bounds, deadline fencing, and atomic reconciliation before publication.
* Missing provider status: documents ingested without provider configuration record status `unavailable` or `failed` in `DocumentSnapshot` (never a successful empty extraction), while baseline document publication remains `Ready` per ADR-001.
* Valid zero relationships distinction: narrative documents with valid Bedrock responses returning zero entity relationships publish an `empty` manifest with spent tokens accounted for in the budget ledger.
* Bounded recovery: legacy stub empty manifests created by missing provider runs (zero completed calls and zero spent output tokens) are safely recovered when an authoritative provider is supplied, without re ingesting the corpus or resetting spent budgets.

### 7. Pipeline TableStore Wiring and Explicit DynamoDB Local Verification (AC-6, AC-9, AC-10, AC-11)
* Pipeline TableStore integration: `run_graph` resolves `table_store` via `get_shared_table_store()` and injects it into `KnowledgeGraphRetriever`, while supporting explicit caller injection in `PipelineState` and `Pipeline.run(...)`.
* Production pipeline path test: `test_production_pipeline_tablestore_hydration_and_evidence_fusion` tests end to end tabular retrieval through `run_graph` to `run_reranker` with document chunks absent, proving that stored table cells hydrate into `Chunk` objects and survive reranking into `retained_evidence_items` with strategy `knowledge_graph`.
* Negative controls verified: missing table rows, wrong document versions, cross workspace access, source tombstones, and unavailable TableStore each yield zero graph candidates while baseline retrieval remains intact.
* Explicit DynamoDB Local coverage: parameterized historical stub recovery tests and competing publication CAS race tests across `["memory", "sqlite", "dynamodb"]`, running against live DynamoDB Local with isolated tables and clean teardown.
* Fixed DynamoDB KnowledgeGraphRepository fence verification in `commit_manifest` to inspect authoritative top level DynamoDB item attributes (`fence_token`, `lease_owner_id`, `lease_expires_at`) instead of stale `data_json`.

### 8. Exception Fallback Contract Enforcement (AC-8, AC-10)
* Exception fallback contract alignment: In `backend/src/services/langgraph_pipeline.py`, when `retriever.expand()` raises an unexpected storage or operational exception, `run_graph` catches the exception and constructs `KnowledgeGraphLookupResult(execution_status="storage_failed", latency_ms=...)`.
* Prevented contract violation: Replaced the invalid non specced `"error"` value, preventing a Pydantic `ValidationError` from escaping and interrupting baseline query processing. The contract was not broadened.
* Preserved fail closed invariants: Returns zero graph candidates, valid failure telemetry, and preserves baseline retrieval intact through downstream `run_reranker`.
* Regression verified: `test_graph_exception_fallback_storage_failed_preserves_baseline` exercises `run_graph` and `run_reranker` when `expand()` raises a storage exception, proving no exception escapes and baseline evidence is retained.

## Review Records and Status
* **Original Slice 2 Review**: `docs/reviews/2026-11-17-re-factor-0007-slice2-acceptance.md` preserved as a historical record (Verdict: Changes requested).
* **Follow Up Slice 2 Review**: `docs/reviews/2026-11-17-re-factor-0007-slice2-review.md` confirmed as the superseding authoritative review (Verdict: Approve).
* **Original Slice 3 Review**: `docs/reviews/2026-11-17-re-factor-0007-slice3-review.md` preserved as a historical record (Verdict: Changes requested).
* **Follow Up Slice 3 Review**: `docs/reviews/2026-11-17-re-factor-0007-slice3-followup-review.md` confirmed as the superseding authoritative review (Verdict: Approve, 0 blockers, 0 majors, 0 minors, 0 nits).
* **Ingestion and Verification Gaps Resolution Review**: `docs/reviews/2026-11-18-re-factor-0007-slice3-ingestion-and-verification-gaps-resolved.md` confirmed as the superseding authoritative review (Verdict: Approve).
* **Pipeline Wiring and DynamoDB Coverage Resolution Review**: `docs/reviews/2026-11-18-re-factor-0007-slice3-pipeline-wiring-and-dynamodb-coverage-resolved.md` confirmed as the superseding authoritative review (Verdict: Approve).
* **Exception Fallback Contract Resolution Review**: `docs/reviews/2026-11-18-re-factor-0007-slice3-exception-fallback-contract-resolved.md` confirmed as the superseding authoritative review (Verdict: Approve).
* **Evidence Reconciliation Review**: `docs/reviews/2026-11-18-re-factor-0007-evidence-reconciliation-review.md` confirmed as the superseding authoritative review (Verdict: Approve).
* **Wiring and Ingestion Gaps Resolution**: All 0007 Slice 3 production ingestion wiring, TableStore pipeline integration, explicit DynamoDB Local coverage, exception fallback contract enforcement, and verification gaps repaired, covered by 34 ablation and application path tests, and verified locally. All historical test count discrepancies reconciled.
* **Current Status**: Slice 1, Slice 2, and Slice 3 are locally verified and approved. Spec 0007 implementation is complete and approved. Spec 0007 remains In Progress until merged.

## Acceptance Criteria Traceability
* **AC-1**: Grounded Narrative PDF Extraction and tabular extraction.
* **AC-2**: Verbatim directional syntax and context preservation grounding.
* **AC-3**: Token budget accounting, Bedrock call limits, and retry bounds.
* **AC-4**: Durable budget ledger, publication staging isolation, and atomic commit.
* **AC-5**: Mandatory Bedrock CountTokens validation and request bounding.
* **AC-6**: Workspace partitioned storage and bounded retrieval.
* **AC-7**: Dynamic source and workspace tombstone enforcement fail closed.
* **AC-8**: GraphConcurrencyController deadlines and capacity retention.
* **AC-9**: Bounded multi hop traversal and targeted chunk hydration.
* **AC-10**: Deeply immutable PinnedSnapshotContext request local bindings, fast path bypass, and calibrated reciprocal rank fusion candidate pooling.
* **AC-11**: Decoupled background enrichment pass, non destructive legacy cutover, and partitioned storage routing.
* **AC-12**: DocumentSnapshot graph metadata attributes and QueryResponse retrieval candidate telemetry trace.
* **AC-13**: Offline ablation comparison harness, counterexample discovery safety, and benchmark comparison barrier blocking unverified claims.

## Remaining Limitations and Invariants
* Spec 0007 implementation is complete and ready for code review.
* Spec 0003 remains proposed and deferred.
* `GRAPH_ENABLED=false` remains preserved by default; enabled selectively in isolated test suites via configuration overrides.
* Zero paid provider calls made, zero benchmarks run, canonical corpus and historical benchmark workspaces preserved untouched.
* Storage conditions exercised across DynamoDB Local and independent SQLite WAL connections; live AWS Bedrock and DynamoDB cloud services remain unverified in local development.
