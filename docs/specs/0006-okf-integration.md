# 0006. Bounded Open Knowledge Framework (OKF) Integration

**Date**: 2026-10-25
**Status**: Proposed

## Summary

This specification defines the bounded integration of the Open Knowledge Framework (OKF) as the next verifiable milestone after accepted 0004 and 0005. OKF extracts canonical entity properties and structured facts (such as founding dates, executive officers, revenue figures, and definitions) from ingested documents to accelerate targeted factual lookups with cited provenance. It replaces unpartitioned global DynamoDB tables with a typed, workspace scoped entity schema and repairs broken repository caller contracts. Baseline BM25 and vector discovery remain fully independent and pooled with OKF candidates so that an incorrect entity match never excludes valid baseline evidence. Baseline retrieval serves as the guaranteed operational fallback per ADR-001 so that any extraction timeout, storage failure, or missing property falls back immediately without degrading core query execution.

## Context

The repository has accepted specification 0004 (Core engine walking skeleton and benchmarks) and specification 0005 (Bounded PageIndex structural retrieval), while preserving specification 0003 (Snapshot registry, publication, and atomic deletion) as proposed and deferred. The engine supports multi tenant workspace isolation, tabular data processing without row truncation, hybrid lexical and vector retrieval, and deterministic layout navigation for PDF documents.

An audit of the existing brownfield OKF component revealed three critical architecture flaws that prevent production use:
1. Cross tenant data leakage: Persisted entities and properties in `backend/src/ingestion/okf_builder.py` write to global unpartitioned DynamoDB tables (`okf_entities`, `okf_properties`, `okf_relations`) that lack workspace identifiers in their partition keys. Entities extracted from one workspace leak into queries originating from unrelated tenant workspaces.
2. Broken runtime repository contracts: In retrieval, `backend/src/services/retrieval/okf_retriever.py` calls `self.repository.get_okf_properties()`, which does not exist on `CloudRepository`. This causes runtime exceptions during every query, swallowed silently by an empty exception block in `backend/src/services/langgraph_pipeline.py`.
3. Unbudgeted model extraction expense: Ingestion invokes unconstrained language model calls on every document batch without token budgets, timeouts, or failure isolation.

Architecture Decision Record ADR-001 (Baseline Publication Independent of Enrichment) governs this integration. ADR-001 dictates that baseline retrieval serves as the operational baseline, while optional enrichments are decoupled. Under no circumstances may an incomplete, slow, or failing OKF extraction or retrieval block baseline operations.

This specification redesigns OKF into a bounded, tenant partitioned vertical slice that repairs runtime caller contracts, enforces strict workspace partition boundaries, bounds extraction budgets during ingestion, and pools verified entity facts into reciprocal rank fusion without hard exclusions.

## Requirements

**User stories**:
- As a retrieval engineer, I want queries targeting specific entity facts (such as founding dates, headquarters, or officer names) to retrieve verified property records so that factual questions are answered with exact chunk citations.
- As a retrieval engineer, I want OKF candidates to pool into reciprocal rank fusion alongside independent BM25, vector, and PageIndex results so that an incorrect or incomplete entity match never excludes true factual evidence found by baseline retrieval.
- As a system operator, I want OKF extraction and retrieval to enforce strict token and latency budgets so that slow model calls or storage hiccups fall back immediately to baseline retrieval without delaying queries or failing document ingestion.
- As a tenant administrator, I want OKF entities and properties to be strictly partitioned by workspace identifier and document version so that proprietary organizational facts never leak across workspace boundaries or surface tombstoned content.

