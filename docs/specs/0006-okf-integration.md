# 0006. Bounded Open Knowledge Framework (OKF) Integration

**Date**: 2026-10-25
**Status**: Accepted

## Summary

This specification defines the bounded integration of the Open Knowledge Framework (OKF) as the next verifiable milestone after accepted 0004 and 0005. OKF extracts canonical entity properties, curated schema definitions, and structured facts from ingested text PDF and tabular CSV documents to accelerate targeted factual lookups with cited provenance. It replaces unpartitioned global DynamoDB tables with a typed, workspace scoped assertion schema and repairs broken repository caller contracts. Grounding validation rejects unsupported model claims by verifying extracted values against authorized source chunks and locators. Baseline BM25, vector discovery, and TableStore deterministic aggregate execution remain fully independent of OKF. Baseline retrieval serves as the guaranteed operational fallback per ADR-001 so that any extraction timeout, storage failure, or missing property falls back immediately without degrading core query execution.

## Context

The repository has accepted specification 0004 (Core engine walking skeleton and benchmarks) and specification 0005 (Bounded PageIndex structural retrieval), while preserving specification 0003 (Snapshot registry, publication, and atomic deletion) as proposed and deferred. The engine supports multi tenant workspace isolation, tabular data processing without row truncation, hybrid lexical and vector retrieval, and deterministic layout navigation for PDF documents.

An audit of the existing brownfield OKF component revealed three critical architecture flaws that prevent production use:
1. Cross tenant data leakage: Persisted entities and properties in `backend/src/ingestion/okf_builder.py` write to global unpartitioned DynamoDB tables (`okf_entities`, `okf_properties`, `okf_relations`) that lack workspace identifiers in their partition keys. Entities extracted from one workspace leak into queries originating from unrelated tenant workspaces.
2. Broken runtime repository contracts: In retrieval, `backend/src/services/retrieval/okf_retriever.py` calls `self.repository.get_okf_properties()`, which does not exist on `CloudRepository`. This causes runtime exceptions during every query, swallowed silently by an empty exception block in `backend/src/services/langgraph_pipeline.py`.
3. Unbudgeted model extraction expense: Ingestion invokes unconstrained language model calls on every document batch without token budgets, timeouts, or failure isolation.

Architecture Decision Record ADR-001 (Baseline Publication Independent of Enrichment) governs this integration. ADR-001 dictates that baseline retrieval serves as the operational baseline, while optional enrichments are decoupled. Under no circumstances may an incomplete, slow, or failing OKF extraction or retrieval block baseline operations.

This specification redesigns OKF into a bounded, tenant partitioned vertical slice that repairs runtime caller contracts, enforces strict workspace partition boundaries, restores curated table and schema support without blanket skipping CSV documents, validates assertions against authorized source chunks, bounds extraction budgets during ingestion, and pools verified entity facts into reciprocal rank fusion without hard exclusions.

## Requirements

**User stories**:
- As a retrieval engineer, I want queries targeting specific entity facts or curated table schemas to retrieve verified property records so that factual questions are answered with exact chunk citations.
- As a retrieval engineer, I want OKF candidates to pool into reciprocal rank fusion alongside independent BM25, vector, and PageIndex results so that an incorrect or incomplete entity match never excludes true factual evidence found by baseline retrieval.
- As a retrieval engineer, I want tabular CSV documents to provide curated schema, header, and column metadata without duplicating deterministic aggregate calculations performed over TableStore data rows.
- As a system operator, I want OKF extraction and retrieval to enforce strict token, call, and latency budgets so that slow model calls or storage hiccups fall back immediately to baseline retrieval without delaying queries or failing document ingestion.
- As a tenant administrator, I want OKF entities and assertions to be strictly partitioned by workspace identifier and document version so that proprietary organizational facts never leak across workspace boundaries or surface tombstoned content.

