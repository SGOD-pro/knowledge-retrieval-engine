# 0007. Bounded Knowledge Graph Enrichment and Multi Hop Relationship Traversal

**Date**: 2026-11-01
**Status**: Proposed

## Summary

This specification defines the bounded integration of Knowledge Graph enrichment as the next verifiable milestone after accepted specifications 0004, 0005, and 0006. Knowledge Graph enrichment extracts canonical entity relationships and inter entity directed connections from ingested text PDF and tabular documents. It enables bounded multi hop relationship traversal to uncover indirect connections across documents with verified chunk provenance. 

It replaces legacy unpartitioned global DynamoDB tables and unconstrained table scans with a typed, workspace scoped relationship schema. Grounding validation rejects unsupported model claims by verifying that relationships and connected entities appear verbatim in authorized source chunks. Baseline BM25, vector discovery, PageIndex structural navigation, and TableStore deterministic aggregate execution remain fully independent of graph enrichment. Baseline retrieval serves as the guaranteed operational fallback per ADR-001 so that any extraction timeout, storage failure, or missing relationship falls back immediately without degrading core query execution.

## Dependencies on Specification 0006 (OKF)

Knowledge Graph enrichment depends on specification 0006 (Open Knowledge Framework) for single entity definitions, property assertions, and tabular schemas. Specification 0006 previously identified two open operational limitations: distributed cross process extraction budget coordination and request token accounting verification. These limitations were closed and verified in OKF via Slice 3.1. Knowledge Graph enrichment builds upon the durable budget coordination (`BUDGET#DOC#{doc}#VER#{ver}`, monotonic fence tokens, 15 second lease expiry, crash recovery) and authoritative request token accounting established in specification 0006.

## Context

The repository has accepted specification 0004 (Core engine walking skeleton and benchmarks), specification 0005 (Bounded PageIndex structural retrieval), and specification 0006 (Bounded Open Knowledge Framework integration), while preserving specification 0003 (Snapshot registry, publication, and atomic deletion) as proposed and deferred. The engine supports multi tenant workspace isolation, tabular data processing without row truncation, hybrid lexical and vector retrieval, deterministic layout navigation for PDF documents, and grounded entity property assertions.

An audit of the existing brownfield graph components revealed five critical architecture flaws that prevent production use:
1. Cross tenant data leakage: The legacy graph repository in `backend/src/modules/graph/graph_repository.py` reads from global unpartitioned DynamoDB tables (`okf_entities`, `okf_properties`, `okf_relations`) that lack workspace identifiers in their partition keys. Additionally, `backend/src/modules/graph/graph_router.py` exposes `/documents/graph`, which queries across all tenants with no workspace scoping.
2. Unconstrained storage scans: `GraphRepository.get_workspace_graph` executes `self.okf_entities_table.scan(Limit=50)`, performing full table scans across all tenant data.
3. Unscalable chunk hydration: In `backend/src/services/retrieval/strategies/knowledge_graph.py:75`, resolving traversed edges calls `self.repository.get_all_chunks(workspace_id=workspace_id)`, loading all chunks in the entire workspace into memory to resolve a few chunk identifiers.
4. Broken runtime contracts: `GraphRetriever` in `backend/src/services/retrieval/graph_retriever.py` calls `self.repository.expand_graph(start_entities, max_hops)`. `CloudRepository` does not implement `expand_graph`, causing runtime failures that are silently swallowed in `langgraph_pipeline.py`.
5. Unbudgeted extraction and missing concurrency controls: Legacy extraction invokes unconstrained language model calls on every document batch without token budgets, timeouts, or multi process safety.

Architecture Decision Record ADR-001 (Baseline Publication Independent of Enrichment) governs this integration. ADR-001 dictates that baseline retrieval serves as the operational baseline, while optional enrichments are decoupled. Under no circumstances may an incomplete, slow, or failing graph extraction or traversal block baseline operations.

## Scope Reconciliation (Feature 7)

In `docs/scope/scope.md`, the At a glance table previously labeled Feature 7 as `Baseline discovery retrieval and fusion`, while section 7 and the roadmap labeled it as `Slice 4: Knowledge graph enrichment`. 

This specification reconciles that mismatch: Feature 7 is formally defined as `Knowledge graph enrichment`. Baseline discovery retrieval, lexical search, vector indexing, and TableStore aggregation were already established and verified in specification 0004 (Feature 4). Slice 4 builds upon that verified baseline by introducing bounded inter entity graph traversal.

