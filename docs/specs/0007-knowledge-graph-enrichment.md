# 0007. Bounded Knowledge Graph Enrichment and Multi Hop Relationship Traversal

**Date**: 2026-11-14
**Status**: Proposed

## Summary

This specification defines the bounded integration of Knowledge Graph enrichment as the next verifiable milestone after accepted specifications 0004, 0005, and 0006. Knowledge Graph enrichment extracts canonical entity relationships and inter entity directed connections from ingested text PDF and tabular documents. It enables bounded multi hop relationship traversal to uncover indirect connections across documents with verified chunk and table cell provenance.

It replaces legacy unpartitioned global DynamoDB tables and unconstrained table scans with a typed, workspace scoped relationship schema. Grounding validation rejects unsupported model claims by verifying that relationships and connected entities appear verbatim in authorized source chunks. For structured tabular documents, relationships are derived strictly from explicit primary and foreign keys or verified schema relationships, never from speculative column name similarity. Baseline BM25, vector discovery, PageIndex structural navigation, and TableStore deterministic aggregate execution remain fully independent of graph enrichment. Baseline retrieval serves as the guaranteed operational fallback per ADR-001 so that any extraction timeout, storage failure, or missing relationship falls back immediately without degrading core query execution.

## Context

The repository has accepted specification 0004 (Core engine walking skeleton and benchmarks), specification 0005 (Bounded PageIndex structural retrieval), and specification 0006 (Bounded Open Knowledge Framework integration), while preserving specification 0003 (Snapshot registry, publication, and atomic deletion) as proposed and deferred. The engine supports multi tenant workspace isolation, tabular data processing without row truncation, hybrid lexical and vector retrieval, deterministic layout navigation for PDF documents, and grounded entity property assertions.

An audit of the existing brownfield graph components revealed five critical architecture flaws that prevent production use:
1. Cross tenant data leakage: The legacy graph repository in `backend/src/modules/graph/graph_repository.py` reads from global unpartitioned DynamoDB tables (`okf_entities`, `okf_properties`, `okf_relations`) that lack workspace identifiers in their partition keys. Additionally, `backend/src/modules/graph/graph_router.py` exposes `/documents/graph`, which queries across all tenants with no workspace scoping.
2. Unconstrained storage scans: `GraphRepository.get_workspace_graph` executes `self.okf_entities_table.scan(Limit=50)`, performing full table scans across all tenant data.
3. Unscalable chunk hydration: In `backend/src/services/retrieval/strategies/knowledge_graph.py:75`, resolving traversed edges calls `self.repository.get_all_chunks(workspace_id=workspace_id)`, loading all chunks in the entire workspace into memory to resolve a few chunk identifiers.
4. Broken runtime contracts: `GraphRetriever` in `backend/src/services/retrieval/graph_retriever.py` calls `self.repository.expand_graph(start_entities, max_hops)`. `CloudRepository` does not implement `expand_graph`, causing runtime failures that are silently swallowed in `langgraph_pipeline.py`.
5. Unbudgeted extraction and missing concurrency controls: Legacy extraction invokes unconstrained language model calls on every document batch without token budgets, timeouts, or multi process safety.

Architecture Decision Record ADR-001 (Baseline Publication Independent of Enrichment) governs this integration. ADR-001 dictates that baseline retrieval serves as the operational baseline, while optional enrichments are decoupled. Under no circumstances may an incomplete, slow, or failing graph extraction or traversal block baseline operations.

## Dependencies on Specification 0006 (OKF)

Knowledge Graph enrichment depends on specification 0006 (Open Knowledge Framework) for single entity definitions, property assertions, and tabular schemas. Specification 0006 closed and verified critical operational mechanisms across Slice 3.1 and Slice 3.2:
1. Durable budget coordination: Atomically acquired extraction permits (`BUDGET#DOC#{doc}#VER#{ver}`), separate prompt and output token ceilings, monotonic fence tokens, 15 second lease expiry, crash recovery fail closed semantics, and strongly consistent ledger reads.
2. Mandatory Bedrock token validation: Mandatory request token bounding before dispatch using the Bedrock Runtime `count_tokens` API on structured converse payloads, failing closed with zero model calls if token counting fails or returns non positive values.
3. Publication identity and staging isolation: Staged payload writes under distinct publication sort keys (`PUB#{pub_id}`), atomic manifest visibility commits, optimistic concurrency against base publication identifiers, same version republication isolation, and deferred inert record cleanup.
4. Concurrency control and capacity retention: Hard client deadlines, bounded worker pools, and worker capacity retention until underlying storage threads finish.
5. Mandatory worker identity: Non empty worker identity required on all fenced writes across memory, SQLite, and DynamoDB stores.

Knowledge Graph enrichment reuses and builds upon these proven contracts without weakening any safety guarantees.

## Scope Reconciliation (Feature 7)

In `docs/scope/scope.md`, Feature 7 represents `Knowledge graph enrichment` (Slice 4). Baseline discovery retrieval, lexical search, vector indexing, and TableStore aggregation were already established and verified in specification 0004 (Feature 4). Slice 4 builds upon that verified baseline by introducing bounded inter entity graph traversal. Specification 0007 remains `Proposed` until approved and built under `/develop`.

## Overlap Analysis with OKF (Spec 0006)

To prevent architectural bloat and duplicate work, the division of responsibility between OKF and Knowledge Graph enrichment is strictly defined:

| Dimension | Open Knowledge Framework (Spec 0006) | Knowledge Graph Enrichment (Spec 0007) |
|---|---|---|
| **Primary Scope** | Entity attributes and scalar properties (for example Acme Corp revenue is 500 Cr; CEO is Jane Doe). | Directed relationships connecting two distinct entities (for example Acme Corp acquired Beta Tech; Beta Tech operates in Region X). |
| **Tabular Role** | Curated table schemas, column data types, units, and header definitions. | Entity relationships discovered across data records and verified foreign keys. |
| **Query Mechanism** | Single hop property lookup given an entity name and optional property key. | Bounded multi hop traversal (1 to 2 hops, max 3) discovering indirect chains across documents. |
| **Model Invocations** | Extracts entity property dictionaries per document version. | Extracts directed entity relationship triples `(source, canonical_predicate, target)` with chunk locators. |
| **Deduplication Rule** | OKF owns entity scalar properties and CSV table schemas. Graph enrichment does not re extract or duplicate scalar entity properties. | Graph owns entity to entity directed edges and multi hop path expansion. OKF does not perform multi hop traversal. |