**Acceptance criteria**:
- **AC-1**: Ingestion extracts and persists typed `OkfEntityRecord` and `OkfPropertyAssertion` records per document version for both text PDF documents and tabular CSV documents. For tabular CSV files, OKF extracts curated table schemas, column names, data types, and header definitions deterministically without model costs, while preserving TableStore deterministic aggregate execution outside reciprocal rank fusion.
- **AC-2**: Extracted assertions are validated against authorized source chunks and locators before persistence. Candidate assertions must cite a chunk belonging to the active document version, possess a valid `DocumentLocation` or `TableLocation`, and contain the asserted property value verbatim within the source chunk text. Model confidence scores and model supplied chunk identifiers are treated as unverified claims; unsupported or hallucinated assertions are rejected and omitted from storage.
- **AC-3**: OKF ingestion extraction enforces strict resource bounds per document version: maximum 2 Bedrock completion calls, maximum 4000 total prompt input tokens (capped at 2000 per call), maximum 1200 total output tokens (capped at 600 per call), and a 10.0s total provider deadline per call (3.0s connect, 7.0s read). Retries are capped at at most 1 retry on throttling (HTTP 429), with zero retries on model errors, bad requests, or timeouts. Extraction is idempotent, keyed by `idempotency_key = sha256(f"{workspace_id}:{document_id}:{document_version}")`; replaying an existing version reuses persisted assertions with zero additional model calls. Baseline publication completes independently of OKF extraction per ADR-001. Note on cross process coordination and framing allowance: Attempt reservations are persisted durably before provider calls, surviving single worker crashes and restarts without resetting the document version budget. Budget recovery uses strongly consistent base table reads (ConsistentRead=True in DynamoDB) to avoid replication lag on restart. In process locks coordinate threads within a single Python process; however, cross process coordination across multiple processes on one host or across multiple hosts remains an open limitation pending distributed leasing infrastructure or atomic conditional writes. The 32 token envelope framing allowance remains an unverified assumption requiring direct provider and model tokenizer evidence.
- **AC-4**: Distinct assertions are preserved using stable content and provenance identities (`assertion_id = sha256(f"{workspace_id}:{document_id}:{document_version}:{entity_id}:{property_key}:{property_value}:{source_chunk_id}")[:24]`). When multiple documents assert differing values for the same entity and property key, both assertions persist without silent overwrite or premature unification. In retrieval, conflicting values are tagged with `property_state = "conflicting"`, candidates from all matching documents are returned with distinct chunk provenance, and fusion weight is divided (0.575 each).
- **AC-5**: Same name entities across different documents or sections retain distinct document scoped identifiers (`entity_id = sha256(f"{workspace_id}:{document_id}:{normalized_name}")[:16]`). When a query matches an entity name shared by multiple distinct entities, retrieval marks `entity_state = "ambiguous"`, returns assertions from all matching entities with explicit document provenance, and divides fusion weight without premature entity merging.
- **AC-6**: OKF storage and lookups are strictly partitioned by `workspace_id`. Queries execute via indexed access paths (GSI1 for entity resolution with prefix and exact matching; GSI2 for document version readiness and scoping) with bounded page limits (10 to 50 items), avoiding workspace wide scans. Cross workspace queries return zero entity matches with zero cross tenant data leakage.
- **AC-7**: Active deletion tombstones in `WorkspaceBoundaryRepository` filter out assertions belonging to deleted source documents. Tombstone checks execute inside the bounded worker task and fail closed: any check error or full workspace tombstone returns zero OKF candidates immediately without throwing unhandled exceptions.
- **AC-8**: OKF lookups enforce hard client deadlines (50ms total wall clock deadline across entity resolution, property reads, and chunk hydration) and bounded concurrency worker admission (maximum 4 concurrent workers, maximum 8 queued submissions) managed by `OkfConcurrencyController`. Concurrency capacity is retained until underlying storage worker threads finish executing, distinguishing caller timeout from cancellation of storage work. Expired lookups drop OKF candidates cleanly, guarantee baseline fallback, and ensure zero late candidate publication.
- **AC-9**: Properties are filtered by matching `property_key` or synonyms against query tokens; if no specific property key matches, at most 2 highest confidence properties are pooled to prevent candidate pool dilution. Source chunks backing matched properties are pooled into reciprocal rank fusion with a calibrated weight (1.15) without hard candidate page exclusions or chunk pruning. Queries executing on the fast path (`plan.fast_path == True`) bypass OKF lookups. Legacy pre BM25 `OKF_BOOST` in `run_bm25` is removed.
- **AC-10**: Non destructive legacy cutover: Scoped retrieval in `OkfRetriever` queries exclusively through the new workspace partitioned table (`kre_okf_facts`), excluding old global unpartitioned records by construction. Legacy tables (`okf_entities`, `okf_properties`, `okf_relations`) and old data are preserved without modification, and deletion is explicitly deferred.
- **AC-11**: Dedicated administrative HTTP endpoints are deferred from this slice. OKF readiness, entity count, and property count are exposed on `DocumentSnapshot` (`okf_status`, `okf_entities_count`, `okf_properties_count`), while candidate chunk identifiers, latency, and match counts are exposed in `QueryResponse.retrieval_candidates` debug trace.
- **AC-12**: An offline ablation comparison suite evaluates candidate page recall, precision, and latency with OKF enabled versus disabled against the repaired BM25 and vector baseline across canonical evaluation documents, including a counterexample test proving that an incorrect entity match does not prevent retrieval of the true answer page.

## Options considered

### Option 1: Bounded Tenant Partitioned OKF with Grounding Validation and Curated Tables (Recommended)

