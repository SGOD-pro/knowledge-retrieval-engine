# Verify: Knowledge Graph enrichment (spec 0007 Slices 1, 2, and 3, updated 2026-11-17)
_Steps derived from spec 0007 acceptance criteria. /check verify runs these; /test locks the durable ones._

## Verification Metadata
* **Tested Commit**: Working tree HEAD (with Slice 3 implementation)
* **Branch**: `re-factor`
* **Repository**: `SGOD-pro/knowledge-retrieval-engine`
* **Execution Date**: 2026-11-17
* **Environment**: Linux x86_64, Python 3.12 via uv (host Python 3.14.7), SQLite 3 with WAL journal mode, DynamoDB Local (port 8000), local FastAPI and Uvicorn server (port 8001)
* **Provider Modes**: Mocked Bedrock runtime client fixtures for CountTokens and Converse, mocked OpenRouter reranker. Zero paid provider calls made. All local mocks are documented as local test doubles and not mischaracterized as live AWS cloud verification.
* **Status**: Slice 1, Slice 2, and Slice 3 fully verified and approved. Spec 0007 complete and approved. Spec 0003 deferred. GRAPH_ENABLED=false preserved.

## Exact Test Commands and Results

### 1. Combined Regression Suites
Command:
```bash
cd backend && uv run pytest \
  tests/test_knowledge_graph_slice1.py \
  tests/test_knowledge_graph_slice2.py \
  tests/test_knowledge_graph_ablation_slice3.py \
  tests/test_okf_slice3_1.py \
  tests/test_table_store.py -q
```
Result: **214 passed in 26.29s**
Suite breakdown:
* `tests/test_knowledge_graph_slice1.py`: 65 passed
* `tests/test_knowledge_graph_slice2.py`: 64 passed
* `tests/test_knowledge_graph_ablation_slice3.py`: 22 passed
* `tests/test_okf_slice3_1.py`: 21 passed
* `tests/test_table_store.py`: 42 passed

### 2. Local HTTP Live Verification Suites
Command 1:
```bash
cd backend && uv run python ../verify_0007_live.py
```
Result: **18 passed out of 18 (18/18)**
* Live workspace creation over HTTP (Alice workspace ws_ac389367ca17) -> 201
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

Command 2:
```bash
cd backend && uv run python ../verify_0004_live.py
```
Result: **17 passed out of 17 (17/17)**
* Workspace creation and boundary isolation -> 201
* Real CSV ingestion via API endpoint -> 201
* Deterministic aggregate query with cited result -> 200
* Large CSV ingestion (250 rows) beyond 99 row batch size -> 201
* Aggregate across 250 rows proving no truncation -> 200
* Malformed CSV ingestion recording exclusions -> 201
* Incomplete coverage blocking aggregate with HTTP 422 -> 422
* Cross workspace query and upload access denial -> 404
* Insufficient evidence returning HTTP 200 refusal with 0 citations -> 200
* Source tombstoning blocking subsequent queries with HTTP 422 -> 422
* Real text PDF ingestion via API endpoint -> 201
* Text PDF query with DocumentLocation citation -> 200
* PDF insufficient evidence returning HTTP 200 refusal -> 200
* PDF source tombstoning blocking citations -> 200

Command 3:
```bash
cd backend && uv run python ../verify_0006_live.py
```
Result: **15 passed out of 15 (15/15)**
* Workspace creation and boundary authorization -> 201
* Real CSV table ingestion with curated schema extraction -> 201
* DocumentSnapshot recording okf_status ready and extracted column count -> 200
* OkfRetriever retrieving curated table schema assertions without model tokens -> 200
* Real PDF ingestion with OKF extraction enrichment pass -> 201
* PDF DocumentSnapshot recording valid okf_status without blocking baseline ingestion -> 200
* Cross workspace query denial with HTTP 404 (0 tenant leakage) -> 404
* OkfRetriever cross tenant lookup returning zero entity matches -> 200
* Source tombstone in workspace boundary repository -> 200
* Tombstoned source assertions immediately filtered out inside worker tasks -> 200
* OkfRetriever 50ms client deadline and concurrency capacity retention -> 200
* Scoped retrieval queries routing exclusively via WORKSPACE#{ws_id} partition keys -> 200
* Administrative endpoints deferred to minimize attack surface -> 404
* Benchmark comparator barrier blocking accuracy claims on non canonical workspaces -> 200
* Live QueryResponse exposing OKF candidate telemetry trace and fusion attributes -> 200

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

## Review Records and Status
* **Original Slice 2 Review**: `docs/reviews/2026-11-17-re-factor-0007-slice2-acceptance.md` preserved as a historical record (Verdict: Changes requested).
* **Follow Up Slice 2 Review**: `docs/reviews/2026-11-17-re-factor-0007-slice2-review.md` confirmed as the superseding authoritative review (Verdict: Approve).
* **Original Slice 3 Review**: `docs/reviews/2026-11-17-re-factor-0007-slice3-review.md` preserved as a historical record (Verdict: Changes requested).
* **Follow Up Slice 3 Review**: `docs/reviews/2026-11-17-re-factor-0007-slice3-followup-review.md` confirmed as the superseding authoritative review (Verdict: Approve, 0 blockers, 0 majors, 0 minors, 0 nits).
* **Wiring Blockers Resolution**: Both Slice 3 production ingestion wiring blockers (CSV schema relations from TableStore and Bedrock provider boundary injection) repaired, covered by 9 dedicated application path tests in Section 14, and verified locally.
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