**Acceptance criteria**:
- **AC-1**: Ingestion extracts and persists typed `OkfEntity` and `OkfProperty` records per document version, capturing entity names, entity types, property keys, property values, value types, and source chunk identifiers with exact page provenance.
- **AC-2**: OKF ingestion extraction enforces a hard token and call budget (maximum 2 language model calls and 2000 prompt tokens per document version), extracting facts exclusively from high density introductory and summary sections. Extraction failures, provider timeouts, or malformed JSON payloads log warnings, update `DocumentSnapshot` metadata, and fail open per ADR-001 without failing document ingestion or blocking baseline snapshot creation.
- **AC-3**: OKF candidate merging preserves independent BM25 and vector discovery. Source chunks backing matched properties are pooled and soft boosted in reciprocal rank fusion with a calibrated weight (1.15) without hard excluding candidate pages or pruning baseline chunks. If OKF matches an incorrect entity, valid baseline candidates remain present in the merged pool.
- **AC-4**: When multiple documents in the same workspace yield contradictory property values (differing normalized strings or numbers) for the same entity and property key, OKF tags `property_state` as `ambiguous`, divides fusion weight across matching candidates (0.575), and preserves document and chunk provenance on each candidate so the synthesis layer can present both facts with evidence citations.
- **AC-5**: OKF entity and property storage is strictly partitioned by `workspace_id`, `document_id`, and `document_version`. Cross workspace queries return zero entity matches with zero cross tenant data leakage.
- **AC-6**: Active deletion tombstones in `WorkspaceBoundaryRepository` filter out properties belonging to deleted source documents. Tombstone checks execute inside the bounded worker task and fail closed: any check error or full workspace tombstone returns zero OKF candidates immediately without throwing unhandled exceptions.
- **AC-7**: OKF lookups enforce hard client deadlines (50ms total wall clock deadline across entity resolution, property reads, and chunk hydration) and bounded concurrency worker admission (maximum 4 concurrent workers, maximum 8 queued submissions) managed by `OkfConcurrencyController`. Properties are filtered by query terms to prevent candidate pool dilution, hydrating Chunk objects inside the worker task. Concurrency capacity is retained until underlying storage worker threads finish executing, distinguishing caller timeout from cancellation of storage work. Expired lookups drop OKF candidates cleanly, guarantee baseline fallback, and ensure zero late candidate publication.
- **AC-8**: Query entity resolution operates deterministically using normalized exact and substring matching against workspace indexed entity names, incurring zero language model completion calls and zero embedding invocations at query time.
- **AC-9**: Dedicated administrative HTTP endpoints are deferred from this slice to keep the attack surface minimal. OKF readiness and statistics are exposed exclusively through `DocumentSnapshot` metadata (`okf_status`, `okf_entities_count`, `okf_properties_count`) and `QueryResponse` debug trace (`okf_candidate_ids`, `okf_latency_ms`, `okf_match_count`).
- **AC-10**: An offline ablation comparison suite evaluates candidate page recall, precision, and latency with OKF enabled versus disabled against the repaired BM25 and vector baseline across canonical evaluation documents, including a counterexample test proving that an incorrect entity match does not prevent retrieval of the true answer page.

## Options considered

### Option 1: Bounded Tenant Partitioned OKF with Storage Safeguards (Recommended)

Refactor OKF into a bounded, tenant partitioned vertical slice. Store entities and properties in dedicated workspace scoped DynamoDB records (and SQLite in local development). Enforce strict ingestion extraction budgets (maximum 2 model calls and 2000 tokens), deterministic query entity resolution (zero query model calls), 50ms storage lookup deadlines, and soft candidate pooling into reciprocal rank fusion.

**Pros**:
- Guarantees strict multi tenant isolation with partition keys prefixed by workspace identifier.
- Bounded token budget prevents runaway extraction expenses during ingestion.
- Deterministic query entity matching incurs zero model tokens and zero cost during retrieval.
- Soft pooling preserves independent baseline discovery per ADR-001; incorrect entity matches never fail queries.
- Reuses proven concurrency controller and capacity retention patterns established in 0005.

**Cons**:
- Restricts property extraction to high density sections rather than performing full document entity relation extraction.
- Cross document entity unification is limited to exact normalized entity name matches.

### Option 2: Full Relational Knowledge Graph Extraction

Build a comprehensive entity relation graph spanning all documents using recursive model calls, multi hop link expansion, and ontology clustering.

**Pros**:
- Capable of resolving multi hop relationship queries across disparate documents.

**Cons**:
- Extreme ingestion expense with unconstrained model completion costs.
- High latency during retrieval traversals exceeding interactive query budgets.
- Severe risk of cross tenant leakage when graph edges bridge documents before single document property extraction is sound.
- Violates simplicity and vertical tracer bullet principles.