## Requirements

**User stories**:
- As a retrieval engineer, I want queries involving multi hop relationships (such as companies connected to partners of an organization) to retrieve verified relationship chains so that complex relationship questions are answered with exact chunk citations.
- As a retrieval engineer, I want graph candidates to pool into reciprocal rank fusion alongside independent BM25, vector, PageIndex, and OKF results so that an incomplete graph path never excludes true factual evidence found by baseline retrieval.
- As a system operator, I want graph extraction and traversal to enforce strict token, call, and latency budgets so that slow model calls or storage hiccups fall back immediately to baseline retrieval without delaying queries or failing document ingestion.
- As a tenant administrator, I want graph entities and relationships to be strictly partitioned by workspace identifier and document version so that proprietary organizational relationships never leak across workspace boundaries or surface tombstoned content.

**Acceptance criteria**:
- **AC-1**: Ingestion extracts and persists typed `KnowledgeGraphEntity` and `KnowledgeGraphRelationship` records per document version for text PDF and structured tabular documents. Relationships represent directed triples `(source_entity, canonical_predicate, target_entity)` with verified source chunk or table cell locators.
- **AC-2**: Extracted relationships are validated against authorized active chunks or verified table structures before persistence. Candidate relationships must cite an active chunk belonging to the current document version, possess a valid locator, and contain the asserted relationship predicate context and both connected entities verbatim in the source chunk text. For tabular data, relationships are constructed strictly from explicit foreign keys or row level co occurrence, never from fuzzy column name similarity. Unsupported claims are rejected as hallucinations and omitted from storage.
- **AC-3**: Graph ingestion extraction enforces strict resource bounds per document version: maximum 2 Bedrock completion calls, maximum 4000 total prompt input tokens (capped at 2000 per call), maximum 1600 total output tokens (capped at 800 per call), and a 10.0s total provider deadline per call (3.0s connect, 7.0s read). Retries are capped at at most 1 retry on throttling (HTTP 429), and each retry strictly counts as a reserved call against the 2 call ceiling. Zero retries are permitted on model errors, bad requests, or timeouts. Extraction is idempotent, keyed by `idempotency_key = sha256(f"{workspace_id}:{document_id}:{document_version}:kg")`. Replaying an existing version reuses persisted relationships with zero additional model calls. Baseline publication completes independently per ADR-001.
- **AC-4**: Durable budget accounting, lease expiry, stale worker fencing, retries, and crash recovery: Aligning with accepted specification 0006 Slice 3.1 and Slice 3.2, graph extraction enforces a durable ledger in `kre_knowledge_graph` (`PK = KG#{workspace_id}#DOC#{document_id}#V#{document_version}`, `SK = BUDGET#LEDGER`) that atomically records call reservations, cumulative token expenditures, active lease ownership, lease expiry timestamp, and a monotonically increasing fence token. Before making a provider call, a worker must atomically acquire a lease permit (`reserved_calls < max_calls`). The lease expires after 15 seconds. Mandatory worker identity (`worker_id`) is enforced on all fenced writes. If a worker call takes longer than the lease window (or if the worker crashes) and another worker takes over by advancing the fence token, the late returning worker is fenced: its write is rejected and no relationships are persisted. The tokens consumed by the stale worker call are reconciled into the ledger cumulative token count. On worker crash or restart, budget recovery performs a strongly consistent read; if `reserved_calls >= max_calls` and extraction is uncompleted, the document version budget fails closed as exhausted with zero additional provider calls.
- **AC-5**: Mandatory Bedrock CountTokens validation and request token bounding: When a live Bedrock client is configured, authoritative token bounding before dispatch is strictly enforced by the official Bedrock Runtime `count_tokens` API on structured converse payloads. If `count_tokens` is missing, fails, or returns non positive tokens, extraction fails closed immediately with zero provider calls. Heuristic token estimation is strictly prohibited when running against a live provider client and is restricted exclusively to offline unit test fixtures with no client. The complete request payload is bounded by maximum 2000 characters per chunk and 8000 UTF-8 bytes total per call prompt. Any prompt exceeding the 2000 input token ceiling fails closed before network dispatch.
- **AC-6**: Storage and lookups are strictly partitioned by `workspace_id`. Graph edges are stored in `kre_knowledge_graph` using indexed partition keys (`KG#{workspace_id}#ENTITY#{source_id}`) and sort keys (`EDGE#{target_entity_id}#{canonical_predicate}#{source_document_id}`). Edges record `source_document_id`, `source_document_version`, and `publication_id`. Reverse relationships are indexed via GSI1 (`KG#{workspace_id}#TARGET#{target_id}`). A dedicated entity mention lookup index is maintained at `PK = KG#{workspace_id}#LOOKUP` with sort key `MENTION#{normalized_mention}#ENT#{entity_id}#DOC#{document_id}`. In memory hash calculation is used for exact entity name matching to eliminate redundant lookup queries. Traversal resolves active publications per candidate document via manifest lookup or request level cache; uncommitted, stale, or failed publication edges are rejected. Multi hop traversal executes strictly via indexed queries against primary keys, GSI1, and the lookup index with bounded limits (maximum 10 edges per node, maximum 25 total nodes visited), with zero unconstrained table scans.
- **AC-7**: Active deletion tombstones in `WorkspaceBoundaryRepository` filter out relationships belonging to deleted source documents. Tombstone checks execute inside worker tasks and fail closed: any check error or workspace tombstone returns zero graph candidates immediately without throwing unhandled exceptions.
- **AC-8**: Retrieval latency targets and admission control: Graph lookups enforce a proposed target deadline of 75ms total wall clock time for cloud environments (50ms for local development and cache hits) across entity lookup, graph traversal, and chunk hydration. Lookups are governed by `GraphConcurrencyController` (maximum 4 concurrent workers, maximum 8 queued submissions). When a caller deadline expires, the caller drops the future immediately, logs a timeout, and yields zero candidates to fusion, ensuring zero late candidate publication. Running storage work retains its worker capacity permit until underlying thread execution completes. Baseline retrieval completes independently without degradation.
- **AC-9**: Graph traversal bounds maximum expansion depth to 2 hops (or 3 hops when explicitly requested by causal query plans), applying cycle detection to prevent infinite loops. Frontier expansion is deterministically ordered by confidence descending and identifier ascending. Total visited nodes are capped at 25. Hydration is capped at 15 chunks, hydrated via targeted batch reads by chunk identifier inside the worker task, never loading entire workspace chunk sets into memory. If traversal terminates early due to limits, partial results are safely returned with truncation telemetry.
- **AC-10**: Candidate fusion and fast path bypass: Validated source chunks backing traversed edges are pooled into reciprocal rank fusion with a provisional initial weight of 1.10 (divided to 0.55 on entity ambiguity or conflicting edges), explicitly marked as provisional until measured and calibrated against the 60 question benchmark in Slice 2. Graph candidates never exclude or prune chunks from other retrieval strategies. Queries executing on the fast path (`plan.fast_path == True`) bypass graph traversal entirely. Zero model calls and zero embedding invocations are performed during graph retrieval.
- **AC-11**: Non destructive legacy cutover: Scoped retrieval in `KnowledgeGraphStrategy` queries exclusively through the new workspace partitioned table (`kre_knowledge_graph`), excluding old global unpartitioned records by construction. Legacy tables (`okf_entities`, `okf_properties`, `okf_relations`) and old data are preserved without modification, and deletion is explicitly deferred.
- **AC-12**: DocumentSnapshot exposes `graph_status`, `graph_entities_count`, and `graph_relations_count` without requiring dedicated administrative HTTP endpoints. Candidate chunk identifiers, latency, traversed node count, edge match counts, and truncation flags are exposed in `QueryResponse.retrieval_candidates` debug trace.
- **AC-13**: An offline ablation comparison suite evaluates candidate recall, precision, and latency with Knowledge Graph enabled versus disabled against the BM25 and vector baseline across canonical evaluation documents, including a counterexample test proving that an incorrect graph expansion does not prevent retrieval of the true answer page. Benchmark comparison barrier is strictly preserved: no benchmark accuracy claims may be asserted without authorized execution on `ws_canonical_comparison_0004`. Paid benchmark execution is prohibited without explicit authorization.

