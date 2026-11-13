# 0007. Bounded Knowledge Graph Enrichment and Multi Hop Relationship Traversal

**Date**: 2026-11-14
**Status**: Proposed

## Summary

This specification defines the bounded integration of Knowledge Graph enrichment as the next verifiable milestone after accepted specifications 0004, 0005, and 0006. Knowledge Graph enrichment extracts canonical entity relationships and inter entity directed connections from ingested text PDF and tabular documents. It enables bounded multi hop relationship traversal to uncover indirect connections across documents with verified chunk provenance.

It replaces legacy unpartitioned global DynamoDB tables and unconstrained table scans with a typed, workspace scoped relationship schema. Grounding validation rejects unsupported model claims by verifying that relationships and connected entities appear verbatim in authorized source chunks. Baseline BM25, vector discovery, PageIndex structural navigation, and TableStore deterministic aggregate execution remain fully independent of graph enrichment. Baseline retrieval serves as the guaranteed operational fallback per ADR-001 so that any extraction timeout, storage failure, or missing relationship falls back immediately without degrading core query execution.

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

Knowledge Graph enrichment depends on specification 0006 (Open Knowledge Framework) for single entity definitions, property assertions, and tabular schemas. Specification 0006 closed and verified two critical operational mechanisms in Slice 3.1: durable budget coordination (`BUDGET#DOC#{doc}#VER#{ver}`, monotonic fence tokens, 15 second lease expiry, crash recovery) and authoritative request token accounting. Knowledge Graph enrichment reuses and builds upon these proven patterns.

## Scope Reconciliation (Feature 7)

In `docs/scope/scope.md`, the At a glance table previously labeled Feature 7 as `Baseline discovery retrieval and fusion`, while section 7 and the roadmap labeled it as `Slice 4: Knowledge graph enrichment`. Feature 7 is formally defined as `Knowledge graph enrichment`. Baseline discovery retrieval, lexical search, vector indexing, and TableStore aggregation were already established and verified in specification 0004 (Feature 4). Slice 4 builds upon that verified baseline by introducing bounded inter entity graph traversal.

## Overlap Analysis with OKF (Spec 0006)

To prevent architectural bloat and duplicate work, the division of responsibility between OKF and Knowledge Graph enrichment is strictly defined:

| Dimension | Open Knowledge Framework (Spec 0006) | Knowledge Graph Enrichment (Spec 0007) |
|---|---|---|
| **Primary Scope** | Entity attributes and scalar properties (for example Acme Corp revenue is 500 Cr; CEO is Jane Doe). | Directed relationships connecting two distinct entities (for example Acme Corp acquired Beta Tech; Beta Tech operates in Region X). |
| **Tabular Role** | Curated table schemas, column data types, units, and header definitions. | Entity relationships discovered across data records and narrative text. |
| **Query Mechanism** | Single hop property lookup given an entity name and optional property key. | Bounded multi hop traversal (1 to 2 hops, max 3) discovering indirect chains across documents. |
| **Model Invocations** | Extracts entity property dictionaries per document version. | Extracts directed entity relationship triples `(source, predicate, target)` with chunk locators. |
| **Deduplication Rule** | OKF owns entity scalar properties and CSV table schemas. Graph enrichment does not re extract or duplicate scalar entity properties. | Graph owns entity to entity directed edges and multi hop path expansion. OKF does not perform multi hop traversal. |

## Requirements

**User stories**:
- As a retrieval engineer, I want queries involving multi hop relationships (such as companies connected to partners of an organization) to retrieve verified relationship chains so that complex relationship questions are answered with exact chunk citations.
- As a retrieval engineer, I want graph candidates to pool into reciprocal rank fusion alongside independent BM25, vector, PageIndex, and OKF results so that an incomplete graph path never excludes true factual evidence found by baseline retrieval.
- As a system operator, I want graph extraction and traversal to enforce strict token, call, and latency budgets so that slow model calls or storage hiccups fall back immediately to baseline retrieval without delaying queries or failing document ingestion.
- As a tenant administrator, I want graph entities and relationships to be strictly partitioned by workspace identifier and document version so that proprietary organizational relationships never leak across workspace boundaries or surface tombstoned content.