### Option 3: Rule Based Regex Entity Extractor

Extract entity properties exclusively using regex patterns and predefined dictionaries without invoking language model providers during ingestion.

**Pros**:
- Exactly zero model completion cost during ingestion.
- Completely deterministic behavior.

**Cons**:
- Brittle extraction that misses unstructured natural language facts, organizational roles, and diverse document phrasings.
- Fails to capture narrative business facts that do not follow rigid tabular or key value templates.

## Decision

**Chosen option**: Option 1: Bounded Tenant Partitioned OKF with Storage Safeguards.

We will build a bounded, tenant partitioned OKF vertical slice that extracts canonical entity properties within strict token limits during ingestion, persists them in workspace scoped storage, enforces 50ms lookup deadlines with bounded concurrency worker protection, and pools verified property candidates into reciprocal rank fusion alongside baseline discovery.

## Rationale

Option 1 provides the single sound architecture for OKF. It directly cures the three fatal flaws identified in the brownfield codebase: multi tenant data leakage, broken repository caller contracts, and unbounded extraction expenses.

By partitioning storage with `PK: WORKSPACE#{workspace_id}`, OKF prevents cross tenant leakage at the database boundary. Bounding ingestion extraction to at most 2 model calls and 2000 prompt tokens ensures predictable ingestion cost. Adhering to ADR-001 guarantees that any extraction failure fails open, allowing baseline document ingestion and vector search to succeed even if the model provider experiences rate limits.

During retrieval, deterministic entity resolution eliminates query time model costs and keeps lookup latency under 50ms. Soft reciprocal rank fusion pooling guarantees that if OKF matches an incorrect entity, valid evidence from vector and BM25 discovery on other pages remains present in the candidate pool.

## Feature design

**Data model sketch**:

```
+-----------------------------------------------------------------------------------+
| OkfEntity Record (DynamoDB / SQLite)                                              |
+-----------------------------------------------------------------------------------+
| PK: WORKSPACE#{workspace_id}                                                      |
| SK: OKF_ENTITY#{entity_id}#{document_id}#{document_version}                       |
| Attributes:                                                                       |
|   - workspace_id: string                                                          |
|   - entity_id: string (normalized lowercase, e.g. "acme_corp")                    |
|   - entity_name: string (display title, e.g. "Acme Corp")                         |
|   - entity_type: string ("organization" | "person" | "location" | "product")     |
|   - document_id: string                                                           |
|   - document_version: string                                                      |
|   - created_at: string (ISO 8601)                                                 |
+-----------------------------------------------------------------------------------+

+-----------------------------------------------------------------------------------+
| OkfProperty Record (DynamoDB / SQLite)                                            |
+-----------------------------------------------------------------------------------+
| PK: WORKSPACE#{workspace_id}                                                      |
| SK: OKF_PROP#{entity_id}#{property_key}#{document_id}#{document_version}           |
| Attributes:                                                                       |
|   - workspace_id: string                                                          |
|   - entity_id: string                                                             |
|   - entity_name: string                                                           |
|   - property_key: string (normalized attribute name, e.g. "revenue", "ceo")       |
|   - property_value: string (e.g. "$10M", "Jane Doe")                             |
|   - value_type: "string" | "number" | "date" | "boolean"                          |
|   - source_chunk_id: string                                                       |
|   - document_id: string                                                           |
|   - document_version: string                                                      |
|   - page_number: integer                                                          |
|   - confidence_score: float (0.0 to 1.0)                                          |
|   - created_at: string (ISO 8601)                                                 |
+-----------------------------------------------------------------------------------+
```