## Overlap Analysis with OKF (Spec 0006)

To prevent architectural bloat and duplicate work, the division of responsibility between OKF and Knowledge Graph enrichment is strictly defined:

| Dimension | Open Knowledge Framework (Spec 0006) | Knowledge Graph Enrichment (Spec 0007) |
|---|---|---|
| **Primary Scope** | Entity attributes and scalar properties (e.g. Acme Corp revenue is 500 Cr; CEO is Jane Doe). | Directed relationships connecting two distinct entities (e.g. Acme Corp acquired Beta Tech; Beta Tech operates in Region X). |
| **Tabular Role** | Curated table schemas, column data types, units, and header definitions. | Entity relationships discovered across data records and narrative text. |
| **Query Mechanism** | Single hop property lookup given an entity name and optional property key. | Bounded multi hop traversal (1 to 2 hops, max 3) discovering indirect chains across documents. |
| **Model Invocations** | Extracts entity property dictionaries per document version. | Extracts directed entity relationship triples `(source, predicate, target)` with chunk locators. |
| **Deduplication Rule** | OKF owns entity scalar properties and CSV table schemas. Graph enrichment does not re extract or duplicate scalar entity properties. | Graph owns entity to entity directed edges and multi hop path expansion. OKF does not perform multi hop traversal. |

## Requirements

**User stories**:
- As a retrieval engineer, I want queries involving multi hop relationships (e.g. companies connected to partners of an organization) to retrieve verified relationship chains so that complex relationship questions are answered with exact chunk citations.
- As a retrieval engineer, I want graph candidates to pool into reciprocal rank fusion alongside independent BM25, vector, PageIndex, and OKF results so that an incomplete graph path never excludes true factual evidence found by baseline retrieval.
- As a system operator, I want graph extraction and traversal to enforce strict token, call, and latency budgets so that slow model calls or storage hiccups fall back immediately to baseline retrieval without delaying queries or failing document ingestion.
- As a tenant administrator, I want graph entities and relationships to be strictly partitioned by workspace identifier and document version so that proprietary organizational relationships never leak across workspace boundaries or surface tombstoned content.