## Options considered

### Option 1: Bounded Workspace Scoped DynamoDB and SQLite Graph with Lease Reservations (Recommended)

Store graph relationships in dedicated workspace scoped DynamoDB records (`kre_knowledge_graph`) and SQLite tables. Enforce strict bounded multi hop traversal (max 2 hops, max 25 nodes) via indexed queries. Enforce ingestion budget leases via DynamoDB conditional writes and SQLite transactions. Pool candidate chunks into reciprocal rank fusion without hard exclusions.

**Pros**:
- Reuses existing DynamoDB and SQLite storage infrastructure without adding new database clusters.
- Guarantees strict multi tenant isolation via workspace prefixed partition keys.
- Adopts the durable budget accounting and fencing pattern proven in OKF Slice 3.1 and Slice 3.2.
- Bounded traversal and targeted chunk hydration prevent memory exhaustion and latency stalls.
- Conforms strictly to ADR-001: graph extraction failures leave baseline search fully functional.

**Cons**:
- Multi hop traversals beyond 3 hops are inefficient in key value stores (accepted, as multi hop retrieval for RAG is proven to degrade past 2 to 3 hops).

### Option 2: Dedicated Graph Database (Neo4j or Amazon Neptune)

Deploy a dedicated graph database cluster to store entities and execute Cypher or Gremlin graph queries.

**Pros**:
- Native graph query languages support arbitrary multi hop traversal and graph algorithms.

**Cons**:
- Adds heavy operational overhead, substantial infrastructure cost, and cold start connection latency.
- Introduces complex cross database consistency and authorization boundaries.
- Violates the project simplicity guidelines for a brownfield engine refactor.

### Option 3: Unconstrained Language Model Graph RAG

Invoke language models at query time to perform iterative graph exploration and multi step agentic expansion.

**Pros**:
- Flexible query time exploration.

**Cons**:
- Incurs high query latency (several seconds) and high monetary cost per query.
- Violates the engine hard constraint requiring deterministic zero model calls during retrieval.

## Decision

**Chosen option**: Option 1: Bounded Workspace Scoped DynamoDB and SQLite Graph with Lease Reservations.

We implement Knowledge Graph enrichment as a bounded, workspace partitioned vertical slice built on existing DynamoDB and SQLite tables. This preserves multi tenant isolation, avoids new database infrastructure, eliminates unconstrained scans, enforces strict token budgets, and ensures baseline search is never blocked.

**Implementation skills**: none

## Rationale

Option 1 provides the exact capabilities needed for multi hop relationship traversal without the operational burden of dedicated graph clusters or the unpredictable latency of query time language model calls. 

By using workspace scoped partition keys (`KG#{workspace_id}#...`), we permanently eliminate cross tenant data leakage. Using targeted batch get reads by chunk identifier replaces the fatal whole workspace scans that previously caused memory exhaustion. Applying the durable budget accounting pattern proven in OKF specification 0006 guarantees that distributed workers cannot exceed provider token budgets or leak ungrounded hallucinations into persisted records.

## Feature design

### 1. Data Model and Contracts

Shared frozen Pydantic contracts are placed in `backend/src/schemas/contracts/knowledge_graph.py`:

```python
from enum import StrEnum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from schemas.contracts.document_location import DocumentLocation
from schemas.contracts.table_location import TableLocation

class GraphStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"

class KnowledgeGraphEntity(BaseModel):
    """Canonical entity record in a workspace."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    entity_id: str = Field(description="Deterministic hash: ent_{sha256(workspace:type:normalized_name)[:16]}")
    workspace_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=512)
    canonical_name: str = Field(..., max_length=512)
    entity_type: Literal["organization", "person", "location", "product", "concept", "table", "other"] = "concept"
    document_ids: tuple[str, ...] = Field(default_factory=tuple)
    aliases: tuple[str, ...] = Field(default_factory=tuple)

class KnowledgeGraphRelationship(BaseModel):
    """Verified directed relationship edge grounded in source document evidence."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    relationship_id: str = Field(description="Deterministic hash: rel_{sha256(ws:src:pred:tgt:doc)[:24]}")
    workspace_id: str = Field(..., max_length=64)
    source_entity_id: str = Field(..., max_length=64)
    source_entity_name: str = Field(..., max_length=512)
    predicate: str = Field(..., max_length=128)
    canonical_predicate: str = Field(..., max_length=128)
    target_entity_id: str = Field(..., max_length=64)
    target_entity_name: str = Field(..., max_length=512)
    directed: bool = True
    confidence: float = Field(ge=0.0, le=1.0)
    source_document_id: str = Field(..., max_length=128)
    source_document_version: str = Field(..., max_length=64)
    publication_id: str = Field(..., max_length=128)
    source_chunk_ids: tuple[str, ...] = Field(default_factory=tuple)
    location: DocumentLocation | TableLocation
    verbatim_evidence: str = Field(..., max_length=4096)
    relationship_state: Literal["unique", "conflicting", "ambiguous"] = "unique"
    created_at_utc: str

class KnowledgeGraphManifest(BaseModel):
    """Manifest record certifying active publication outcome for a document version."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    workspace_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=128)
    document_version: str = Field(..., max_length=64)
    publication_id: str = Field(..., max_length=128)
    base_publication_id: str | None = Field(default=None, max_length=128)
    status: Literal["ready", "empty", "failed"] = "ready"
    entity_count: int = Field(default=0, ge=0)
    relationship_count: int = Field(default=0, ge=0)
    ingested_at_utc: str
    content_hash: str = Field(..., max_length=128)

class KnowledgeGraphLookupResult(BaseModel):
    """Outcome of multi hop graph traversal and chunk hydration."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    seed_entities: tuple[str, ...]
    entity_state: Literal["unique", "ambiguous", "none"] = "none"
    relationship_state: Literal["unique", "conflicting", "none"] = "none"
    traversed_node_count: int = 0
    edge_match_count: int = 0
    matched_chunk_ids: tuple[str, ...] = Field(default_factory=tuple)
    candidate_chunks: tuple[Any, ...] = Field(default_factory=tuple)
    execution_status: Literal[
        "completed", "truncated", "caller_timeout", "admission_rejected", "tombstoned", "storage_failed"
    ] = "completed"
    latency_ms: float = 0.0

class KnowledgeGraphBudgetLedger(BaseModel):
    """Durable budget ledger record tracking call reservations and spent tokens."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    workspace_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=128)
    document_version: str = Field(..., max_length=64)
    reserved_calls: int = Field(default=0, ge=0)
    completed_calls: int = Field(default=0, ge=0)
    reserved_tokens: int = Field(default=0, ge=0)
    spent_tokens: int = Field(default=0, ge=0)
    reserved_prompt_tokens: int = Field(default=0, ge=0)
    spent_prompt_tokens: int = Field(default=0, ge=0)
    reserved_output_tokens: int = Field(default=0, ge=0)
    spent_output_tokens: int = Field(default=0, ge=0)
    lease_owner_id: str | None = Field(default=None, max_length=128)
    lease_expires_at: float = Field(default=0.0, ge=0.0)
    fence_token: int = Field(default=0, ge=0)
    reconciled_fences: tuple[int, ...] = Field(default_factory=tuple)
    status: Literal["active", "completed", "exhausted", "failed"] = "active"
    updated_at: str
```

### 2. Entity Identity, Resolution, and Cross Document Semantics

1. **Deterministic Canonical Identifiers**:
   Entities do not merge purely on shared words. Identity incorporates the active workspace, entity type, and NFKD normalized canonical name:
   `canonical_key = f"{workspace_id}:{entity_type}:{normalized_canonical_name}"`
   `entity_id = f"ent_{hashlib.sha256(canonical_key.encode('utf-8')).hexdigest()[:16]}"`
   This guarantees that "Washington (location)" and "Washington (person)" remain distinct nodes with collision safe identifiers.

2. **Mention Lookup and Aliases**:
   Mention lookup records are stored under:
   `PK = KG#{workspace_id}#LOOKUP`
   `SK = MENTION#{normalized_mention}#ENT#{entity_id}#DOC#{document_id}`
   This preserves exact mention provenance across documents. Exact query matches compute `entity_id` directly in memory, saving one network lookup. Prefix matches query `SK begins_with MENTION#{query_token}#` with `Limit=5`.

3. **Ambiguity and Conflicting Evidence**:
   - Entity Ambiguity: If a query token matches multiple distinct `entity_id` values with differing entity types, `entity_state = "ambiguous"` is flagged. The retriever expands all matched entities independently and applies the ambiguity fusion discount (halving fusion weight to 0.55 per affected candidate).
   - Conflicting Evidence: If two documents assert contradictory predicates between the exact same pair of entities (such as acquired versus divested), both edges are preserved under their distinct document IDs. In retrieval, `relationship_state = "conflicting"` is flagged, distinct citations are surfaced, and fusion weight is halved to 0.55 for chunks backing those edges.

4. **Distinction from OKF**:
   OKF entities are strictly document scoped (`OkfEntityRecord` carries `document_id`) to isolate scalar facts. Knowledge Graph entities are workspace scoped canonical nodes, while directed edges retain document version and publication provenance.