**Acceptance criteria**:
- **AC-1**: Ingestion extracts and persists typed `KnowledgeGraphEntity` and `KnowledgeGraphRelationship` records per document version for text PDF and structured documents. Relationships represent directed triples `(source_entity, predicate, target_entity)` with verified source chunk locators.
- **AC-2**: Extracted relationships are validated against authorized active chunks before persistence. Candidate relationships must cite an active chunk belonging to the current document version, possess a valid locator, and contain the asserted relationship predicate context and both connected entities verbatim in the source chunk text. Unsupported claims are rejected as hallucinations and omitted from storage.
- **AC-3**: Graph ingestion extraction enforces strict resource bounds per document version: maximum 2 Bedrock completion calls, maximum 4000 total prompt input tokens (capped at 2000 per call), maximum 1600 total output tokens (capped at 800 per call), and a 10.0s total provider deadline per call (3.0s connect, 7.0s read). Retries are capped at at most 1 retry on throttling (HTTP 429), with zero retries on model errors, bad requests, or timeouts. Extraction is idempotent, keyed by `idempotency_key = sha256(f"{workspace_id}:{document_id}:{document_version}:kg")`. Replaying an existing version reuses persisted relationships with zero additional model calls. Baseline publication completes independently per ADR-001.
- **AC-4**: Durable budget accounting, lease expiry, stale worker fencing, retries, and crash recovery: Graph extraction enforces a durable ledger in `kre_knowledge_graph` (`PK = KG#{workspace_id}#DOC#{document_id}#V#{document_version}`, `SK = BUDGET#LEDGER`) that atomically records call reservations, cumulative token expenditures, active lease ownership, lease expiry timestamp, and a monotonically increasing fence token. Before making a provider call, a worker must atomically acquire a lease permit (`reserved_calls < max_calls`). The lease expires after 15 seconds. If a worker call takes longer than the lease window (or if the worker crashes) and another worker takes over by advancing the fence token, the late returning worker is fenced: its write is rejected and no relationships are persisted. The tokens consumed by the stale worker call are reconciled into the ledger cumulative token count. On worker crash or restart, budget recovery performs a strongly consistent read; if `reserved_calls >= max_calls` and extraction is uncompleted, the document version budget fails closed as exhausted with zero additional provider calls. Throttling retries (HTTP 429) must reuse the acquired lease slot within its window or count as a reserved call. Retries are strictly prohibited on timeouts, bad requests, or client errors.
- **AC-5**: Complete provider request token bounding and verification: Ingestion extraction strictly bounds complete Bedrock request payloads, including system prompt blocks, chunk text, formatting instructions, XML tags, and Converse API envelope wrapper overhead. The text payload is bounded by maximum 2000 characters per chunk and 8000 UTF-8 bytes total per call prompt. Converse API framing overhead is bounded by an empirically verified upper bound rather than an arbitrary heuristic. When Bedrock token counting API is unreachable, a conservative fallback formula `math.ceil(len(payload.encode("utf-8")) / 3)` is applied. Automated offline test fixtures verify that estimated request tokens equal or exceed token counts generated by official model tokenizer profiles across edge case payloads (dense punctuation, repeating whitespace, unicode sequences). Any prompt exceeding the 2000 input token ceiling fails closed before network dispatch with zero provider calls.
- **AC-6**: Storage and lookups are strictly partitioned by `workspace_id`. Graph edges are stored in `kre_knowledge_graph` using indexed partition keys (`KG#{workspace_id}#ENTITY#{source_id}`) and publication scoped sort keys (`PUB#{publication_id}#EDGE#{sanitized_predicate}#{target_id}#{relationship_id}`). Reverse relationships are indexed via GSI1 (`KG#{workspace_id}#TARGET#{target_id}`). A dedicated, workspace scoped entity name lookup index is maintained at `PK = KG#{workspace_id}#LOOKUP` with sort key `NAME#{normalized_canonical_name}#DOC#{document_id}#VER#{document_version}#ENT#{entity_id}`, mapping entity names to `entity_id` and document references. In memory hash calculation is used for exact entity name matching to eliminate redundant lookup queries. Multi hop traversal executes strictly via indexed queries against primary keys, GSI1, and the lookup index with bounded limits (maximum 10 edges per node, maximum 25 total nodes visited), with zero unconstrained table scans.
- **AC-7**: Active deletion tombstones in `WorkspaceBoundaryRepository` filter out relationships belonging to deleted source documents. Tombstone checks execute inside worker tasks and fail closed: any check error or workspace tombstone returns zero graph candidates immediately without throwing unhandled exceptions.
- **AC-8**: Retrieval latency targets and admission control: Graph lookups enforce a proposed target deadline of 75ms total wall clock time for cloud environments (50ms for local development and cache hits) across entity lookup, graph traversal, and chunk hydration. Lookups are governed by `GraphConcurrencyController` (maximum 4 concurrent workers, maximum 8 queued submissions). When a caller deadline expires, downstream storage tasks cancel immediately to prevent worker saturation. Expired lookups drop graph candidates cleanly, guarantee baseline fallback, and ensure zero late candidate publication.
- **AC-9**: Graph traversal bounds maximum expansion depth to 2 hops (or 3 hops when explicitly requested by causal query plans), applying cycle detection to prevent infinite loops. Chunks backing traversed edges are hydrated via targeted batch reads by chunk identifier inside the worker task, never loading entire workspace chunk sets into memory.
- **AC-10**: Candidate fusion and fast path bypass: Validated source chunks backing traversed edges are pooled into reciprocal rank fusion with a proposed initial weight of 1.10 (divided to 0.55 on entity ambiguity or conflicting edges), explicitly marked as proposed until measured and calibrated against the 60 question benchmark in Slice 2. Graph candidates never exclude or prune chunks from other retrieval strategies. Queries executing on the fast path (`plan.fast_path == True`) bypass graph traversal entirely. Zero model calls and zero embedding invocations are performed during graph retrieval.
- **AC-11**: Non destructive legacy cutover: Scoped retrieval in `KnowledgeGraphStrategy` queries exclusively through the new workspace partitioned table (`kre_knowledge_graph`), excluding old global unpartitioned records by construction. Legacy tables (`okf_entities`, `okf_properties`, `okf_relations`) and old data are preserved without modification, and deletion is explicitly deferred.
- **AC-12**: DocumentSnapshot exposes `graph_status`, `graph_entities_count`, and `graph_relations_count` without requiring dedicated administrative HTTP endpoints. Candidate chunk identifiers, latency, traversed node count, and edge match counts are exposed in `QueryResponse.retrieval_candidates` debug trace.
- **AC-13**: An offline ablation comparison suite evaluates candidate recall, precision, and latency with Knowledge Graph enabled versus disabled against the BM25 and vector baseline across canonical evaluation documents, including a counterexample test proving that an incorrect graph expansion does not prevent retrieval of the true answer page.