**Acceptance criteria**:
- **AC-1**: Ingestion extracts and persists typed `KnowledgeGraphEntity` and `KnowledgeGraphRelationship` records per document version for text PDF and structured documents. Relationships represent directed triples `(source_entity, predicate, target_entity)` with verified source chunk locators.
- **AC-2**: Extracted relationships are validated against authorized active chunks before persistence. Candidate relationships must cite an active chunk belonging to the current document version, possess a valid locator, and contain the asserted relationship predicate and both connected entities verbatim in the source chunk text. Unsupported claims are rejected as hallucinations and omitted from storage.
- **AC-3**: Graph ingestion extraction enforces strict resource bounds per document version: maximum 2 Bedrock completion calls, maximum 4000 total prompt input tokens (capped at 2000 per call), maximum 1600 total output tokens (capped at 800 per call), and a 10.0s total provider deadline per call (3.0s connect, 7.0s read). Retries are capped at at most 1 retry on throttling (HTTP 429), with zero retries on model errors, bad requests, or timeouts. Extraction is idempotent, keyed by `idempotency_key = sha256(f"{workspace_id}:{document_id}:{document_version}:kg")`. Replaying an existing version reuses persisted relationships with zero additional model calls. Baseline publication completes independently per ADR-001.
- **AC-4**: Durable budget accounting, lease expiry, stale worker fencing, retries, and crash recovery: A conditional lease alone is not proof that spending stays within budget. Graph extraction enforces a durable ledger in `kre_knowledge_graph` (`PK = KG#{workspace_id}#DOC#{document_id}#V#{document_version}`, `SK = BUDGET#LEDGER`) that atomically records call reservations, cumulative token expenditures, active lease ownership, lease expiry timestamp, and a monotonically increasing fence token. Before making a provider call, a worker must atomically acquire a lease permit (`reserved_calls < max_calls`). The lease expires after 15 seconds. If a worker call takes longer than the lease window (or if the worker crashes) and another worker takes over by advancing the fence token, the late returning worker is fenced: its write is rejected and no relationships are persisted. However, the tokens consumed by the stale worker call are reconciled into the ledger cumulative token count. On worker crash or restart, budget recovery performs a strongly consistent read; if `reserved_calls >= max_calls` and extraction is uncompleted, the document version budget fails closed as exhausted with zero additional provider calls. Throttling retries (HTTP 429) must reuse the acquired lease slot within its window or count as a reserved call. Retries are strictly prohibited on timeouts, bad requests, or client errors.
- **AC-5**: Complete provider request token bounding and verification: Ingestion extraction strictly bounds complete Bedrock request payloads, including system prompt blocks, chunk text, formatting instructions, XML tags, and Converse API envelope wrapper overhead. The text payload is bounded by maximum 2000 characters per chunk and 8000 UTF-8 bytes total per call prompt. Converse API framing overhead is bounded by an empirically verified upper bound rather than an arbitrary heuristic. Automated offline test fixtures verify that estimated request tokens equal or exceed token counts generated by official model tokenizer profiles across edge case payloads (dense punctuation, repeating whitespace, unicode sequences). Any prompt exceeding the 2000 input token ceiling fails closed before network dispatch with zero provider calls.
- **AC-6**: Storage and lookups are strictly partitioned by `workspace_id`. Graph edges are stored in `kre_knowledge_graph` using indexed partition keys (`KG#{workspace_id}#ENTITY#{source_id}`) and sorted ranges (`EDGE#{predicate}#{target_id}`). Reverse relationships are indexed via GSI1 (`KG#{workspace_id}#TARGET#{target_id}`). A dedicated, workspace scoped entity name lookup index is maintained at `PK = KG#{workspace_id}#LOOKUP` with sort key `NAME#{normalized_canonical_name}`, mapping entity names to `entity_id` and document references. Multi hop traversal executes strictly via indexed queries against primary keys, GSI1, and the lookup index with bounded limits (maximum 10 edges per node, maximum 25 total nodes visited), with zero unconstrained table scans.
- **AC-7**: Active deletion tombstones in `WorkspaceBoundaryRepository` filter out relationships belonging to deleted source documents. Tombstone checks execute inside worker tasks and fail closed: any check error or workspace tombstone returns zero graph candidates immediately without throwing unhandled exceptions.
- **AC-8**: Retrieval latency targets and admission control: Graph lookups enforce a proposed target deadline of 50ms total wall clock time across entity lookup, graph traversal, and chunk hydration, explicitly marked as a proposed target pending empirical measurement and calibration during Slice 2 ablation. Lookups are governed by `GraphConcurrencyController` (maximum 4 concurrent workers, maximum 8 queued submissions). Concurrency capacity is retained until underlying storage worker threads finish executing. Expired lookups drop graph candidates cleanly, guarantee baseline fallback, and ensure zero late candidate publication.
- **AC-9**: Graph traversal bounds maximum expansion depth to 2 hops (or 3 hops when explicitly requested by causal query plans), applying cycle detection to prevent infinite loops. Chunks backing traversed edges are hydrated via targeted batch reads by chunk identifier, never loading entire workspace chunk sets.
- **AC-10**: Candidate fusion and fast path bypass: Validated source chunks backing traversed edges are pooled into reciprocal rank fusion with a proposed initial weight of 1.10 (divided to 0.55 on entity ambiguity or conflicting edges), explicitly marked as proposed until measured and calibrated against the 60 question benchmark in Slice 2. Graph candidates never exclude or prune chunks from other retrieval strategies. Queries executing on the fast path (`plan.fast_path == True`) bypass graph traversal entirely. Zero model calls and zero embedding invocations are performed during graph retrieval.
- **AC-11**: Non destructive legacy cutover: Scoped retrieval in `KnowledgeGraphStrategy` queries exclusively through the new workspace partitioned table (`kre_knowledge_graph`), excluding old global unpartitioned records by construction. Legacy tables (`okf_entities`, `okf_properties`, `okf_relations`) and old data are preserved without modification, and deletion is explicitly deferred.
- **AC-12**: DocumentSnapshot exposes `graph_status`, `graph_entities_count`, and `graph_relations_count` without requiring dedicated administrative HTTP endpoints. Candidate chunk identifiers, latency, traversed node count, and edge match counts are exposed in `QueryResponse.retrieval_candidates` debug trace.
- **AC-13**: An offline ablation comparison suite evaluates candidate recall, precision, and latency with Knowledge Graph enabled versus disabled against the BM25 and vector baseline across canonical evaluation documents, including a counterexample test proving that an incorrect graph expansion does not prevent retrieval of the true answer page.