### 3. Storage Schema, Indexing, and Publication Staging

Storage is implemented in `kre_knowledge_graph` (DynamoDB in production, SQLite and in memory in tests):

1. **Active Manifest Record**:
   - `PK`: `KG#{workspace_id}#DOC#{document_id}#V#{document_version}`
   - `SK`: `MANIFEST`
   - Attributes: `workspace_id`, `document_id`, `document_version`, `publication_id`, `status`, `entity_count`, `relationship_count`, `ingested_at_utc`, `content_hash`.
   - Read consistency: Strongly consistent reads for publication authority and crash recovery.

2. **Durable Budget Ledger Record**:
   - `PK`: `KG#{workspace_id}#DOC#{document_id}#V#{document_version}`
   - `SK`: `BUDGET#LEDGER`
   - Attributes: Ledger fields matching `KnowledgeGraphBudgetLedger`.
   - Read consistency: Strongly consistent reads.

3. **Directed Relationship Edge Records**:
   - `PK`: `KG#{workspace_id}#ENTITY#{source_entity_id}`
   - `SK`: `EDGE#{target_entity_id}#{canonical_predicate}#{source_document_id}`
   - Attributes: `workspace_id`, `source_entity_id`, `source_entity_name`, `target_entity_id`, `target_entity_name`, `predicate`, `canonical_predicate`, `confidence`, `source_document_id`, `source_document_version`, `publication_id`, `source_chunk_ids`, `locations_json`, `verbatim_evidence`, `created_at_utc`.
   - Edge deduplication: By embedding `target_entity_id`, `canonical_predicate`, and `source_document_id` in the sort key, a document holds at most one edge per directed relationship, merging multiple chunk references into `source_chunk_ids`. This prevents intra document edge duplication from starving traversal limits.

4. **Global Secondary Index 1 (GSI1 Incoming and Reverse Edges)**:
   - Index Name: `gsi1_target_lookup`
   - `GSI1_PK`: `KG#{workspace_id}#TARGET#{target_entity_id}`
   - `GSI1_SK`: `EDGE#{source_entity_id}#{canonical_predicate}#{source_document_id}`
   - Projection: `ALL`

5. **Global Secondary Index 2 (GSI2 Document Version Scoping and Cleanup)**:
   - Index Name: `gsi2_doc_version`
   - `GSI2_PK`: `KG#{workspace_id}#DOC#{document_id}#V#{document_version}`
   - `GSI2_SK`: `EDGE#{source_entity_id}#{target_entity_id}#{canonical_predicate}`
   - Projection: `KEYS_ONLY`

6. **Publication Staging and Cross Document Visibility**:
   - Extraction generates a fresh `publication_id = f"pub_{uuid.uuid4().hex[:16]}"`.
   - Staging writes edges with `publication_id` attached.
   - Atomic Commit: The manifest is committed atomically with the budget ledger condition check and worker identity validation.
   - Cross Document Resolution: In retrieval, when edges from multiple documents are returned, the retriever checks each edge's `(source_document_id, source_document_version, publication_id)` against the active publication context (cached in a request level dictionary). If an edge's `publication_id` does not match the active ready manifest, it is rejected as uncommitted or stale.
   - Failed or Fenced Publication: If extraction fails or times out, manifest status is `failed` or uncommitted. Traversal rejects all edges citing that publication.
   - Inert Record Cleanup: Deletion or version replacement triggers asynchronous cleanup via GSI2 without blocking the publication path.

### 4. Predicate Sanitization and Allowlisted Representation

To prevent sort key delimiter collision on `#` or special characters:
All predicates are mapped to an allowlisted canonical snake case representation (`ACQUIRED`, `SUBSIDIARY_OF`, `PARTNERED_WITH`, `LOCATED_IN`, `FOUNDED_BY`, `SUPPLIES`, `PRODUCES`, `EMPLOYS`, `INVESTED_IN`, `MEMBER_OF`, `RELATED_TO`), or URL safe percent encoded:
`canonical_predicate = urllib.parse.quote(predicate.strip().lower().replace(" ", "_"), safe="")`
This eliminates all delimiter collisions in sort keys.

### 5. Structured Tabular Relationships

1. **Deterministic Construction**:
   Relationships in CSV or tabular data are derived strictly from:
   - Explicit foreign key mappings defined in table schemas, or
   - Verified row level co occurrence of primary entity keys.
   Inferring relationships from fuzzy column name similarity is strictly prohibited.
2. **Provenance and Evidence**:
   Tabular edges cite a `TableLocation` specifying `table_name`, `row_index`, `source_column`, and `target_column`.
   `verbatim_evidence` records the exact serialized row text: `f"Row {row_index}: {source_col}={source_val} -> {target_col}={target_val}"`.
3. **Non Duplication**:
   Graph enrichment never extracts scalar numbers, schema types, or TableStore aggregation metrics. It extracts only entity to entity links.

### 6. Bounded Multi Hop Traversal Algorithm

The traversal algorithm executes deterministically:
1. **Seed Entity Resolution**: Match query tokens deterministically. Exact name matches compute `entity_id` in memory to eliminate a network query. Partial matches query `PK = KG#{workspace_id}#LOOKUP` with `SK begins_with MENTION#{token}#` (Limit 5).
2. **Hop 1 Expansion**:
   - Outgoing edges: `PK = KG#{ws}#ENTITY#{seed_id}`, `SK begins_with EDGE#` (Limit 10).
   - Incoming edges: `GSI1_PK = KG#{ws}#TARGET#{seed_id}`, `GSI1_SK begins_with EDGE#` (Limit 10).
   - Filter edges by active publication and document version.
   - Add target entities to frontier and mark visited: `visited_entities.add(seed_id)`.
3. **Hop 2 Expansion** (if `max_hops >= 2` and visited nodes < 25):
   - Sort unvisited frontier nodes deterministically by `(confidence DESC, entity_id ASC)`.
   - Take top 5 frontier nodes. For each, query outgoing edges (Limit 5) and incoming edges (Limit 5).
   - Filter by active publication, add to visited set, and detect cycles (`if target_id in visited: skip`).