+-----------------------------------------------------------------------------------+
| OkfLookupResult Contract (Pydantic Model in src/schemas/contracts/okf.py)         |
+-----------------------------------------------------------------------------------+
| Attributes:                                                                       |
|   - target_entity: string                                                         |
|   - matched_properties: tuple[OkfProperty, ...]                                   |
|   - property_state: "unique" | "ambiguous" | "none"                               |
|   - candidate_chunk_ids: tuple[str, ...]                                          |
|   - candidate_chunks: tuple[Any, ...]                                            |
|   - execution_status: "completed" | "tombstoned" | "caller_timeout" | "saturated"  |
|   - latency_ms: float                                                             |
+-----------------------------------------------------------------------------------+
```

Each `OkfManifest` captures the complete extraction outcome for a document version:
* `workspace_id`: string
* `document_id`: string
* `document_version`: string
* `entities`: tuple of `OkfEntity`
* `properties`: tuple of `OkfProperty`
* `status`: `"ready"` | `"empty"` | `"failed"`
* `extraction_tokens`: integer
* `model_calls`: integer
* `created_at`: string (ISO 8601)

**API and internal interface surface**:

| Interface / Component | Method / Action | Key inputs | Key outputs | Auth / Isolation | Key errors |
|---|---|---|---|---|---|
| `OkfRepository` | `persist_manifest` | `workspace_id`, `manifest` | void | Partition key scoped | `StorageError` |
| `OkfRepository` | `get_entity_properties` | `workspace_id`, `entity_ids` | `list[OkfProperty]` | Partition key scoped | Empty on not found |
| `OkfBuilder` | `extract_and_persist` | `workspace_id`, `document`, `budget_tokens` | `OkfManifest` | Document principal context | Fails open per ADR-001 |
| `OkfConcurrencyController` | `run_storage_work_sync` | callable, args, `deadline_s` | `(result, status)` | Internal concurrency gate | Returns `caller_timeout` |
| `OkfRetriever` | `lookup_sync` | `workspace_id`, `query`, `target_entities`, `deadline_ms` | `OkfLookupResult` | Workspace boundary verified | Returns empty on timeout |
| Administrative API | Deferred | None | None | None | 404 Not Found |

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| Ingest Document | `OkfEntity` records | Extracted by bounded Bedrock completion call over introductory chunks |
| Ingest Document | `OkfProperty` records | Parsed from model JSON output, bound to source chunk identifiers |
| Ingest Document | `okf_status` | Derived from extraction outcome (`ready`, `empty`, `failed`, `skipped`) |
| Ingest Document | `okf_entities_count` | Computed count of unique entities persisted for document |
| Ingest Document | `okf_properties_count` | Computed count of properties persisted for document |
| Route Query | `property_state` | Computed by `OkfRetriever` comparing normalized values across documents |
| Route Query | `candidate_chunks` | Hydrated inside bounded worker task via `chunk_repo.get_all_chunks` |
| Route Query | `okf_candidate_ids` | Chunk identifiers backing matched entity properties |
| Route Query | `okf_latency_ms` | Measured wall clock duration of OKF retrieval lookup |
| Route Query | `okf_match_count` | Count of property candidate chunks pooled into rank fusion |

**Key invariants**:
- OKF lookups never invoke remote language model or embedding APIs at query time (0 tokens, $0 retrieval cost).
- OKF candidates are always soft pooled; a non empty OKF candidate set never excludes or prunes baseline BM25 and vector candidates.
- Ingestion extraction strictly caps model consumption at 2 calls and 2000 prompt tokens per document version, selecting introductory chunks from the first 5 pages.
- Non text documents (such as CSV files) record `okf_status="skipped"`, `okf_entities_count=0`, and `okf_properties_count=0` without invoking model extraction.
- Tombstone checking inside worker tasks filters out properties belonging to deleted source documents; if the entire workspace is tombstoned, OKF lookups return empty immediately.
- Properties are filtered by matching `property_key` or synonyms against query tokens; if no specific key matches, at most 2 highest confidence properties are pooled to avoid candidate pool dilution.
- Chunk objects are hydrated inside the bounded worker task within the caller deadline, preventing late publication.
- Queries executing on the fast path (`plan.fast_path == True`) bypass OKF lookups to preserve zero token low latency execution.
- Legacy pre BM25 `OKF_BOOST` in `run_bm25` is removed in favor of clean reciprocal rank fusion pooling in `langgraph_pipeline.py`.
- OKF storage items always include `workspace_id` in their partition key. Cross workspace access is blocked at the repository boundary.
- An absent, corrupt, or failed OKF extraction never causes document ingestion to fail or query execution to return an error (ADR-001).

**Security model**:
- Ingestion and query operations validate workspace authorization through existing identity checks.
- Document and workspace deletion tombstones take immediate precedence over cached or persisted OKF facts.
- Document text passed to language model extraction prompts is isolated with strict delimiter tags and JSON schema enforcement, treating source content as untrusted data.

**Configuration required**:
- `OKF_ENABLED`: boolean (default true)
- `OKF_TIMEOUT_MS`: integer (default 50)
- `OKF_MAX_EXTRACTION_TOKENS_PER_DOC`: integer (default 2000)
- `OKF_MAX_MODEL_CALLS_PER_DOC`: integer (default 2)
- `OKF_BEDROCK_MODEL_ID`: string (default "anthropic.claude-3-haiku-20240307-v1:0")
- `OKF_FUSION_WEIGHT`: float (default 1.15)

**Critical test scenarios**:
- Soft pooling discovery preservation: Query for an executive officer matches an OKF property on page 2, while the required factual answer resides in paragraph text on page 18. Verify that page 18 candidates from vector search are preserved in the merged pool and cited correctly, verifying **AC-3**.
- Ingestion budget enforcement: Ingest a 50 page document; verify that OKF extraction executes at most 2 model calls and stays within the 2000 token limit, verifying **AC-2**.
- Ingestion failure resilience (ADR-001): Inject a model provider timeout or malformed JSON during ingestion; verify that document ingestion succeeds, baseline chunks and snapshot are persisted, and `okf_status` records `failed`, verifying **AC-2**.
- Contradictory property handling: Ingest two documents where Doc A reports revenue as $10M and Doc B reports revenue as $12M. Query for revenue; verify `property_state == "ambiguous"`, candidates from both documents are pooled with divided weights, and distinct chunk provenance is preserved, verifying **AC-4**.
- Storage deadline enforcement: Inject a 100ms storage delay into OKF lookup; verify that the 50ms deadline fires, logs a warning, drops OKF candidates, and allows baseline retrieval to complete within normal latency budgets, verifying **AC-7**.
- Multi tenant boundary isolation: Attempt to lookup OKF properties using another workspace identifier; verify empty response with 0 cross tenant leakage, verifying **AC-5**.
- Tombstone enforcement: Insert a deletion tombstone for a document; verify OKF lookups immediately return empty without error, verifying **AC-6**.
- Offline ablation comparison: Run ablation harness across canonical test corpus comparing candidate page recall, precision, and latency with OKF enabled versus disabled against the repaired BM25 and vector baseline, verifying **AC-10**.

## Build plan

Following the Tracer Bullet build approach, this feature is implemented across two vertical slices:

### Slice 1: Extraction, Bounded Storage, and Isolated Lookup
- [ ] 1. **Typed OKF Schema and Partitioned Storage**: Define `OkfEntity`, `OkfProperty`, `OkfManifest`, and `OkfLookupResult` contracts in `src/schemas/contracts/okf.py`, and implement `OkfRepository` supporting partitioned DynamoDB and SQLite/memory adapters with `PK: WORKSPACE#{workspace_id}`, satisfying **AC-1**, **AC-5**.
- [ ] 2. **Bounded Ingestion Extraction and ADR-001 Fallback**: Implement `OkfBuilder` invoking Bedrock completion within strict limits (max 2 calls, 2000 tokens), handling errors gracefully by updating `DocumentSnapshot` (`okf_status`, `okf_entities_count`) while preserving baseline ingestion, satisfying **AC-1**, **AC-2**.
- [ ] 3. **Boundary and Tombstone Enforcement**: Enforce workspace partition key isolation and active tombstone checks inside worker tasks, returning empty candidates on tombstoned sources without storage errors, satisfying **AC-5**, **AC-6**.
- [ ] 4. **Deadline Enforced Lookup and Concurrency Controls**: Implement `OkfConcurrencyController` and `OkfRetriever` with 50ms total caller deadline, disabling SDK retries, bounding admission queue capacity (4 workers, 8 queue limit), retaining worker capacity until underlying storage completes, and guaranteeing zero late candidate publication, satisfying **AC-7**, **AC-8**.
- [ ] 5. **Focused Slice 1 Unit Regressions**: Add unit tests verifying schema validation, budget enforcement, ADR-001 fail open ingestion resilience, tombstone blocking, and timeout handling, satisfying **AC-1**, **AC-2**, **AC-5**, **AC-6**, **AC-7**.