## Architecture and Design

### 1. Data Model and Contracts

Shared frozen Pydantic contracts are added to `backend/src/schemas/contracts/knowledge_graph.py`:

```python
from enum import StrEnum
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

class KnowledgeGraphManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    workspace_id: str
    document_id: str
    document_version: str
    status: GraphStatus
    entity_count: int
    relationship_count: int
    ingested_at_utc: str
    content_hash: str
```

### 2. Storage Schema and Indexed Access Paths

Storage is implemented in `kre_knowledge_graph` (DynamoDB in cloud production, SQLite in local development):

* **Directed Relationship Edge Records**:
  * Partition Key (`PK`): `KG#{workspace_id}#ENTITY#{source_entity_id}`
  * Sort Key (`SK`): `EDGE#{predicate}#{target_entity_id}#{relationship_id}`
  * Attributes: `workspace_id`, `relationship_id`, `source_entity_name`, `target_entity_name`, `source_document_id`, `source_document_version`, `source_chunk_id`, `location_json`, `verbatim_evidence`, `confidence`, `created_at_utc`.

* **Workspace Scoped Entity Name Lookup Records**:
  * Partition Key (`PK`): `KG#{workspace_id}#LOOKUP`
  * Sort Key (`SK`): `NAME#{normalized_canonical_name}`
  * Attributes: `workspace_id`, `entity_id`, `canonical_name`, `entity_type`, `document_ids`, `created_at_utc`.
  * Enables deterministic, bounded seed entity resolution for queries without table scans.

* **Durable Extraction Budget Ledger Records**:
  * Partition Key (`PK`): `KG#{workspace_id}#DOC#{document_id}#V#{document_version}`
  * Sort Key (`SK`): `BUDGET#LEDGER`
  * Attributes: `workspace_id`, `document_id`, `document_version`, `reserved_calls`, `completed_calls`, `input_tokens_spent`, `output_tokens_spent`, `lease_owner_id`, `lease_expires_at`, `fence_token`, `status`, `updated_at_utc`.

* **Global Secondary Index 1 (GSI1 - Incoming and Reverse Edges)**:
  * Partition Key (`GSI1_PK`): `KG#{workspace_id}#TARGET#{target_entity_id}`
  * Sort Key (`GSI1_SK`): `EDGE#{predicate}#{source_entity_id}#{relationship_id}`
  * Projection: `ALL` (or key attributes required for traversal).

* **Global Secondary Index 2 (GSI2 - Document Version Scoping and Cleanup)**:
  * Partition Key (`GSI2_PK`): `KG#{workspace_id}#DOC#{document_id}#V#{document_version}`
  * Sort Key (`GSI2_SK`): `EDGE#{relationship_id}`
  * Used for atomic version invalidation and document deletion.

### 3. Durable Budget Accounting, Stale Worker Fencing, and Token Verification

A conditional lease alone is not proof that spending stays within budget. If a worker acquires a lease but experiences a latency spike exceeding the lease duration, a second worker could acquire the expired lease. If both workers then dispatch Bedrock calls, the document version exceeds its 2 call budget ceiling. Similarly, if a worker crashes after dispatching a provider call, releasing the lease blindly would permit unbudgeted retries.

To ensure extraction spending stays strictly within budget across distributed workers and crashes, graph extraction enforces a durable ledger protocol:

```python
# 1. Atomic pre call reservation with monotonic fencing
now_ts = int(time.time())
worker_id = str(uuid.uuid4())
lease_window_s = 15

# Conditional reservation: acquire only if reserved_calls < max_calls
# and (no active lease exists OR active lease has expired OR lease is owned by this worker)
try:
    response = table.update_item(
        Key={"PK": ledger_pk, "SK": "BUDGET#LEDGER"},
        UpdateExpression=(
            "SET reserved_calls = if_not_exists(reserved_calls, :zero) + :one, "
            "fence_token = if_not_exists(fence_token, :zero) + :one, "
            "lease_owner_id = :worker_id, "
            "lease_expires_at = :expires_at, "
            "status = :in_progress"
        ),
        ConditionExpression=(
            "(attribute_not_exists(reserved_calls) OR reserved_calls < :max_calls) AND "
            "(attribute_not_exists(lease_expires_at) OR lease_expires_at < :now OR lease_owner_id = :worker_id)"
        ),
        ExpressionAttributeValues={
            ":zero": 0,
            ":one": 1,
            ":max_calls": 2,
            ":worker_id": worker_id,
            ":expires_at": now_ts + lease_window_s,
            ":now": now_ts,
            ":in_progress": "in_progress",
        },
        ReturnValues="ALL_NEW",
    )
    my_fence_token = response["Attributes"]["fence_token"]
except ClientError as e:
    if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
        # Budget exhausted or active lease held by peer worker
        logger.warning("Graph extraction budget exhausted or lease locked by peer")
        return False
```