4. **Hop 3 Expansion** (only when explicitly requested by causal query plans):
   - Take top 3 frontier nodes, query outgoing edges with Limit 3, respecting the 25 node ceiling.
5. **Targeted Chunk Hydration**:
   - Collect distinct `source_chunk_ids` from traversed valid edges, capped at 15 chunks.
   - Sort candidate edges by `(confidence DESC, hop_distance ASC, relationship_id ASC)` to select the top 15 chunks.
   - Hydrate chunks via `batch_get_item` inside the worker task.
   - Filter against active deletion tombstones in `WorkspaceBoundaryRepository`.
6. **Incomplete Traversal**:
   If limits or deadlines truncate traversal, `execution_status = "truncated"` is recorded, and collected evidence items are safely returned to fusion.

### 7. Deadline, Concurrency Control, and Baseline Decoupling

1. **Calibrated Deadline**:
   Target deadline is 75ms total wall clock time for cloud environments (50ms for local development and cache hits).
2. **Admission and Capacity Retention**:
   Governed by `GraphConcurrencyController` (maximum 4 concurrent workers, maximum 8 queue limit).
   When the caller deadline expires, the caller logs a timeout and returns zero candidates to reciprocal rank fusion immediately.
   Worker capacity is retained until the underlying storage thread completes, preventing queue saturation from orphaned tasks. Late candidates are discarded with zero pipeline injection.
3. **Baseline Decoupling**:
   Ingestion and query retrieval are fully decoupled from graph enrichment per ADR-001. Failures or timeouts fall back immediately to BM25, vector, and PageIndex search.

### 8. API Surface

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| Ingestion background task | Internal | `workspace_id`, `document_id`, `document_version`, `chunks` | `KnowledgeGraphManifest` | Internal worker token | 422 (malformed chunk), 429 (throttled) |
| Pipeline `run_graph` | Internal | `query`, `workspace_id`, `plan`, `limits` | `list[EvidenceItem]`, telemetry | Session principal | Non blocking fallback to empty list |

### 9. Value Sourcing

| Action | Value produced / displayed | Source |
|---|---|---|
| Canonical entity creation | `entity_id` | `f"ent_{sha256(ws:type:norm_name)[:16]}"` |
| Relationship extraction | `relationship_id` | `f"rel_{sha256(ws:src:pred:tgt:doc)[:24]}"` |
| Ingestion extraction | `publication_id` | Fresh UUID generated per extraction attempt (`pub_{uuid[:16]}`) |
| Relationship extraction | `canonical_predicate` | Allowlisted snake case identifier or URL safe percent encoding |
| Relationship extraction | `source_chunk_ids` | Deduplicated chunk identifiers from source document chunks |
| Relationship extraction | `location` | `DocumentLocation` or `TableLocation` from source metadata |
| Relationship extraction | `verbatim_evidence` | Exact substring from chunk text or serialized row data |
| Relationship extraction | `confidence` | Model confidence score (0.0 to 1.0) or 1.0 for verified foreign keys |
| Budget reservation | `budget_ledger` | Atomic update on `PK = KG#{ws}#DOC#{doc}#V#{ver}`, `SK = BUDGET#LEDGER` |
| Traversal seed lookup | `matched_entity_ids` | In memory hash for exact match, or prefix query on `PK = KG#{ws}#LOOKUP` |
| Hop expansion | `outgoing_edges` / `incoming_edges` | Queried from Main PK and GSI1 with active publication filtering |
| Chunk hydration | `hydrated_chunks` | Batch get item by chunk ID inside worker task, filtered by tombstones |
| Reciprocal rank fusion | `graph_candidate_chunks` | Hydrated chunks pooled with provisional weight 1.10 (halved to 0.55 if ambiguous) |
| Document snapshot | `graph_status` | Status enum on `DocumentSnapshot` ("pending", "processing", "ready", "failed", "unavailable") |
| Document snapshot | `graph_entities_count`, `graph_relations_count` | Counts from active `KnowledgeGraphManifest` |
| Telemetry trace | `retrieval_candidates.graph` | Debug dict in `QueryResponse` detailing latency, matched IDs, visited nodes, truncation |

### 10. Key Invariants

1. Multi tenant isolation: All partition keys must begin with `KG#{workspace_id}#`. Cross workspace queries return zero entities and zero edges by construction.
2. Verbatim grounding: Every persisted relationship must have its predicate context and both entity names appear verbatim in the cited chunk text or verified table row. Hallucinated relationships are discarded.
3. Strict budget ceilings: A document version never consumes more than 2 Bedrock calls, 4000 total prompt tokens, and 1600 output tokens. Throttling retries count against the 2 call ceiling.
4. ADR-001 decoupling: Graph extraction or lookup failures must never block baseline document publication or baseline query retrieval.
5. Zero table scans: All storage queries must run against partition keys or indexed sort key prefixes with bounded limits. Whole table or workspace scans are prohibited.
6. Deterministic retrieval: Retrieval traversals perform zero model calls and zero embedding invocations.

### 11. Configuration Required