Refactor OKF into a bounded, tenant partitioned vertical slice. Store assertions in dedicated workspace scoped DynamoDB records (and SQLite in local development). Support both text PDF documents and tabular CSV documents with curated schema and header facts. Validate extracted assertions against authorized source chunks before persistence, rejecting hallucinated claims. Enforce strict ingestion extraction budgets (maximum 2 model calls, 4000 input tokens, 1200 output tokens), deterministic query entity resolution (zero query model calls), 50ms storage lookup deadlines, and soft candidate pooling into reciprocal rank fusion. Exclude legacy global tables without destructive deletion.

**Pros**:
- Guarantees strict multi tenant isolation with partition keys prefixed by workspace identifier.
- Restores curated table, column, and header discovery for CSV files without duplicating deterministic aggregate math.
- Grounding verification eliminates hallucinated model assertions before storage.
- Bounded token budgets and idempotency prevent runaway extraction expenses during ingestion.
- Deterministic query entity matching incurs zero model tokens and zero cost during retrieval.
- Soft pooling preserves independent baseline discovery per ADR-001; incorrect entity matches never fail queries.
- Non destructive legacy cutover avoids schema migration downtime or data loss risks.

**Cons**:
- Restricts text property extraction to high density sections rather than performing full document recursive relation extraction.
- Cross document entity unification is restricted to explicit matching and flags ambiguities rather than performing automated global clustering.

### Option 2: Full Relational Knowledge Graph Extraction

Build a comprehensive entity relation graph spanning all documents using recursive model calls, multi hop link expansion, and ontology clustering.

**Pros**:
- Capable of resolving multi hop relationship queries across disparate documents.

**Cons**:
- Extreme ingestion expense with unconstrained model completion costs.
- High latency during retrieval traversals exceeding interactive query budgets.
- Severe risk of cross tenant leakage when graph edges bridge documents before single document property extraction is sound.
- Violates simplicity and vertical tracer bullet principles.

### Option 3: Rule Based Regex Entity Extractor Only

Extract entity properties exclusively using regex patterns and predefined dictionaries without invoking language model providers for any document format.

**Pros**:
- Exactly zero model completion cost during ingestion.
- Completely deterministic behavior.

**Cons**:
- Brittle extraction that misses unstructured natural language facts, organizational roles, and diverse document phrasings in narrative text.
- Fails to capture narrative business facts that do not follow rigid tabular or key value templates.

## Decision

**Chosen option**: Option 1: Bounded Tenant Partitioned OKF with Grounding Validation and Curated Tables.

We will build a bounded, tenant partitioned OKF vertical slice that extracts canonical entity properties within strict token limits for text PDFs, extracts curated schema facts deterministically for CSV tables, validates assertions against authorized source chunks, persists them in workspace scoped storage, enforces 50ms lookup deadlines with bounded concurrency worker protection, and pools verified property candidates into reciprocal rank fusion alongside baseline discovery.

## Rationale

Option 1 directly cures the three fatal flaws identified in the brownfield codebase: multi tenant data leakage, broken repository caller contracts, and unbounded extraction expenses.

By partitioning storage with `PK: WORKSPACE#{workspace_id}`, OKF prevents cross tenant leakage at the database boundary. Bounding ingestion extraction to at most 2 model calls, 4000 input tokens, and 1200 output tokens ensures predictable ingestion cost. Adhering to ADR-001 guarantees that any extraction failure fails open, allowing baseline document ingestion and vector search to succeed even if the model provider experiences rate limits.

Restoring curated table and schema support for CSV files provides structured metadata discovery for tabular datasets without duplicating the exact arithmetic performed by `DeterministicExecutor`. Validating candidate assertions against authorized source chunks ensures that only grounded, verifiable facts enter the index.

During retrieval, deterministic entity resolution eliminates query time model costs and keeps lookup latency under 50ms. Soft reciprocal rank fusion pooling guarantees that if OKF matches an incorrect entity, valid evidence from vector and BM25 discovery on other pages remains present in the candidate pool.

## Feature design

**Data model sketch**:

```
+-----------------------------------------------------------------------------------+
| OkfEntityRecord (DynamoDB / SQLite)                                               |
+-----------------------------------------------------------------------------------+
| PK: WORKSPACE#{workspace_id}                                                      |
| SK: ENTITY#{entity_id}#DOC#{document_id}#VER#{document_version}                   |
| GSI1PK: WORKSPACE#{workspace_id}                                                  |
| GSI1SK: ENTITY_NAME#{normalized_name}                                             |
| Attributes:                                                                       |
|   - workspace_id: string                                                          |
|   - entity_id: string (sha256(ws_id:doc_id:normalized_name)[:16])                 |
|   - entity_name: string (display title, e.g. "Acme Corp" or "survay")             |
|   - normalized_name: string (lowercase stripped, e.g. "acme corp" or "survay")    |
|   - entity_type: string ("organization" | "person" | "location" | "table")        |
|   - document_id: string                                                           |
|   - document_version: string                                                      |
|   - created_at: string (ISO 8601)                                                 |
+-----------------------------------------------------------------------------------+

+-----------------------------------------------------------------------------------+
| OkfPropertyAssertion Record (DynamoDB / SQLite)                                   |
+-----------------------------------------------------------------------------------+
| PK: WORKSPACE#{workspace_id}                                                      |
| SK: ENTITY#{entity_id}#ASSERTION#{assertion_id}                                   |
| GSI2PK: WORKSPACE#{workspace_id}#DOC#{document_id}                                |
| GSI2SK: VER#{document_version}#ASSERTION#{assertion_id}                           |
| Attributes:                                                                       |
|   - assertion_id: string (sha256(ws:doc:ver:entity:key:val:chunk_id)[:24])        |
|   - workspace_id: string                                                          |
|   - entity_id: string                                                             |
|   - entity_name: string                                                           |
|   - property_key: string (normalized attribute name, e.g. "revenue" or "column:Year")|
|   - property_value: string (e.g. "$10M" or "integer")                             |
|   - value_type: "string" | "number" | "date" | "boolean" | "schema_type"           |
|   - source_chunk_id: string                                                       |
|   - document_id: string                                                           |
|   - document_version: string                                                      |
|   - page_number: integer | null (for PDF)                                         |
|   - table_name: string | null (for CSV/tables)                                    |
|   - column_name: string | null (for CSV/tables)                                   |
|   - confidence_score: float (0.0 to 1.0)                                          |
|   - validation_status: "verified"                                                 |
|   - created_at: string (ISO 8601)                                                 |
+-----------------------------------------------------------------------------------+

+-----------------------------------------------------------------------------------+
| OkfLookupResult Contract (Pydantic Model in src/schemas/contracts/okf.py)         |
+-----------------------------------------------------------------------------------+
| Attributes:                                                                       |
|   - target_entity: string                                                         |
|   - entity_state: "unique" | "ambiguous" | "none"                                 |
|   - property_state: "unique" | "conflicting" | "none"                             |
|   - matched_assertions: tuple[OkfPropertyAssertion, ...]                          |
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
* `entities`: tuple of `OkfEntityRecord`
* `assertions`: tuple of `OkfPropertyAssertion`
* `rejected_assertions_count`: integer
* `status`: `"ready"` | `"empty"` | `"failed"`
* `extraction_tokens`: integer
* `model_calls`: integer
* `idempotency_key`: string
* `created_at`: string (ISO 8601)

**Exact OkfRepository Interface and Methods**:

The `OkfRepository` defines exact access methods, enforcing partition scoping, bounded limits, and pagination:

```python
class OkfRepository:
    def save_manifest(self, workspace_id: str, manifest: OkfManifest) -> None:
        """Atomically persist manifest entities and validated assertions under workspace PK."""
        ...

    def resolve_entities(
        self,
        workspace_id: str,
        candidate_names: Sequence[str],
        limit: int = 10,
    ) -> list[OkfEntityRecord]:
        """Query GSI1 by normalized name prefix or exact match with bounded limit. Avoids scans."""
        ...

    def get_assertions_for_entity(
        self,
        workspace_id: str,
        entity_id: str,
        property_key: str | None = None,
        active_document_ids: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[OkfPropertyAssertion]:
        """Query Main table by PK=WORKSPACE#{ws_id} and SK begins_with ENTITY#{entity_id}."""
        ...

    def has_ready_okf(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
    ) -> bool:
        """Query GSI2 with Limit=1 to verify whether active OKF assertions exist."""
        ...

    def list_assertions_by_document(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        limit: int = 50,
        next_token: str | None = None,
    ) -> tuple[list[OkfPropertyAssertion], str | None]:
        """Paginated query over GSI2 for document assertions with bounded limit."""
        ...
```

**Curated Table, Header, and Schema Support**:
- Tabular CSV files are supported in Slice 1 alongside text PDFs.
- When a CSV document is ingested, `OkfBuilder` inspects the TableStore manifest and header chunks:
  - Generates an `OkfEntityRecord` for the table dataset (e.g. `entity_name="survay"`, `entity_type="table"`).
  - Generates `OkfPropertyAssertion` records for each column header (e.g. `property_key="column:Year"`, `property_value="integer"`), temporal range, and primary key definitions.
  - Locators attach to `TableLocation(table_name=..., column_name=...)` and header chunk IDs.
  - Extraction runs deterministically with 0 language model calls and 0 token cost.
  - Deterministic aggregate queries (SUM, AVG, COUNT, MIN, MAX over data rows) continue to execute through `DeterministicExecutor` outside reciprocal rank fusion and are never duplicated in OKF.

**Assertion Grounding and Locator Validation**:
- Model extraction output is treated as unverified. Before any assertion is accepted into `OkfManifest`, it must pass three checks:
  1. Authorized source chunk membership: `source_chunk_id` must exist within the active document chunks passed to the builder for `(workspace_id, document_id, document_version)`.
  2. Locator verification: Chunk must have a valid `DocumentLocation` (page number, character offset) or `TableLocation` (table name, column name).
  3. Grounding text match: The extracted `property_value` (or its normalized alphanumeric token sequence) must appear verbatim in `chunk.content`.
  4. Unsupported assertions failing any check are rejected, logged, and omitted from persistence, incrementing `rejected_assertions_count`.

**Assertion Stability, Ambiguity, and Conflict Handling**:
- Stable assertion identity: `assertion_id = sha256(f"{workspace_id}:{document_id}:{document_version}:{entity_id}:{property_key}:{property_value}:{source_chunk_id}")[:24]`. Multiple documents stating the same fact persist separate assertions with distinct chunk provenance without overwriting each other.
- Same name entity ambiguity: Distinct entities sharing a name in different documents retain distinct `entity_id`s. Queries matching multiple entities return `entity_state = "ambiguous"`, include assertions from all candidates with distinct document provenance, and divide fusion weights (0.575) without premature entity merging.
- Conflicting values: When multiple assertions exist for the same entity and property key with differing normalized values, both assertions are returned with distinct chunk IDs and document versions, tagged with `property_state = "conflicting"`, and their fusion weights are divided so synthesis can present both values with exact source citations.

**Ingestion Budgets, Token Caps, and Idempotency**:
- Bedrock provider limits per document version:
  - Input tokens: Maximum 2000 prompt tokens per call, capped at 4000 total input tokens per document version.
  - Output tokens: Maximum 600 completion tokens per call, capped at 1200 total output tokens per document version.
  - Model calls: Maximum 2 Bedrock completion calls per document version.
  - Provider deadline: 10.0s total wall clock deadline per call (3.0s connect timeout, 7.0s read timeout).
  - Retries: Maximum 1 retry on throttling (HTTP 429 / ThrottlingException) with 2.0s backoff; 0 retries on model errors, bad requests, or timeouts.
  - Idempotency and replay: Keyed by `idempotency_key = sha256(f"{workspace_id}:{document_id}:{document_version}")`. Re ingestion replays existing assertions with 0 model calls and 0 tokens.
  - ADR-001 decoupling: Baseline document chunking, embeddings, TableStore row persistence, and baseline `DocumentSnapshot` publish immediately and independently. OKF extraction executes as a decoupled enrichment pass. If OKF times out or fails, `DocumentSnapshot` records `okf_status="failed"` without blocking baseline publication.

**Non Destructive Legacy Cutover**:
- Legacy global tables (`okf_entities`, `okf_properties`, `okf_relations`) and old unpartitioned records are preserved untouched in DynamoDB.
- No table drop or delete item operations are performed on legacy tables during this slice.
- Scoped retrieval in `OkfRetriever` and `OkfRepository` routes exclusively through new workspace scoped partitions (`PK = WORKSPACE#{workspace_id}`).
- Legacy global records are excluded from retrieval by construction because they lack workspace partition keys.
- Deletion of legacy tables and data is explicitly deferred to future maintenance windows.

**API and internal interface surface**:

| Interface / Component | Method / Action | Key inputs | Key outputs | Auth / Isolation | Key errors |
|---|---|---|---|---|---|
| `OkfRepository` | `save_manifest` | `workspace_id`, `manifest` | void | Partition key scoped | `StorageError` |
| `OkfRepository` | `resolve_entities` | `workspace_id`, `candidate_names`, `limit` | `list[OkfEntityRecord]` | GSI1 scoped query | Empty on not found |
| `OkfRepository` | `get_assertions_for_entity` | `workspace_id`, `entity_id`, `key`, `docs`, `limit` | `list[OkfPropertyAssertion]` | Partition key scoped | Empty on not found |
| `OkfRepository` | `has_ready_okf` | `workspace_id`, `doc_id`, `doc_ver` | boolean | GSI2 scoped query | False on not found |
| `OkfBuilder` | `extract_and_persist` | `workspace_id`, `document`, `budget` | `OkfManifest` | Document principal context | Fails open per ADR-001 |
| `OkfConcurrencyController` | `run_storage_work_sync` | callable, args, `deadline_s` | `(result, status)` | Internal concurrency gate | Returns `caller_timeout` |
| `OkfRetriever` | `lookup_sync` | `workspace_id`, `query`, `target_entities`, `deadline_ms` | `OkfLookupResult` | Workspace boundary verified | Returns empty on timeout |
| Administrative API | Deferred | None | None | None | 404 Not Found |

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| Ingest PDF | `OkfEntityRecord` records | Extracted by bounded Bedrock call over introductory chunks |
| Ingest PDF | `OkfPropertyAssertion` records | Parsed from model JSON output, validated against source chunk text |
| Ingest CSV | Curated `OkfEntityRecord` records | Extracted deterministically from TableStore manifest table names |
| Ingest CSV | Curated `OkfPropertyAssertion` records | Extracted deterministically from TableStore schema column headers |
| Ingest Document | `okf_status` | Derived from extraction outcome (`ready`, `empty`, `failed`) |
| Ingest Document | `okf_entities_count` | Computed count of unique entities persisted for document |
| Ingest Document | `okf_properties_count` | Computed count of validated assertions persisted for document |
| Route Query | `entity_state` | Computed by `OkfRetriever` evaluating matching entities (`unique`, `ambiguous`, `none`) |
| Route Query | `property_state` | Computed by `OkfRetriever` comparing normalized values across documents (`unique`, `conflicting`, `none`) |
| Route Query | `candidate_chunks` | Hydrated inside bounded worker task via `chunk_repo.get_all_chunks` |
| Route Query | `okf_candidate_ids` | Chunk identifiers backing matched entity assertions |
| Route Query | `okf_latency_ms` | Measured wall clock duration of OKF retrieval lookup |
| Route Query | `okf_match_count` | Count of property candidate chunks pooled into rank fusion |

**Key invariants**:
- OKF lookups never invoke remote language model or embedding APIs at query time (0 tokens, $0 retrieval cost).
- OKF candidates are always soft pooled; a non empty OKF candidate set never excludes or prunes baseline BM25 and vector candidates.
- Ingestion extraction strictly caps model consumption at 2 calls, 4000 input tokens, and 1200 output tokens per document version.
- Extracted assertions failing chunk membership, locator, or verbatim grounding validation are rejected as unsupported claims.
- Curated table schemas and headers for CSV files extract deterministically without language model calls; deterministic aggregations over TableStore rows remain separate outside reciprocal rank fusion.
- Properties are filtered by matching `property_key` or synonyms against query tokens; if no specific key matches, at most 2 highest confidence properties are pooled to avoid candidate pool dilution.
- Chunk objects are hydrated inside the bounded worker task within the caller deadline, preventing late publication.
- Queries executing on the fast path (`plan.fast_path == True`) bypass OKF lookups to preserve zero token low latency execution.
- Legacy pre BM25 `OKF_BOOST` in `run_bm25` is removed in favor of clean reciprocal rank fusion pooling in `langgraph_pipeline.py`.
- Non destructive legacy cutover leaves old global tables untouched and excludes legacy records by scoping all queries to new workspace partition keys.
- An absent, corrupt, or failed OKF extraction never causes document ingestion to fail or query execution to return an error (ADR-001).

**Security model**:
- Ingestion and query operations validate workspace authorization through existing identity checks.
- Document and workspace deletion tombstones take immediate precedence over cached or persisted OKF facts.
- Document text passed to language model extraction prompts is isolated with strict delimiter tags and JSON schema enforcement, treating source content as untrusted data.

**Configuration required**:
- `OKF_ENABLED`: boolean (default true)
- `OKF_TIMEOUT_MS`: integer (default 50)
- `OKF_MAX_INPUT_TOKENS_PER_DOC`: integer (default 4000)
- `OKF_MAX_OUTPUT_TOKENS_PER_DOC`: integer (default 1200)
- `OKF_MAX_MODEL_CALLS_PER_DOC`: integer (default 2)
- `OKF_BEDROCK_MODEL_ID`: string (default "anthropic.claude-3-haiku-20240307-v1:0")
- `OKF_FUSION_WEIGHT`: float (default 1.15)

**Critical test scenarios**:
- Soft pooling discovery preservation: Query for an executive officer matches an OKF property on page 2, while the required factual answer resides in paragraph text on page 18. Verify that page 18 candidates from vector search are preserved in the merged pool and cited correctly, verifying **AC-9**, **AC-12**.
- Curated table schema extraction: Ingest a CSV table; verify that column headers and data types are extracted deterministically with 0 Bedrock model calls, recorded in `DocumentSnapshot.okf_status="ready"`, and queryable via OKF schema queries without duplicating deterministic aggregate calculations, verifying **AC-1**.
- Grounding verification rejection: Supply model extraction output where the property value does not appear in the referenced chunk content; verify the assertion is rejected as unsupported, omitted from storage, and `rejected_assertions_count` is incremented, verifying **AC-2**.
- Ingestion budget enforcement: Ingest a 50 page document; verify that OKF extraction executes at most 2 model calls and stays within the 4000 input and 1200 output token limits, verifying **AC-3**.
- Ingestion failure resilience (ADR-001): Inject a model provider timeout or malformed JSON during ingestion; verify that document ingestion succeeds, baseline chunks and snapshot are persisted, and `okf_status` records `failed`, verifying **AC-3**.
- Idempotent replay: Re ingest the same document with identical content hash; verify OKF skips model extraction and reuses existing assertions with 0 model calls, verifying **AC-3**.
- Conflicting property value handling: Ingest two documents where Doc A reports revenue as $10M and Doc B reports revenue as $12M for Acme Corp. Query for revenue; verify both assertions persist under distinct `assertion_id`s, `property_state == "conflicting"`, candidates from both documents are pooled with divided weights (0.575), and distinct chunk provenance is preserved, verifying **AC-4**.
- Same name entity ambiguity: Ingest Doc A mentioning John Smith (CEO) and Doc B mentioning John Smith (Author). Query for John Smith; verify both entities retain distinct `entity_id`s, `entity_state == "ambiguous"`, and neither entity overwrites the other, verifying **AC-5**.
- Indexed entity resolution: Query for an entity in a workspace with multiple documents; verify resolution queries GSI1 with bounded limit and executes zero table scans, verifying **AC-6**.
- Storage deadline enforcement: Inject a 100ms storage delay into OKF lookup; verify that the 50ms deadline fires, logs a warning, drops OKF candidates, and allows baseline retrieval to complete within normal latency budgets, verifying **AC-8**.
- Multi tenant boundary isolation: Attempt to lookup OKF properties using another workspace identifier; verify empty response with 0 cross tenant leakage, verifying **AC-6**.
- Tombstone enforcement: Insert a deletion tombstone for a document; verify OKF lookups immediately filter out assertions from the tombstoned source while preserving active documents, verifying **AC-7**.
- Non destructive legacy isolation: Verify that queries through `OkfRetriever` never access legacy unpartitioned tables (`okf_entities`, `okf_properties`, `okf_relations`) and that existing legacy records remain unmodified, verifying **AC-10**.
- Offline ablation comparison: Run ablation harness across canonical test corpus comparing candidate page recall, precision, and latency with OKF enabled versus disabled against the repaired BM25 and vector baseline, verifying **AC-12**.

## Build plan

Following the Tracer Bullet build approach, this feature is implemented across two vertical slices:

### Slice 1: Extraction, Grounding Validation, Partitioned Storage, and Isolated Lookup
- [x] 1. **Typed OKF Schema and Contracts**: Define `OkfEntityRecord`, `OkfPropertyAssertion`, `OkfManifest`, and `OkfLookupResult` contracts in `src/schemas/contracts/okf.py` with stable assertion identities and document scoped entity IDs, satisfying **AC-1**, **AC-4**, **AC-5**.
- [x] 2. **Partitioned Storage Repository**: Implement `OkfRepository` supporting partitioned DynamoDB and SQLite/memory stores with Main PK/SK, GSI1 (entity resolution), and GSI2 (document readiness and scoping), with exact query methods and bounded limits, satisfying **AC-6**, **AC-10**.
- [x] 3. **Curated Table and Schema Extraction**: Implement deterministic schema and header assertion extraction for tabular CSV files in `OkfBuilder`, mapping column names, data types, and table entities to TableStore locators with 0 model tokens, satisfying **AC-1**.
- [ ] 4. **Bounded Ingestion Extraction with Grounding Verification**: Implement Bedrock completion extraction for text PDFs in `OkfBuilder` with hard token caps (4000 input, 1200 output), call limits (max 2), provider deadlines, idempotency replay, and verbatim chunk grounding validation that rejects unsupported assertions, satisfying **AC-2** and partially satisfying **AC-3** (multi process distributed locking kept open as documented limitation).
- [x] 5. **ADR-001 Baseline Publication Decoupling**: Wire OKF extraction as a decoupled enrichment pass in `DocumentsService`, updating `DocumentSnapshot` metadata (`okf_status`, `okf_entities_count`, `okf_properties_count`) while guaranteeing baseline snapshot creation never fails if OKF extraction fails or times out, satisfying **AC-1**, **AC-3**, **AC-11**.
- [x] 6. **Boundary and Tombstone Enforcement**: Enforce workspace partition key isolation and active tombstone filtering inside worker tasks, dropping assertions from deleted sources without storage errors, satisfying **AC-6**, **AC-7**.
- [x] 7. **Deadline Enforced Lookup and Concurrency Controls**: Implement `OkfConcurrencyController` and `OkfRetriever` with 50ms total caller deadline, disabling SDK retries, bounding admission queue capacity (4 workers, 8 queue limit), retaining worker capacity until underlying storage completes, and hydrating chunk objects inside the worker task, satisfying **AC-8**.
- [x] 8. **Slice 1 Regressions and Grounding Tests**: Add unit tests verifying schema validation, curated CSV extraction, grounding rejection, token budget enforcement, ADR-001 fail open ingestion resilience, stable assertion identity preservation, same name ambiguity, conflicting value retention, tombstone filtering, and timeout handling, satisfying **AC-1**, **AC-2**, **AC-3**, **AC-4**, **AC-5**, **AC-6**, **AC-7**, **AC-8**.

### Slice 2: Candidate Fusion, Pipeline Wiring, and Offline Ablation
- [x] 9. **Reciprocal Rank Fusion Pooling and Ambiguity Handling**: Wire OKF property lookups into `langgraph_pipeline.py:run_okf_router`, remove legacy pre BM25 `OKF_BOOST` in `run_bm25`, bypass OKF on fast path queries, pool candidate chunks into reciprocal rank fusion with calibrated weight (1.15), and divide weights on ambiguous or conflicting properties, satisfying **AC-4**, **AC-5**, **AC-9**.
- [x] 10. **Pipeline Telemetry Exposure**: Expose `okf_candidate_ids`, `okf_latency_ms`, and `okf_match_count` in `QueryResponse.retrieval_candidates` trace, satisfying **AC-11**.
- [x] 11. **Offline Ablation Suite**: Compare candidate page recall, precision, and latency with OKF enabled versus disabled across canonical evaluation documents, verifying counterexample resilience where an incorrect entity match preserves the true answer page, satisfying **AC-9**, **AC-12**.
- [x] 12. **Benchmark Comparison Barrier**: Enforce comparator rules ensuring no benchmark accuracy claims are made until controlled canonical comparison run executes on `ws_canonical_comparison_0004`, satisfying **AC-12**.

## Migration plan

**Strategy**: Strangler pattern with non destructive legacy cutover.

**Phases**:
1. Phase 1: Deploy new partitioned `OkfRepository` and typed contracts (`kre_okf_facts`) alongside existing codebase without modifying active query routing. Legacy tables (`okf_entities`, `okf_properties`, `okf_relations`) remain intact.
2. Phase 2: Route document ingestion through new bounded `OkfBuilder`, updating `DocumentSnapshot` metadata with OKF metrics.
3. Phase 3: Cut over `langgraph_pipeline.py:run_okf_router` from brownfield `OKFRetriever` to new bounded `OkfRetriever` under concurrency controller and deadline protection, removing legacy `OKF_BOOST`.
4. Phase 4: Verify production behavior. Legacy tables remain unreferenced and dormant; formal deprecation and deletion of legacy tables is deferred to future maintenance windows.

**Rollback**:
If any regression is detected during deployment, set `OKF_ENABLED=false` to immediately deactivate OKF extraction and retrieval without requiring a code deployment, falling back entirely to baseline retrieval.

**Risks**:
- Stale brownfield global tables could be queried if legacy code paths are not completely retired. Mitigated by removing legacy imports and enforcing workspace partition checks in all repository methods.
- Language model provider rate limits during ingestion could slow document processing. Mitigated by ADR-001 non blocking fallback, strict token caps, and idempotency replay.

## Consequences

**Positive**:
- Eliminates cross tenant data leakage by partitioning all OKF records by workspace identifier.
- Restores curated schema and header discovery for tabular CSV files without duplicating deterministic aggregate calculations.
- Grounding verification rejects unsupported model claims, ensuring that only verified facts enter the index.
- Preserves distinct assertions and conflicts under stable IDs without silent overwrites or premature unifications.
- Repairs broken repository caller contracts in `okf_retriever.py`, eliminating swallowed runtime exceptions.
- Binds ingestion extraction to a predictable token budget with idempotency replay, preventing unbudgeted API expenses.
- Deterministic query entity lookup adds zero language model tokens during retrieval.
- Preserves independent baseline discovery, ensuring wrong entity matches never exclude valid factual evidence.
- Non destructive legacy cutover eliminates migration downtime and avoids accidental data loss.

**Negative / tradeoffs**:
- Restricting text extraction to introductory sections may miss minor entities mentioned only in deep document appendices.
- Model extraction for narrative text during ingestion incurs small incremental Bedrock token usage compared to zero cost PageIndex structural extraction.

**Neutral**:
- Requires creating new partitioned DynamoDB tables in cloud environments and updating `DocumentSnapshot` schema with OKF metadata fields.

## Follow up

- [ ] Update `backend/AGENTS.md` context file when implementation begins.
- [ ] Verify that LocalStack DynamoDB configuration provisions the new partitioned OKF table schema in test environments.