```python
# 2. Stale worker fencing on result writeback
# When the call completes, the worker checks its fence_token.
# If lease expired and another worker advanced fence_token, this write fails.
try:
    table.update_item(
        Key={"PK": ledger_pk, "SK": "BUDGET#LEDGER"},
        UpdateExpression=(
            "SET completed_calls = if_not_exists(completed_calls, :zero) + :one, "
            "input_tokens_spent = if_not_exists(input_tokens_spent, :zero) + :in_toks, "
            "output_tokens_spent = if_not_exists(output_tokens_spent, :zero) + :out_toks, "
            "lease_expires_at = :zero"
        ),
        ConditionExpression="fence_token = :my_fence",
        ExpressionAttributeValues={
            ":zero": 0,
            ":one": 1,
            ":in_toks": actual_input_tokens,
            ":out_toks": actual_output_tokens,
            ":my_fence": my_fence_token,
        },
    )
except ClientError as e:
    if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
        # Worker was fenced; discard extracted relationships to prevent corrupting state.
        # Still record consumed tokens to keep cumulative accounting honest.
        logger.warning("Worker fenced out; discarding stale extracted relationships")
        _record_fenced_token_spend(ledger_pk, actual_input_tokens, actual_output_tokens)
        return False
```

**Crash Recovery and Fail Closed Semantics**:
When a worker starts or restarts, it performs a strongly consistent read on the budget ledger. If `reserved_calls >= max_calls` and status is not `completed`, the document version budget is marked `exhausted`. No further provider calls are permitted, failing closed per ADR-001 so that baseline search remains functional.

**Retries and Throttling Policy**:
Retries are capped at at most 1 retry on throttling (HTTP 429), and only when the retry delay fits within the active lease window. Zero retries are permitted on model errors, bad requests, or client timeouts. A retry acquires a separate atomic durable reservation while reusing the active lease owner identity; it never creates an unreserved provider call.

**Complete Provider Request Token Bounding and Verification**:
Before any network call is dispatched, the complete request payload is bounded:
1. Hard text character limit: maximum 2000 characters per chunk prompt.
2. Hard byte limit: maximum 8000 UTF-8 bytes per call prompt.
3. Verified envelope framing: includes Converse API message blocks, XML system instructions, role wrappers, and format delimiters, calibrated with direct empirical measurements.
4. Tokenizer test fixtures: automated offline test fixtures run against official model tokenizers to assert that `count_request_tokens` never underestimates actual tokens across adversarial text structures.
5. If calculated tokens exceed 2000 input tokens, extraction fails closed immediately with zero provider calls.

### 4. Bounded Multi Hop Traversal Algorithm

The traversal algorithm executes without full table scans or unbounded recursion:

1. **Seed Entity Resolution**: Match query tokens deterministically against known entity canonical names in the active workspace using exact matching and prefix lookup against `PK = KG#{workspace_id}#LOOKUP` with `SK begins_with NAME#{query_token}` (Limit 5). Zero model calls and zero table scans are performed.
2. **Hop 1 Expansion**:
   * For each seed entity, query outgoing edges: `PK = KG#{workspace_id}#ENTITY#{seed_id}` (Limit 10).
   * Query incoming edges via GSI1: `GSI1_PK = KG#{workspace_id}#TARGET#{seed_id}` (Limit 10).
   * Add destination entities to the frontier and mark visited: `visited_entities.add(seed_id)`.
3. **Hop 2 Expansion** (if `max_hops >= 2` and total visited nodes < 25):
   * For each unvisited node in the frontier (capped at 5 nodes), query outgoing edges with Limit 5.
   * Add to `visited_entities`. If an edge leads to an already visited entity, skip to prevent cycles.