### Slice 2: Candidate Fusion, Pipeline Wiring, and Offline Ablation
- [ ] 6. **Reciprocal Rank Fusion Pooling and Ambiguity Handling**: Wire OKF property lookups into `langgraph_pipeline.py:run_okf_router`, remove legacy pre BM25 `OKF_BOOST` in `run_bm25`, bypass OKF on fast path queries, pool candidate chunks into reciprocal rank fusion with calibrated weight (1.15), and divide weights on ambiguous contradictory properties, satisfying **AC-3**, **AC-4**.
- [ ] 7. **Pipeline Telemetry Exposure**: Expose `okf_candidate_ids`, `okf_latency_ms`, and `okf_match_count` in `QueryResponse.retrieval_candidates` trace, satisfying **AC-9**.
- [ ] 8. **Offline Ablation Suite**: Compare candidate page recall, precision, and latency with OKF enabled versus disabled across canonical evaluation documents, verifying counterexample resilience where an incorrect entity match preserves the true answer page, satisfying **AC-3**, **AC-10**.
- [ ] 9. **Benchmark Comparison Barrier**: Enforce comparator rules ensuring no benchmark accuracy claims are made until controlled canonical comparison run executes on `ws_canonical_comparison_0004`, satisfying **AC-10**.

## Migration plan