Registered in `backend/src/config.py`:
- `GRAPH_ENABLED: bool = False` (default False to prevent unexpected provider cost or baseline disruption)
- `GRAPH_TIMEOUT_MS: int = 75` (retrieval deadline across lookup, traversal, and hydration)
- `GRAPH_MAX_INPUT_TOKENS_PER_DOC: int = 4000` (input token ceiling for document extraction)
- `GRAPH_MAX_OUTPUT_TOKENS_PER_DOC: int = 1600` (output token ceiling for document extraction)
- `GRAPH_MAX_MODEL_CALLS_PER_DOC: int = 2` (maximum Bedrock calls per document version)
- `GRAPH_BEDROCK_MODEL_ID: str = "anthropic.claude-3-haiku-20240307-v1:0"` (provider model)
- `GRAPH_FUSION_WEIGHT: float = 1.10` (provisional reciprocal rank fusion initial weight)
- `GRAPH_AMBIGUOUS_FUSION_WEIGHT: float = 0.55` (provisional weight under ambiguity or conflicts)
- `GRAPH_MAX_HOPS: int = 2` (default expansion depth)
- `GRAPH_MAX_NODES: int = 25` (ceiling for total visited nodes)
- `GRAPH_MAX_EDGES_PER_NODE: int = 10` (maximum edges queried per node)
- `GRAPH_MAX_HYDRATED_CHUNKS: int = 15` (maximum chunks hydrated per query)
- `GRAPH_MAX_CONCURRENCY: int = 4` (worker pool size in concurrency controller)
- `GRAPH_MAX_QUEUE: int = 8` (admission queue limit in concurrency controller)
- `DYNAMODB_GRAPH_TABLE_NAME: str = "kre_knowledge_graph"` (DynamoDB table name)

### 12. Critical Test Scenarios

- Happy path extraction: Ingest a multi page text PDF with clear entity relationships; verify persisted entities and relationships with verbatim chunk evidence, verifies **AC-1**, **AC-2**.
- Verbatim grounding rejection: Mock model output containing relationship not present in source chunk text; verify extraction discards it, verifies **AC-2**.
- Tabular deterministic extraction: Ingest CSV table with explicit foreign key; verify relationship with `TableLocation` and zero model calls, verifies **AC-1**, **AC-2**.
- Resource budget and idempotency: Ingest document version twice; verify second ingestion makes zero provider calls, verifies **AC-3**.
- Throttling retry ceiling: Simulate Bedrock 429 throttling on Call 1; verify retry acquires Call 2 reservation and third attempt fails closed without dispatch, verifies **AC-3**, **AC-4**.
- Mandatory CountTokens validation: When live client is present and `count_tokens` returns error or missing count; verify extraction fails closed with 0 calls, verifies **AC-5**.
- Durable budget fencing: Simulate slow or crashed worker whose 15 second lease expires; verify late write is rejected and fence token prevents state corruption, verifies **AC-4**.
- Cross document publication visibility: Query entity with edges from two documents with different active publication IDs; verify both edges are visible, verifies **AC-6**.
- Stale publication rejection: Republish Document A to new publication; verify edges from prior publication are rejected during traversal, verifies **AC-6**.
- Bounded multi hop traversal: Create cyclic graph with 30 nodes; verify traversal terminates at 2 hops and maximum 25 visited nodes without infinite loop, verifies **AC-6**, **AC-9**.
- Targeted chunk hydration: Query graph with 20 matching edges; verify only top 15 chunks are hydrated via batch get, verifies **AC-9**.
- Concurrency and deadline enforcement: Inject 100ms storage delay; verify caller deadline drops graph candidates, retains worker capacity, and falls back cleanly to baseline retrieval, verifies **AC-8**.
- Cross tenant isolation: Query graph with different workspace ID; verify zero candidate leakage, verifies **AC-6**.
- Tombstone enforcement: Insert deletion tombstone for source document; verify graph edges from that document are excluded from traversal results, verifies **AC-7**.
- Reciprocal rank fusion: Verify graph candidate chunks pool into fusion with provisional weight 1.10 and fast path bypasses graph lookup entirely, verifies **AC-10**.
- Offline ablation comparison: Run ablation suite across canonical evaluation documents; verify candidate recall and counterexample discovery safety, verifies **AC-13**.

## Build plan

Following the Tracer Bullet build approach, this feature is implemented across two vertical slices:

### Slice 1: Contracts, Storage Schema, Grounded Extraction, Durable Budgeting, and Bounded Traversal

- [ ] 1. **Typed Knowledge Graph Contracts**: Define `KnowledgeGraphEntity`, `KnowledgeGraphRelationship`, `KnowledgeGraphManifest`, `KnowledgeGraphLookupResult`, and `KnowledgeGraphBudgetLedger` in `backend/src/schemas/contracts/knowledge_graph.py` with immutable fields, deterministic ID hashing, and explicit entity typing, satisfying **AC-1**, **AC-4**, **AC-6**.
- [ ] 2. **Partitioned Storage Repository**: Implement `KnowledgeGraphRepository` supporting DynamoDB, SQLite, and in memory stores with Main table partition keys (`KG#{workspace_id}#ENTITY#{source_id}`), sort keys (`EDGE#{target_id}#{canonical_predicate}#{doc_id}`), lookup index (`KG#{workspace_id}#LOOKUP`), GSI1 (reverse edges), GSI2 (document version scoping), durable budget ledger (`BUDGET#LEDGER`), staged publication, and tombstone checks, satisfying **AC-4**, **AC-6**, **AC-7**, **AC-11**.
- [ ] 3. **Grounded Ingestion Extraction and Tabular Processing**: Implement `KnowledgeGraphBuilder` in `backend/src/ingestion/graph_builder.py` extracting canonical entity relationship triples from PDF text chunks and CSV foreign keys with strict Bedrock completion caps (max 2 calls, 4000 total prompt tokens, 1600 total output tokens), idempotency hashing, and verbatim chunk grounding validation rejecting unsupported claims, satisfying **AC-1**, **AC-2**, **AC-3**.
- [ ] 4. **Durable Budget Accounting, Fencing, and Mandatory CountTokens**: Integrate atomic pre call budget reservations, 15 second lease expiries, monotonic fence tokens, stale worker fencing, crash recovery fail closed semantics, throttling retry call accounting, and mandatory Bedrock `count_tokens` validation, satisfying **AC-3**, **AC-4**, **AC-5**.
- [ ] 5. **Bounded Multi Hop Traversal and Concurrency Admission**: Implement `KnowledgeGraphRetriever` and `GraphConcurrencyController` enforcing seed entity resolution with in memory hash fast path, bounded breadth first traversal (max 2 hops, max 25 nodes, max 10 edges per node, max 15 hydrated chunks), cycle detection, targeted chunk hydration via `batch_get_item`, 75ms caller deadline, worker capacity retention, and zero table scans, satisfying **AC-6**, **AC-8**, **AC-9**.
- [ ] 6. **Slice 1 Unit and Regression Test Suite**: Implement comprehensive test suite in `backend/tests/test_knowledge_graph_slice1.py` verifying contract immutability, verbatim grounding validation, budget ledger fencing, crash recovery, multi hop traversal limits, cycle detection, targeted chunk batching, tombstone filtering, publication staging, and boundary isolation, satisfying **AC-1**, **AC-2**, **AC-3**, **AC-4**, **AC-5**, **AC-6**, **AC-7**, **AC-8**, **AC-9**.