4. **Targeted Chunk Hydration**:
   * Collect distinct `source_chunk_id` values from the traversed edges (capped at 15 chunk IDs).
   * Fetch only these specific chunk records by primary key using `batch_get_item`, never scanning or querying all workspace chunks.
   * Filter chunks against active deletion tombstones in `WorkspaceBoundaryRepository`.
5. **Output Assembly**: Convert hydrated chunks into `EvidenceItem` records with graph provenance metadata detailing seed entities, traversed edge predicates, and hop distance.

### 5. Concurrency Control, Deadlines, and Worker Capacity Retention

Graph lookups run through `GraphConcurrencyController`:
* 4 concurrent storage worker threads.
* 8 maximum queued requests.
* 50ms proposed target deadline across entity matching, graph traversal, and chunk hydration (explicitly marked as proposed until measured under benchmark load).
* Dedicated socket timeouts: 30ms connect, 40ms read. Explicit `botocore` retry disabling (`max_attempts=1`).
* Worker capacity retention: If the 50ms caller deadline expires before storage completes, worker capacity remains reserved until the underlying thread finishes, preventing saturation under latency spikes.

### 6. Candidate Fusion and Reciprocal Rank Fusion

* **Proposed Fusion Weight**: 1.10 base weight (explicitly marked as proposed until calibrated in Slice 2 ablation).
* **Ambiguity and Conflict Adjustment**: If an entity matches multiple candidates or relationship edges contain conflicting statements across documents, the weight is halved to 0.55 (proposed target).
* **Fast Path Bypass**: If `plan.fast_path == True`, graph lookup is skipped entirely (zero tokens, zero latency).
* **Soft Candidate Pooling**: Graph candidates are combined with BM25, vector, PageIndex, and OKF candidates using Reciprocal Rank Fusion ($k=60$). Graph candidates never exclude or prune chunks from other retrieval strategies.

### 7. Lifecycle, Snapshot Integration, and Tombstone Enforcement

* **Snapshots**: Ingestion publishes `DocumentSnapshot` with `graph_status: "ready"`, `graph_entities_count`, and `graph_relations_count`.
* **Tombstone Enforcement**: When a document or workspace is tombstoned, `WorkspaceBoundaryRepository` tombstones are checked inside worker tasks. If tombstoned, traversed edges are discarded immediately.

## Options Considered

### Option 1: Bounded Workspace Scoped DynamoDB and SQLite Graph with Lease Reservations (Recommended)

Store graph relationships in dedicated workspace scoped DynamoDB records (`kre_knowledge_graph`) and SQLite tables. Enforce strict bounded multi hop traversal (max 2 hops, max 25 nodes) via indexed queries. Enforce ingestion budget leases via DynamoDB conditional writes. Pool candidate chunks into reciprocal rank fusion without hard exclusions.

**Pros**:
* Reuses existing DynamoDB and SQLite storage infrastructure without adding new database clusters.
* Guarantees strict multi tenant isolation via workspace prefixed partition keys.
* Adopts the durable budget accounting and fencing pattern proven in OKF Slice 3.1.
* Bounded traversal and targeted chunk hydration prevent memory exhaustion and latency stalls.
* Conforms strictly to ADR-001: graph extraction failures leave baseline search fully functional.

**Cons**:
* Multi hop traversals beyond 3 hops are inefficient in key value stores (accepted, as multi hop retrieval for RAG is proven to degrade past 2 to 3 hops).

### Option 2: Dedicated Graph Database (Neo4j or Amazon Neptune)

Deploy a dedicated graph database cluster to store entities and execute Cypher or Gremlin graph queries.

**Pros**:
* Native graph query languages support arbitrary multi hop traversal and graph algorithms.

**Cons**:
* Adds heavy operational overhead, substantial infrastructure cost, and cold start connection latency.
* Introduces complex cross database consistency and authorization boundaries.
* Violates the project's simplicity guidelines for a brownfield engine refactor.

### Option 3: Unconstrained Language Model Graph RAG

Invoke language models at query time to perform iterative graph exploration and multi step agentic expansion.

**Pros**:
* Flexible query time exploration.

**Cons**:
* Incurs high query latency (several seconds) and high monetary cost per query.
* Violates the engine's hard constraint requiring deterministic zero model calls during retrieval.

## Build Plan and Rollout Slices

The build plan follows the Tracer Bullet approach:

### Slice 1: Contracts, Storage Schema, Extraction, Grounding, and Bounded Traversal
* **Milestone 1.1**: Define shared frozen contracts in `schemas/contracts/knowledge_graph.py` (`KnowledgeGraphEntity`, `KnowledgeGraphRelationship`, `KnowledgeGraphManifest`).
* **Milestone 1.2**: Implement `KnowledgeGraphRepository` supporting DynamoDB and SQLite with indexed partition keys, GSI1, GSI2, and atomic conditional lease reservations.
* **Milestone 1.3**: Implement grounded relation extraction in `backend/src/ingestion/graph_builder.py` with 2 Bedrock call limit, token bounds, and verbatim chunk validation.
* **Milestone 1.4**: Implement bounded multi hop traversal and `GraphConcurrencyController` with 50ms deadline, capacity retention, cycle detection, and targeted chunk hydration.
* **Milestone 1.5**: Write offline regression tests in `backend/tests/test_knowledge_graph_slice1.py` covering contracts, grounding, isolation, atomic lease reservations, and traversal bounds (AC-1, AC-2, AC-3, AC-4, AC-5, AC-6, AC-7, AC-8).

### Slice 2: Pipeline Fusion, Telemetry, and Offline Ablation
* **Milestone 2.1**: Implement `KnowledgeGraphStrategy` and wire `run_graph` in `services/langgraph_pipeline.py`.
* **Milestone 2.2**: Integrate soft candidate pooling in reciprocal rank fusion with calibrated weight (1.10) and fast path bypass.
* **Milestone 2.3**: Update `DocumentSnapshot` metadata and expose graph telemetry in `QueryResponse.retrieval_candidates`.
* **Milestone 2.4**: Implement offline ablation comparison suite in `backend/tests/test_knowledge_graph_ablation_slice2.py` verifying candidate recall, precision, latency, and counterexample discovery safety (AC-9, AC-10, AC-11, AC-12, AC-13).

## Test Strategy

1. **Contract and Identity Tests**: Immutability, extra field rejection, and deterministic ID hashing.
2. **Grounding Validation Tests**: Rejection of hallucinated relationship predicates, ungrounded entity targets, and invalid chunk locators.
3. **Distributed Budget and Lease Tests**: Multi thread and simulated multi process concurrent attempts asserting conditional lease reservation prevents double spending.
4. **Storage and Traversal Tests**: Zero scan verification; queries execute strictly via PK, GSI1, and GSI2; loop and cycle prevention on cyclic graphs; node count and hop ceilings enforced.
5. **Boundary and Tombstone Tests**: Cross tenant access returns 404 or empty results; document tombstones immediately prune edges from worker traversal.
6. **Latency and Admission Tests**: 50ms deadline enforcement, capacity retention on caller timeout, and fallback to baseline without error.
7. **Ablation Comparison Tests**: Verifying candidate recall improvement on relationship queries, with counterexample tests proving incorrect graph matches never displace true answer chunks.

## Operational Risks and Mitigations

1. **Graph Loop and Cycle Stalls**: Highly interconnected entities could cause traversal loops. *Mitigation*: Hard set of visited entity identifiers and maximum 25 node ceiling terminate traversal immediately upon revisiting.
2. **Hub Node Fan Out Explosion**: Entities connected to hundreds of edges (such as generic category terms) could saturate query latency. *Mitigation*: Edges per node are capped at 10 items in the storage query limit.
3. **Stale Cross Document Edges**: Relationships linking entities across documents where one document is updated or deleted. *Mitigation*: Tombstone checks filter edges inside worker tasks; GSI2 enables targeted cleanup upon version deletion.
4. **Candidate Pool Dilution**: Traversed chunks could flood reciprocal rank fusion and push down true lexical matches. *Mitigation*: Bounded candidate contribution (max 10 chunks) and calibrated weight (1.10, halved on ambiguity).

## References

* ADR-001: Baseline Publication Independent of Enrichment (`docs/reference/ADR/ADR-001-baseline-vs-enrichment.md`)
* Architecture Bible §6.3, §7.2: Entity graphs, multi hop traversal, and reciprocal rank fusion
* Specification 0005: Bounded PageIndex Structural Retrieval (`docs/specs/0005-pageindex-structural-retrieval.md`)
* Specification 0006: Bounded Open Knowledge Framework Integration (`docs/specs/0006-okf-integration.md`)