**Strategy**: Strangler pattern with clean boundary replacement.

**Phases**:
1. Phase 1: Deploy new partitioned `OkfRepository` and typed contracts alongside existing codebase without modifying active query routing.
2. Phase 2: Route document ingestion through new bounded `OkfBuilder`, updating `DocumentSnapshot` metadata.
3. Phase 3: Cut over `langgraph_pipeline.py:run_okf_router` from brownfield `OKFRetriever` to new bounded `OkfRetriever` under concurrency controller and deadline protection.
4. Phase 4: Retire obsolete unpartitioned global DynamoDB tables (`okf_entities`, `okf_properties`, `okf_relations`) and remove dead brownfield code in `backend/src/ingestion/okf_builder.py`.

**Rollback**:
If any regression is detected during deployment, set `OKF_ENABLED=false` to immediately deactivate OKF extraction and retrieval without requiring a code deployment, falling back entirely to baseline retrieval.

**Risks**:
- Stale brownfield global tables could be queried if legacy code paths are not completely retired. Mitigated by removing legacy imports and enforcing workspace partition checks in all repository methods.
- Language model provider rate limits during ingestion could slow document processing. Mitigated by ADR-001 non blocking fallback and strict token caps.

## Consequences

**Positive**:
- Eliminates cross tenant data leakage by partitioning all OKF records by workspace identifier.
- Repairs broken repository caller contracts in `okf_retriever.py`, eliminating swallowed runtime exceptions.
- Binds ingestion extraction to a predictable token budget, preventing unbudgeted API expenses.
- Deterministic query entity lookup adds zero language model tokens during retrieval.
- Preserves independent baseline discovery, ensuring wrong entity matches never exclude valid factual evidence.

**Negative / tradeoffs**:
- Restricting extraction to introductory and summary sections may miss minor entities mentioned only in deep document appendices.
- Model extraction during ingestion incurs small incremental Bedrock token usage compared to zero cost PageIndex structural extraction.

**Neutral**:
- Requires creating new partitioned DynamoDB tables in cloud environments and updating `DocumentSnapshot` schema with OKF metadata fields.

## Follow-up

- [ ] Update `backend/AGENTS.md` context file when implementation begins.
- [ ] Verify that LocalStack DynamoDB configuration provisions the new partitioned OKF table schema in test environments.