## Options considered

### Option 1: Bounded Workspace Scoped DynamoDB and SQLite Graph with Lease Reservations (Recommended)

Store graph relationships in dedicated workspace scoped DynamoDB records (`kre_knowledge_graph`) and SQLite tables. Enforce strict bounded multi hop traversal (max 2 hops, max 25 nodes) via indexed queries. Enforce ingestion budget leases via DynamoDB conditional writes and SQLite transactions. Pool candidate chunks into reciprocal rank fusion without hard exclusions.

**Pros**:
- Reuses existing DynamoDB and SQLite storage infrastructure without adding new database clusters.
- Guarantees strict multi tenant isolation via workspace prefixed partition keys.
- Adopts the durable budget accounting and fencing pattern proven in OKF Slice 3.1.
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
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    entity_id: str = Field(description="Deterministic hash of workspace and canonical name")
    workspace_id: str
    name: str
    entity_type: str = "Concept"
    document_ids: tuple[str, ...] = Field(default_factory=tuple)

class KnowledgeGraphRelationship(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    relationship_id: str = Field(description="Deterministic hash of workspace, entities, predicate, and chunk")
    workspace_id: str
    publication_id: str | None = None
    source_entity_id: str
    source_entity_name: str
    predicate: str
    target_entity_id: str
    target_entity_name: str
    directed: bool = True
    confidence: float = Field(ge=0.0, le=1.0)
    source_document_id: str
    source_document_version: str
    source_chunk_id: str
    location: DocumentLocation | TableLocation
    verbatim_evidence: str
    created_at_utc: str

class KnowledgeGraphManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    workspace_id: str
    document_id: str
    document_version: str
    publication_id: str
    status: GraphStatus
    entity_count: int
    relationship_count: int
    ingested_at_utc: str
    content_hash: str

class KnowledgeGraphLookupResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    seed_entities: tuple[str, ...]
    traversed_node_count: int
    edge_match_count: int
    matched_chunk_ids: tuple[str, ...]
    candidate_chunks: tuple[Any, ...] = Field(default_factory=tuple)
    latency_ms: float
    timed_out: bool = False

class KnowledgeGraphBudgetLedger(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    workspace_id: str
    document_id: str
    document_version: str
    reserved_calls: int = 0
    completed_calls: int = 0
    input_tokens_spent: int = 0
    output_tokens_spent: int = 0
    lease_owner_id: str = ""
    lease_expires_at: int = 0
    fence_token: int = 0
    status: str = "pending"
```

### 2. Storage Schema and Indexed Access Paths

Storage is implemented in `kre_knowledge_graph` (DynamoDB in cloud production, SQLite and in memory in local development):

- **Manifest Record**:
  - Partition Key (`PK`): `KG#{workspace_id}#DOC#{document_id}#V#{document_version}`
  - Sort Key (`SK`): `MANIFEST`
  - Attributes: `workspace_id`, `document_id`, `document_version`, `publication_id`, `status`, `entity_count`, `relationship_count`, `ingested_at_utc`, `content_hash`.

- **Directed Relationship Edge Records**:
  - Partition Key (`PK`): `KG#{workspace_id}#ENTITY#{source_entity_id}`
  - Sort Key (`SK`): `PUB#{publication_id}#EDGE#{sanitized_predicate}#{target_entity_id}#{relationship_id}`
  - Attributes: `workspace_id`, `publication_id`, `relationship_id`, `source_entity_id`, `source_entity_name`, `predicate`, `target_entity_id`, `target_entity_name`, `source_document_id`, `source_document_version`, `source_chunk_id`, `location_json`, `verbatim_evidence`, `confidence`, `created_at_utc`.
  - Predicate sanitization replaces `#` characters with `_` to guarantee deterministic sort key delimiter parsing.

- **Workspace Scoped Entity Name Lookup Records**:
  - Partition Key (`PK`): `KG#{workspace_id}#LOOKUP`
  - Sort Key (`SK`): `NAME#{normalized_canonical_name}#DOC#{document_id}#VER#{document_version}#ENT#{entity_id}`
  - Attributes: `workspace_id`, `entity_id`, `canonical_name`, `entity_type`, `document_id`, `document_version`, `created_at_utc`.
  - Preserves duplicate names across distinct documents and versions while enabling deterministic prefix queries (`begins_with`) for bounded seed entity resolution without table scans.
  - In memory hash computation `sha256(f"{workspace_id}:{normalized_name}".encode("utf-8")).hexdigest()[:16]` resolves exact seed entity names directly, bypassing the lookup table and saving one sequential network round trip.

- **Durable Extraction Budget Ledger Records**:
  - Partition Key (`PK`): `KG#{workspace_id}#DOC#{document_id}#V#{document_version}`
  - Sort Key (`SK`): `BUDGET#LEDGER`
  - Attributes: `workspace_id`, `document_id`, `document_version`, `reserved_calls`, `completed_calls`, `input_tokens_spent`, `output_tokens_spent`, `lease_owner_id`, `lease_expires_at`, `fence_token`, `status`, `updated_at_utc`.

- **Global Secondary Index 1 (GSI1 Incoming and Reverse Edges)**:
  - Partition Key (`GSI1_PK`): `KG#{workspace_id}#TARGET#{target_entity_id}`
  - Sort Key (`GSI1_SK`): `PUB#{publication_id}#EDGE#{sanitized_predicate}#{source_entity_id}#{relationship_id}`
  - Projection: `ALL` (or key attributes required for traversal).

- **Global Secondary Index 2 (GSI2 Document Version Scoping and Cleanup)**:
  - Partition Key (`GSI2_PK`): `KG#{workspace_id}#DOC#{document_id}#V#{document_version}`
  - Sort Key (`GSI2_SK`): `PUB#{publication_id}#EDGE#{relationship_id}`
  - Used for atomic version invalidation and document deletion.

### 3. Traversal Algorithm

The traversal algorithm executes without full table scans or unbounded recursion:

1. **Seed Entity Resolution**: Match query tokens deterministically against known entity canonical names in the active workspace. When query tokens exactly match an entity name, compute `entity_id` in memory to eliminate a network query. For partial or prefix tokens, query `PK = KG#{workspace_id}#LOOKUP` with `SK begins_with NAME#{query_token}` (Limit 5), deduplicating entity matches across distinct documents and versions. Zero model calls and zero table scans are performed.
2. **Hop 1 Expansion**:
   - For each seed entity, query outgoing edges: `PK = KG#{workspace_id}#ENTITY#{seed_id}` with `SK begins_with PUB#{pub_id}#EDGE#` (Limit 10).
   - Query incoming edges via GSI1: `GSI1_PK = KG#{workspace_id}#TARGET#{seed_id}` with `GSI1_SK begins_with PUB#{pub_id}#EDGE#` (Limit 10).
   - Add destination entities to the frontier and mark visited: `visited_entities.add(seed_id)`.
3. **Hop 2 Expansion** (if `max_hops >= 2` and total visited nodes < 25):
   - Sort unvisited frontier nodes by edge confidence descending, selecting top 5 nodes.
   - For each selected node, query outgoing edges with Limit 5.
   - Add to `visited_entities`. If an edge leads to an already visited entity, skip to prevent cycles.
4. **Hop 3 Expansion** (only when `max_hops >= 3` on explicit causal query plans):
   - Cap frontier expansion at top 3 nodes with Limit 3 outgoing edges per node, enforcing overall 25 node ceiling.
5. **Targeted Chunk Hydration**:
   - Collect distinct `source_chunk_id` values from the traversed edges (capped at 15 chunk IDs).
   - Fetch only these specific chunk records by primary key using `batch_get_item`, never scanning or querying all workspace chunks.
   - Filter chunks against active deletion tombstones in `WorkspaceBoundaryRepository`.
6. **Output Assembly**: Convert hydrated chunks into `EvidenceItem` records with graph provenance metadata detailing seed entities, traversed edge predicates, and hop distance.

### 4. Ingestion Extraction Policy and Chunk Selection

- **Chunk Selection Heuristic**: Ingest the first 3 introductory chunks and up to 3 highest entity density narrative chunks (selected by named entity keyword frequency from baseline ingestion), bounded by 4000 total prompt tokens across at most 2 Bedrock calls.
- **Tabular Documents**: Tabular CSV documents are processed deterministically from column headers, primary keys, and table relationships with zero model calls.
- **Provider Output Schema**: Model outputs a strict JSON array of objects:
  ```json
  [
    {
      "source_entity": "Acme Corp",
      "predicate": "ACQUIRED",
      "target_entity": "Beta Tech",
      "verbatim_evidence": "Acme Corp completed the acquisition of Beta Tech in 2024",
      "confidence": 0.95
    }
  ]
  ```
- **Verbatim Grounding Rule**: Both `source_entity` and `target_entity` must appear verbatim in the source chunk text. The `verbatim_evidence` string must be an exact substring of the chunk text containing the asserted relationship context.

### 5. API Surface

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| Ingestion background task | Internal | `workspace_id`, `document_id`, `document_version`, `chunks` | `KnowledgeGraphManifest` | Internal worker token | 422 (malformed chunk), 429 (throttled) |
| Pipeline `run_graph` | Internal | `query`, `workspace_id`, `plan`, `limits` | `list[EvidenceItem]`, telemetry | Session principal | Non blocking fallback to empty list |

### 6. Value Sourcing

| Action | Value produced / displayed | Source |
|---|---|---|
| Ingestion extraction | `entity_id` | Deterministic SHA256 hash of `workspace_id` and normalized canonical entity name, truncated to 16 hex characters |
| Ingestion extraction | `relationship_id` | Deterministic SHA256 hash of `workspace_id`, `source_entity_id`, sanitized predicate, `target_entity_id`, and `source_chunk_id`, truncated to 24 hex characters |
| Ingestion extraction | `publication_id` | Fresh UUID generated per extraction attempt to isolate staging |
| Ingestion extraction | `source_chunk_id` | Chunk identifier from active document version chunk metadata |
| Ingestion extraction | `location` | `DocumentLocation` or `TableLocation` copied from source chunk metadata |
| Ingestion extraction | `verbatim_evidence` | Exact substring from source chunk text containing predicate context and both entities |
| Ingestion extraction | `confidence` | Extraction confidence score asserted by Bedrock model call (bounded 0.0 to 1.0) |
| Ingestion extraction | `budget_ledger` | Atomic item update on `PK = KG#{ws}#DOC#{doc}#V#{ver}`, `SK = BUDGET#LEDGER` |
| Seed entity resolution | `matched_entity_ids` | In memory hash for exact match, or queried from `PK = KG#{ws}#LOOKUP` with prefix `NAME#{token}` |
| Hop 1 expansion | `outgoing_edges` | Queried from `PK = KG#{workspace_id}#ENTITY#{seed_id}`, limit 10 |
| Hop 1 expansion | `incoming_edges` | Queried from `GSI1_PK = KG#{workspace_id}#TARGET#{seed_id}`, limit 10 |
| Hop 2 expansion | `hop2_edges` | Queried from top 5 unvisited frontier entities, limit 5 per node, overall node ceiling 25 |
| Targeted chunk hydration | `hydrated_chunks` | `batch_get_item` by chunk ID from chunks table inside worker task, filtered by tombstones |
| Reciprocal rank fusion | `graph_candidate_chunks` | Hydrated chunks pooled with weight 1.10 (halved to 0.55 if ambiguous or conflicting) |
| Telemetry and snapshot | `graph_status` | Status enum on `DocumentSnapshot` ("pending", "processing", "ready", "failed", "unavailable") |
| Telemetry and snapshot | `graph_entities_count`, `graph_relations_count` | Aggregate entity and relationship counts from `KnowledgeGraphManifest` |
| Telemetry and trace | `retrieval_candidates.graph` | Debug dict in `QueryResponse` detailing latency, matched IDs, visited node count |

### 7. Key Invariants

1. Multi tenant isolation: All partition keys must begin with `KG#{workspace_id}#`. Cross workspace queries return zero entities and zero edges by construction.
2. Verbatim grounding: Every persisted relationship must have its predicate context and both entity names appear verbatim in the cited chunk text. Hallucinated relationships are discarded.
3. Strict budget ceilings: A document version never consumes more than 2 Bedrock calls, 4000 total prompt tokens, and 1600 output tokens.
4. ADR-001 decoupling: Graph extraction or lookup failures must never block baseline document publication or baseline query retrieval.
5. Zero table scans: All storage queries must run against partition keys or indexed sort key prefixes with bounded limits. Whole table or workspace scans are prohibited.
6. Deterministic retrieval: Retrieval traversals perform zero model calls and zero embedding invocations.

### 8. Security Model

- Tenant scoping: All reads and writes enforce the active `workspace_id` from the caller context.
- Tombstone enforcement: Tombstoned documents and workspaces immediately filter out associated graph candidates inside worker tasks.
- Non destructive cutover: Legacy global tables are not touched or dropped during migration.

### 9. Configuration Required

Registered in `backend/src/config.py`:
- `GRAPH_ENABLED: bool = True` (flag to toggle graph enrichment extraction and traversal)
- `GRAPH_TIMEOUT_MS: int = 75` (total retrieval deadline across lookup, traversal, and hydration)
- `GRAPH_MAX_INPUT_TOKENS_PER_DOC: int = 4000` (input token ceiling for document extraction)
- `GRAPH_MAX_OUTPUT_TOKENS_PER_DOC: int = 1600` (output token ceiling for document extraction)
- `GRAPH_MAX_MODEL_CALLS_PER_DOC: int = 2` (maximum Bedrock calls per document version)
- `GRAPH_BEDROCK_MODEL_ID: str = "anthropic.claude-3-haiku-20240307-v1:0"` (provider model)
- `GRAPH_FUSION_WEIGHT: float = 1.10` (reciprocal rank fusion initial weight)
- `GRAPH_AMBIGUOUS_FUSION_WEIGHT: float = 0.55` (calibrated weight under ambiguity or conflicts)
- `GRAPH_MAX_HOPS: int = 2` (default expansion depth)
- `GRAPH_MAX_NODES: int = 25` (ceiling for total visited nodes)
- `GRAPH_MAX_EDGES_PER_NODE: int = 10` (maximum edges queried per node)
- `GRAPH_MAX_CONCURRENCY: int = 4` (worker pool size in concurrency controller)
- `GRAPH_MAX_QUEUE: int = 8` (admission queue limit in concurrency controller)
- `DYNAMODB_GRAPH_TABLE_NAME: str = "kre_knowledge_graph"` (DynamoDB table name)

### 10. Critical Test Scenarios

- Happy path extraction: Ingest a multi page text PDF with clear entity relationships; verify persisted entities and relationships with verbatim chunk evidence, verifies **AC-1**, **AC-2**.
- Verbatim grounding rejection: Mock model output containing relationship not present in source chunk text; verify extraction discards it, verifies **AC-2**.
- Resource budget and idempotency: Ingest document version twice; verify second ingestion makes zero provider calls, verifies **AC-3**.
- Durable budget fencing: Simulate slow or crashed worker whose 15 second lease expires; verify late write is rejected and fence token prevents state corruption, verifies **AC-4**.
- Request token bounding: Provide prompt with dense unicode and whitespace; verify conservative token estimation bounds request before network dispatch, verifies **AC-5**.
- Bounded multi hop traversal: Create cyclic graph with 30 nodes; verify traversal terminates at 2 hops and maximum 25 visited nodes without infinite loop, verifies **AC-6**, **AC-9**.
- Targeted chunk hydration: Query graph with 3 matching edges; verify only 3 chunk IDs are fetched via batch get without reading all workspace chunks, verifies **AC-9**.
- Concurrency and deadline enforcement: Inject 100ms storage delay; verify caller deadline drops graph candidates, cancels background task, and falls back cleanly to baseline retrieval, verifies **AC-8**.
- Cross tenant isolation: Query graph with different workspace ID; verify zero candidate leakage, verifies **AC-6**.
- Tombstone enforcement: Insert deletion tombstone for source document; verify graph edges from that document are excluded from traversal results, verifies **AC-7**.
- Reciprocal rank fusion: Verify graph candidate chunks pool into fusion with weight 1.10 and fast path bypasses graph lookup entirely, verifies **AC-10**.
- Offline ablation comparison: Run ablation suite across canonical evaluation documents; verify candidate recall and counterexample discovery safety, verifies **AC-13**.

## Build plan

Following the Tracer Bullet build approach, this feature is implemented across two vertical slices:

### Slice 1: Contracts, Storage Schema, Grounded Extraction, Durable Budgeting, and Bounded Traversal

- [ ] 1. **Typed Knowledge Graph Contracts**: Define `KnowledgeGraphEntity`, `KnowledgeGraphRelationship`, `KnowledgeGraphManifest`, `KnowledgeGraphLookupResult`, and `KnowledgeGraphBudgetLedger` in `backend/src/schemas/contracts/knowledge_graph.py` with immutable fields and deterministic ID hashing, satisfying **AC-1**, **AC-4**, **AC-6**.
- [ ] 2. **Partitioned Storage Repository**: Implement `KnowledgeGraphRepository` supporting DynamoDB, SQLite, and in memory stores with Main table partition keys (`KG#{workspace_id}#ENTITY#{source_id}`), sort keys (`PUB#{pub_id}#EDGE#{predicate}#{target_id}`), lookup index (`KG#{workspace_id}#LOOKUP`), GSI1 (reverse edges), GSI2 (document version scoping), durable budget ledger (`BUDGET#LEDGER`), staged publication, and tombstone checks, satisfying **AC-4**, **AC-6**, **AC-7**, **AC-11**.
- [ ] 3. **Grounded Ingestion Extraction**: Implement `KnowledgeGraphBuilder` in `backend/src/ingestion/graph_builder.py` extracting canonical entity relationship triples from PDF and text chunks with strict Bedrock completion caps (max 2 calls, 4000 total prompt tokens, 1600 total output tokens), idempotency hashing, and verbatim chunk grounding validation rejecting unsupported claims, satisfying **AC-1**, **AC-2**, **AC-3**.
- [ ] 4. **Durable Budget Accounting and Stale Worker Fencing**: Integrate atomic pre call budget reservations, 15 second lease expiries, monotonic fence tokens, stale worker fencing, crash recovery fail closed semantics, and authoritative request token bounding (`count_tokens` with conservative estimation fallback), satisfying **AC-3**, **AC-4**, **AC-5**.
- [ ] 5. **Bounded Multi Hop Traversal and Concurrency Admission**: Implement `KnowledgeGraphRetriever` and `GraphConcurrencyController` enforcing seed entity resolution with in memory hash fast path, bounded breadth first traversal (max 2 hops, max 25 nodes, max 10 edges per node), cycle detection, targeted chunk hydration via `batch_get_item`, 75ms caller deadline, early task cancellation, and zero table scans, satisfying **AC-6**, **AC-8**, **AC-9**.
- [ ] 6. **Slice 1 Unit and Regression Test Suite**: Implement comprehensive test suite in `backend/tests/test_knowledge_graph_slice1.py` verifying contract immutability, verbatim grounding validation, budget ledger fencing, crash recovery, multi hop traversal limits, cycle detection, targeted chunk batching, tombstone filtering, and boundary isolation, satisfying **AC-1**, **AC-2**, **AC-3**, **AC-4**, **AC-5**, **AC-6**, **AC-7**, **AC-8**, **AC-9**.

### Slice 2: Pipeline Fusion, Telemetry, and Offline Ablation

- [ ] 7. **Pipeline Fusion and Fast Path Bypass**: Implement `KnowledgeGraphStrategy` and wire `run_graph` in `backend/src/services/langgraph_pipeline.py`, routing graph candidates into reciprocal rank fusion with calibrated initial weight (1.10, halved to 0.55 on entity ambiguity or conflicting edges) while bypassing graph lookups entirely on fast path queries, satisfying **AC-10**.
- [ ] 8. **Decoupled Baseline Ingestion and Snapshot Metadata**: Wire graph extraction as a decoupled background enrichment pass in `backend/src/modules/documents/documents_service.py`, ensuring baseline publication completes independently per ADR-001, and exposing `graph_status`, `graph_entities_count`, and `graph_relations_count` in `DocumentSnapshot`, satisfying **AC-3**, **AC-11**, **AC-12**.
- [ ] 9. **Retrieval Candidate Telemetry Trace**: Expose `graph_candidate_ids`, `graph_latency_ms`, `graph_traversed_nodes`, and `graph_matched_edges` in `QueryResponse.retrieval_candidates` debug trace, satisfying **AC-12**.
- [ ] 10. **Offline Ablation Comparison Suite**: Implement evaluation harness in `backend/tests/test_knowledge_graph_ablation_slice2.py` comparing candidate page recall, precision, and latency with Knowledge Graph enabled versus disabled across canonical evaluation documents, including counterexample test proving incorrect graph expansion never displaces true answer pages, satisfying **AC-10**, **AC-13**.
- [ ] 11. **Benchmark Comparison Barrier**: Enforce comparator rules ensuring no benchmark accuracy claims are made until controlled canonical comparison run executes on `ws_canonical_comparison_0004`, satisfying **AC-13**.

## Migration plan

**Strategy**: Strangler pattern with non destructive legacy cutover.

**Phases**:
1. Phase 1: Deploy new partitioned `KnowledgeGraphRepository` and typed contracts (`kre_knowledge_graph`) alongside existing codebase without modifying active query routing. Legacy tables (`okf_entities`, `okf_properties`, `okf_relations`) remain intact.
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
- Enables multi hop indirect relationship discovery with exact chunk citations.
- Grounding verification rejects unsupported model claims, ensuring only verified relations enter the index.
- Targeted batch get chunk hydration replaces disastrous whole workspace scans.
- Binds ingestion extraction to a predictable token budget with idempotency replay and durable fencing.
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