### Slice 2: Pipeline Fusion, Telemetry, and Offline Ablation

- [ ] 7. **Pipeline Fusion and Fast Path Bypass**: Implement `KnowledgeGraphStrategy` and wire `run_graph` in `backend/src/services/langgraph_pipeline.py`, routing graph candidates into reciprocal rank fusion with provisional initial weight (1.10, halved to 0.55 on entity ambiguity or conflicting edges) while bypassing graph lookups entirely on fast path queries, satisfying **AC-10**.
- [ ] 8. **Decoupled Baseline Ingestion and Snapshot Metadata**: Wire graph extraction as a decoupled background enrichment pass in `backend/src/modules/documents/documents_service.py`, ensuring baseline publication completes independently per ADR-001, and exposing `graph_status`, `graph_entities_count`, and `graph_relations_count` in `DocumentSnapshot`, satisfying **AC-3**, **AC-11**, **AC-12**.
- [ ] 9. **Retrieval Candidate Telemetry Trace**: Expose `graph_candidate_ids`, `graph_latency_ms`, `graph_traversed_nodes`, `graph_matched_edges`, and `graph_execution_status` in `QueryResponse.retrieval_candidates` debug trace, satisfying **AC-12**.
- [ ] 10. **Offline Ablation Comparison Suite**: Implement evaluation harness in `backend/tests/test_knowledge_graph_ablation_slice2.py` comparing candidate page recall, precision, and latency with Knowledge Graph enabled versus disabled across canonical evaluation documents, including counterexample test proving incorrect graph expansion never displaces true answer pages, satisfying **AC-10**, **AC-13**.
- [ ] 11. **Benchmark Comparison Barrier**: Enforce comparator rules ensuring no benchmark accuracy claims are made until controlled canonical comparison run executes on `ws_canonical_comparison_0004`, with zero paid benchmark execution without explicit user authorization, satisfying **AC-13**.

## Migration plan

**Strategy**: Strangler pattern with non destructive legacy cutover.

**Phases**:
1. Phase 1: Deploy new partitioned `KnowledgeGraphRepository` and typed contracts (`kre_knowledge_graph`) alongside existing codebase without modifying active query routing. Legacy tables (`okf_entities`, `okf_properties`, `okf_relations`) remain intact. `GRAPH_ENABLED` defaults to `False`.
2. Phase 2: Route document ingestion through new bounded `KnowledgeGraphBuilder`, updating `DocumentSnapshot` metadata with graph metrics.
3. Phase 3: Cut over `langgraph_pipeline.py:run_graph` and `KnowledgeGraphStrategy` to the new bounded `KnowledgeGraphRetriever` under concurrency controller and deadline protection.
4. Phase 4: Verify production behavior. Legacy tables remain unreferenced; formal deprecation and deletion of legacy tables is deferred to future maintenance windows.

**Rollback**:
If any regression is detected during deployment, set `GRAPH_ENABLED=false` to immediately deactivate graph extraction and retrieval without requiring code redeployment, falling back entirely to baseline retrieval.

**Risks**:
- Stale brownfield global tables could be queried if legacy code paths are not retired. Mitigated by removing legacy imports and enforcing workspace partition checks in all repository methods.
- Language model provider rate limits during ingestion could slow document processing. Mitigated by ADR-001 non blocking fallback, strict token caps, and idempotency replay.

## Consequences

**Positive**:
- Eliminates cross tenant data leakage by partitioning all graph records by workspace identifier.
- Enables multi hop indirect relationship discovery across multiple documents with verified chunk and table cell provenance.
- Grounding verification rejects unsupported model claims, ensuring only verified relations enter the index.
- Targeted batch get chunk hydration replaces disastrous whole workspace scans.
- Binds ingestion extraction to a predictable token budget with idempotency replay, durable fencing, and mandatory Bedrock token validation.
- Deterministic query traversal adds zero language model tokens during retrieval.
- Preserves independent baseline discovery, ensuring incomplete graph paths never degrade core queries.
- Non destructive legacy cutover eliminates migration downtime and avoids accidental data loss.

**Negative / tradeoffs**:
- Traversals beyond 3 hops are restricted to preserve query latency and prevent graph fan out explosion.
- Model extraction for narrative text during ingestion incurs incremental Bedrock token usage compared to zero cost PageIndex structural extraction.

**Neutral**:
- Requires creating new partitioned DynamoDB tables in cloud environments and updating `DocumentSnapshot` schema with graph metadata fields.

## Follow up

- [ ] Update `backend/AGENTS.md` context file when implementation begins.
- [ ] Verify that LocalStack DynamoDB configuration provisions the new partitioned Knowledge Graph table schema in test environments.

## References

**Project sources**:
- ADR-001: Baseline Publication Independent of Enrichment (`docs/reference/ADR/ADR-001-baseline-vs-enrichment.md`)
- Architecture Bible §6.3, §7.2: Entity graphs, multi hop traversal, and reciprocal rank fusion
- Specification 0005: Bounded PageIndex Structural Retrieval (`docs/specs/0005-pageindex-structural-retrieval.md`)
- Specification 0006: Bounded Open Knowledge Framework Integration (`docs/specs/0006-okf-integration.md`)

**Practices & standards**:
- Strangler pattern for non destructive cutover
- Distributed monotonic lease fencing for idempotent resource accounting
- Reciprocal rank fusion ($k=60$) for multi strategy evidence pooling
