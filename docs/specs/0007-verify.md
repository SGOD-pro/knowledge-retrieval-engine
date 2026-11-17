# Verify: Knowledge Graph enrichment (spec 0007 Slices 1 and 2, updated 2026-11-17)
_Steps derived from spec 0007 acceptance criteria. /check verify runs these; /test locks the durable ones._

## Verification Metadata
* **Tested Commit**: `6d8bebad7e41cf0ca16cf02563a436740b5218df`
* **Branch**: `re-factor`
* **Repository**: `SGOD-pro/knowledge-retrieval-engine`
* **Execution Date**: 2026-11-17
* **Environment**: Linux x86_64, Python 3.12 via uv (host Python 3.14.7), SQLite 3 with WAL journal mode, DynamoDB Local (port 8000), local FastAPI and Uvicorn server (port 8001)
* **Provider Modes**: Mocked Bedrock runtime client fixtures for CountTokens and Converse, mocked OpenRouter reranker. Zero paid provider calls made. All local mocks are documented as local test doubles and not mischaracterized as live AWS cloud verification.
* **Status**: Slice 1 and Slice 2 fully verified and approved. Slice 3 pending. Spec 0007 In Progress. Spec 0003 deferred. GRAPH_ENABLED=false preserved.

## Exact Test Commands and Results

### 1. Combined Regression Suites
Command:
```bash
cd backend && uv run pytest \
  tests/test_knowledge_graph_slice1.py \
  tests/test_knowledge_graph_slice2.py \
  tests/test_okf_slice3_1.py \
  tests/test_table_store.py -q
```
Result: **192 passed in 19.37s**
Suite breakdown:
* `tests/test_knowledge_graph_slice1.py`: 65 passed
* `tests/test_knowledge_graph_slice2.py`: 64 passed
* `tests/test_okf_slice3_1.py`: 21 passed
* `tests/test_table_store.py`: 42 passed

### 2. Local HTTP Live Verification Suites
Command 1:
```bash
cd backend && uv run python ../verify_0007_live.py
```
Result: **18 passed out of 18 (18/18)**
* Live workspace creation over HTTP (Alice workspace ws_2f1ab8e9022b) -> 201
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

## Review Records and Status
* **Original Review**: `docs/reviews/2026-11-17-re-factor-0007-slice2-acceptance.md` preserved as a historical record (Verdict: Changes requested, identifying 3 Major and 4 Minor findings in sentence splitting, calendar modalities, intervening token allowlists, and passive auxiliaries).
* **Follow Up Review**: `docs/reviews/2026-11-17-re-factor-0007-slice2-review.md` confirmed as the superseding authoritative review (Verdict: Approve, 0 blockers, 0 majors, 0 minors, 0 nits).
* **Current Status**: Slice 2 is locally verified and reviewed. Spec 0007 remains In Progress.

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
* **AC-10**: Deeply immutable PinnedSnapshotContext request local bindings. (Candidate reciprocal rank fusion pooling deferred to Slice 3).
* **AC-11**: Non destructive legacy cutover and partitioned storage routing.
* **AC-12**: DocumentSnapshot graph metadata attributes.
* **AC-13**: Benchmark comparison barrier blocking unverified claims.

## Remaining Limitations and Invariants
* Spec 0007 remains In Progress.
* Slice 3 (Pipeline reciprocal rank fusion, snapshot metadata wiring, candidate telemetry trace, and offline ablation) remains pending and was not implemented during this task.
* Spec 0003 remains proposed and deferred.
* `GRAPH_ENABLED=false` remains preserved.
* Zero paid provider calls made, zero benchmarks run, canonical corpus and historical benchmark workspaces preserved untouched.
* Storage conditions exercised across DynamoDB Local and independent SQLite WAL connections; live AWS Bedrock and DynamoDB cloud services remain unverified in local development.
