# 0005. Bounded PageIndex Structural Retrieval Integration

**Date**: 2026-10-24  
**Status**: Proposed  

## Summary

This specification defines the bounded integration of PageIndex structural retrieval as the next verifiable milestone after accepted 0004. PageIndex provides deterministic document layout and page hierarchy navigation (sections, headings, tables, pages) to guide vector and lexical discovery without generative model costs. It replaces unscalable in memory chunk scans with a typed, workspace scoped hierarchy index persisted during document ingestion. Baseline BM25 and vector retrieval remain the operational fallback per ADR-001 so that any PageIndex absence or failure never degrades core queryability.

## Context

The repository has accepted specification 0004 (Core engine walking skeleton and benchmarks) and deferred specification 0003 (Snapshot registry, publication, and atomic deletion). The baseline walking skeleton provides reliable CSV and text PDF ingestion, deterministic structured aggregation without row truncation, hybrid lexical and vector retrieval, bounded reranking, and verified citation evidence.

Architecture Decision Record ADR-001 (Baseline Publication Independent of Enrichment) governs subsequent strategy integration. ADR-001 dictates that baseline retrieval serves as the guaranteed operational fallback, while optional enrichments (PageIndex, Open Knowledge Framework facts, and Knowledge Graph relations) are decoupled and publish asynchronously. Under no circumstances may an incomplete or failing enrichment block baseline queries.

To choose the next integration, existing strategy implementations were audited against their documented designs:

1. **Open Knowledge Framework (OKF)**:
   * Documented intent: Extract and index canonical entity properties and structured facts for rapid property lookups.
   * Implementation audit: Ingestion invokes an unbudgeted model completion call per document batch (`ingestion/okf_builder.py`). Persisted facts are written to global DynamoDB tables (`okf_entities`, `okf_properties`, `okf_relations`) that lack workspace identifiers in their partition keys. In retrieval, `services/retrieval/okf_retriever.py` calls `self.repository.get_okf_properties()`, which does not exist on `db/database.py:CloudRepository` (it is defined on `modules/graph/graph_repository.py`), causing runtime `AttributeError` exceptions that were caught and swallowed by an empty exception block in `services/langgraph_pipeline.py`.
   * Assessment: OKF suffers from severe multi tenant data leakage, broken runtime repository contracts, and unbudgeted model extraction expenses. Integrating OKF requires a comprehensive cross cutting rewrite of the global entity schema to introduce workspace partition keys, version scoping, and tombstoning.

2. **Knowledge Graph (KG)**:
   * Documented intent: Perform bounded multi hop relational traversals to uncover connected evidence across documents.
   * Implementation audit: Knowledge Graph builds directly upon the global, unisolated `okf_relations` table. `services/retrieval/graph_retriever.py` calls `self.repository.expand_graph()`, which also does not exist on `CloudRepository`. Furthermore, `modules/graph/graph_repository.py:get_workspace_graph` runs an unpartitioned scan (`okf_entities_table.scan(Limit=50)`), returning nodes belonging to unrelated tenants. The strategy router (`services/retrieval/strategy_router.py`) explicitly sets `graph_isolation_verified = False`, preventing graph routing because multi tenant isolation is unverified.
   * Assessment: Knowledge Graph inherits all tenant leakage flaws of OKF, possesses broken caller contracts, and cannot be isolated until underlying entity relations are partitioned by workspace and document version.

3. **PageIndex**:
   * Documented intent: Extract hierarchical document layout structure (document, section, page, paragraph, table, figure) during ingestion, allowing queries with structural or page constraints to scope candidate retrieval deterministically.
   * Implementation audit: The scoring heuristic in `ingestion/page_index_service.py` evaluates element weights and section depth decay cleanly. However, in `services/retrieval/strategies/page_index.py`, execution calls `self.repository.get_all_chunks(workspace_id=workspace_id)`, loading all workspace chunks into memory on every query, which is unscalable. In `services/langgraph_pipeline.py:run_page_index`, the node only rescores candidates already returned by BM25 rather than looking up document layout directly. No persistent, typed hierarchy index exists.
   * Assessment: Unlike OKF and Knowledge Graph, PageIndex requires zero model generation calls (zero token budget), operates on verified document layout properties already parsed from PDFs, has no cross tenant entity entanglement, and can be scoped strictly to workspace and document version.

PageIndex is therefore the single sound candidate for Slice 2. It introduces structural navigation while preserving the operational baseline and strict isolation.

## Requirements

**User stories**:
- As a retrieval engineer, I want queries with structural constraints (such as table numbers, figure titles, or section references) to navigate document layout directly so that candidate retrieval is focused without scanning every chunk in the workspace.
- As a system operator, I want PageIndex to be fully decoupled from baseline retrieval so that any missing index, corrupt hierarchy, or timeout falls back immediately to BM25 and vector search without failing user queries.
- As a tenant administrator, I want PageIndex structural indexes to be partitioned strictly by workspace and document version so that document layout metadata never leaks across workspace boundaries or surfaces tombstoned content.

**Acceptance criteria**:
- **AC-1**: Ingestion persists a typed `PageIndexManifest` per document version into durable storage, capturing document section hierarchy, heading paths, page ranges, and element categories without row or page truncation.
- **AC-2**: PageIndex construction and query execution are completely deterministic, incurring exactly zero model generation calls, zero embedding calls, and zero remote provider invocations.
- **AC-3**: PageIndex storage and lookups are strictly partitioned by `workspace_id`, `document_id`, and `document_version`. Cross workspace queries return empty results or HTTP 404, with zero leakage across tenant boundaries.
- **AC-4**: Active source tombstones in `WorkspaceBoundaryRepository` immediately block PageIndex retrieval for that source or workspace, returning zero structural candidates without throwing storage errors.
- **AC-5**: Retrieval queries route through PageIndex only when structural intent is detected (such as references to sections, tables, figures, exhibits, or page ranges). General semantic queries continue using the default baseline discovery path.
- **AC-6**: If PageIndex lookup returns empty candidates, takes longer than the configured timeout (50ms budget), or encounters storage unavailability, the pipeline falls back gracefully to baseline BM25 and vector retrieval. An enrichment error never causes query failure or error responses to the caller.
- **AC-7**: Structural evidence items emitted by PageIndex include exact provenance: `document_id`, `document_version`, `page_number`, `section_path`, and `element_type`, matching the canonical evidence contract.
- **AC-8**: An offline ablation test suite compares candidate page recall, precision, and latency with PageIndex enabled versus disabled against the repaired BM25 and vector baseline across canonical evaluation documents.
- **AC-9**: Formal benchmark accuracy claims remain explicitly blocked until post repair canonical 60 question benchmark execution is run under controlled comparison conditions.

## Options considered

### Option 1: Bounded PageIndex Structural Layout Index (Recommended)

Build an immutable structural hierarchy index (`PageIndexManifest`) during document ingestion that maps section headings, page numbers, and structural elements (tables, figures, notes) for each PDF document version. In retrieval, structural queries lookup target pages directly from the manifest, passing candidate page scopes to vector and lexical search without loading all workspace chunks into memory.

**Pros**:
- Completely deterministic with zero model generation calls and zero token cost.
- Inherits existing `DocumentSnapshot` provenance (`workspace_id`, `document_id`, `document_version`).
- Resolves the unscalable full chunk scan defect in `services/retrieval/strategies/page_index.py`.
- Adheres strictly to ADR-001 by acting as an optional enrichment with guaranteed baseline fallback.

**Cons**:
- Only benefits documents with hierarchical layout (primarily structured PDFs, not raw flat CSV files).
- Requires a compact storage schema in DynamoDB or SQLite to hold page hierarchy metadata.

### Option 2: OKF Entity Property Integration

Repair the broken repository contracts in `okf_retriever.py`, add `workspace_id` to `okf_entities` and `okf_properties` DynamoDB tables, and wire OKF property lookups into query execution.

**Pros**:
- Accelerates exact attribute lookups for known named entities.

**Cons**:
- Ingestion extraction relies on expensive model completion calls, violating bounded cost principles.
- Requires major data migration to rebuild global DynamoDB tables with composite workspace partition keys.
- Does not solve structural document navigation.

### Option 3: Knowledge Graph Relational Traversal

Repair graph repository methods, introduce workspace scoped graph edge storage, and enable multi hop graph traversal in `services/langgraph_pipeline.py`.

**Pros**:
- Enables multi hop reasoning across cross document entity relationships.

**Cons**:
- Highest architectural complexity and risk of multi tenant leakage.
- Depends on OKF entity extraction, which is currently unisolated and unbudgeted.
- Premature before single document structural hierarchy (PageIndex) is verified.

## Decision

**Chosen option**: Option 1: Bounded PageIndex Structural Layout Index.

We will build a bounded, deterministic PageIndex vertical slice that persists document hierarchy during ingestion and provides structural candidate scoping during query routing, with complete isolation and guaranteed baseline fallback.

## Rationale

PageIndex is the natural and sound progression for Slice 2. Unlike OKF and Knowledge Graph, which have broken runtime caller contracts and global DynamoDB tables lacking workspace partition keys, PageIndex is purely structural. It requires zero remote model calls, adding zero cost to document ingestion and query execution.

By persisting a compact `PageIndexManifest` per document version, we eliminate the existing defect where `services/retrieval/strategies/page_index.py` executes a full table scan (`get_all_chunks(workspace_id)`). It also upgrades `services/langgraph_pipeline.py:run_page_index` from a passive filter on BM25 outputs into an independent structural navigator that inspects the document hierarchy directly.

If PageIndex is missing, corrupted, or times out, the system falls back to baseline BM25 and vector retrieval seamlessly, fulfilling ADR-001.

## Feature design

**Data model sketch**:

```
+-----------------------------------------------------------------------------------+
| PageIndexManifest Record (DynamoDB / SQLite)                                      |
+-----------------------------------------------------------------------------------+
| PK: WORKSPACE#{workspace_id}                                                      |
| SK: PAGEINDEX#{document_id}#{document_version}                                    |
| Attributes:                                                                       |
|   - workspace_id: string                                                          |
|   - document_id: string                                                           |
|   - document_version: string                                                      |
|   - total_pages: integer                                                          |
|   - root_sections: list[SectionNode]                                              |
|   - element_index: map[string, list[ElementLocation]]                             |
|       e.g. "table 8" -> [{"page": 42, "element_id": "el_109", "title": "..."}]   |
|            "figure 2" -> [{"page": 15, "element_id": "el_45", "title": "..."}]   |
|   - created_at: string (ISO 8601)                                                 |
|   - status: "ready" | "failed"                                                    |
+-----------------------------------------------------------------------------------+
```

Each `SectionNode` contains:
* `title`: Section or heading text (string)
* `level`: Heading level (1 to 6)
* `page_start`: First page number (integer)
* `page_end`: Last page number (integer)
* `subsections`: list of nested `SectionNode` items

**State transitions**:
`Document Ingestion` -> `Parse Text & Elements` -> `Extract PageIndexManifest` -> `Persist Manifest (status: ready)`
If parsing fails -> `Persist Manifest (status: failed)` -> Baseline ingestion succeeds without blocking document availability.

**API surface**:
PageIndex operates internally within ingestion and retrieval services, with an administrative read endpoint:

| Endpoint | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `/api/v1/workspaces/{ws_id}/documents/{doc_id}/page-index` | GET | `version: string (optional)` | `PageIndexManifest` payload | bearer token | 404 not found, 403 denied |

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| Ingest PDF | `PageIndexManifest` | Extracted from PDF outline, bookmarks, and heading element chunks |
| Ingest PDF | `element_index` | Built by matching regex patterns (`table \d+`, `figure \d+`, `exhibit \w+`) against chunk texts |
| Route Query | `candidate_page_scopes` | Resolved by looking up query structural entities in `PageIndexManifest` |
| Route Query | `page_index_latency_ms` | Measured wall clock time of PageIndex lookup (budget <= 50ms) |
| Route Query | `page_index_confidence` | Normalized match score based on heading depth and exact entity matches |

**Key invariants**:
- PageIndex lookup never invokes any remote language model or embedding provider (0 tokens, $0 cost).
- PageIndex queries must complete within 50ms; any timeout triggers immediate silent fallback to baseline retrieval.
- When `WorkspaceBoundaryRepository.has_tombstone(workspace_id, "source", document_id)` returns true, PageIndex lookups for that document return empty immediately.
- PageIndex storage items always include `workspace_id` in their partition key. Cross workspace access is blocked at the repository boundary.
- An absent or failed PageIndex manifest never causes document ingestion to fail or query execution to return an error (ADR-001).

**Security model**:
- Ingestion and query operations validate workspace authorization through existing identity checks.
- Document and workspace deletion tombstones take immediate precedence over cached or persisted PageIndex manifests.

**Configuration required**:
- `PAGEINDEX_ENABLED`: boolean (default true)
- `PAGEINDEX_TIMEOUT_MS`: integer (default 50)
- `PAGEINDEX_SCORE_THRESHOLD`: float (default 1.5)

**Critical test scenarios**:
- Happy path: Query with structural entity (such as "Table 8" or "Section 3.2") matches `PageIndexManifest`, restricting vector candidate page scopes to target pages, verifying **AC-1**, **AC-5**, **AC-7**.
- Deterministic budget: Verify that PageIndex construction and query resolution make 0 Bedrock completion calls and 0 embedding calls, verifying **AC-2**.
- Multi tenant boundary isolation: Attempt to lookup PageIndex manifest using another workspace identifier; verify 404 or empty response with 0 cross tenant leakage, verifying **AC-3**.
- Tombstone enforcement: Insert a deletion tombstone for a document; verify PageIndex lookups immediately return empty without error, verifying **AC-4**.
- Graceful degradation: Simulate missing manifest, corrupt JSON, or storage timeout; verify query execution falls back cleanly to baseline BM25 and vector retrieval without user visible errors, verifying **AC-6**.
- Offline ablation comparison: Run ablation harness across canonical test corpus comparing candidate page recall, precision, and latency with PageIndex active versus inactive, verifying **AC-8**.
- Benchmark gate: Confirm that canonical benchmark accuracy delta claims remain blocked until formal benchmark run executes on a dedicated comparison workspace, verifying **AC-9**.

## Build plan

Following the Tracer Bullet build approach, this slice is implemented in thin, vertically verified steps:

1. **Hierarchy Schema and Store**: Define `PageIndexManifest`, `SectionNode`, and storage repository adapter partitioned by `workspace_id` and document version, satisfying **AC-1**, **AC-3**.
2. **Ingestion Extraction Pipeline**: Add deterministic layout hierarchy extraction to PDF adapter during ingestion, generating and persisting `PageIndexManifest` without remote model calls, satisfying **AC-1**, **AC-2**.
3. **Boundary and Tombstone Enforcement**: Implement workspace scoping and tombstone filtering in `PageIndexRepository`, ensuring deleted documents return zero items, satisfying **AC-3**, **AC-4**.
4. **Retrieval Integration and Fallback Router**: Wire PageIndex lookup into `services/retrieval/page_index_retriever.py` and `services/langgraph_pipeline.py:run_page_index` to lookup target page scopes directly with hard 50ms timeout and baseline fallback, satisfying **AC-5**, **AC-6**, **AC-7**.
5. **Offline Ablation Harness**: Build an offline ablation regression suite comparing candidate page recall, precision, and latency with PageIndex enabled versus disabled against the repaired BM25 and vector baseline, satisfying **AC-8**.
6. **Benchmark Comparison Preservation**: Enforce comparator rules ensuring no benchmark accuracy claims are made until controlled canonical comparison run executes, satisfying **AC-9**.

## Consequences

**Positive**:
- Eliminates the unscalable `get_all_chunks(workspace_id)` memory scan in `strategies/page_index.py`.
- Provides targeted page navigation for documents containing tables, exhibits, and numbered sections.
- Completely deterministic with zero model generation expenses.
- Strictly honors ADR-001 by preserving baseline retrieval as the operational fallback.

**Negative / tradeoffs**:
- Adds one new storage item (`PageIndexManifest`) per ingested PDF document version.
- Only benefits documents with explicit headings, outlines, or structural entities.

**Neutral**:
- CSV files and unstructured short text notes skip PageIndex extraction, relying on structured table and vector stores.

## Follow-up

- [ ] Execute post repair canonical 60 question benchmark on `ws_canonical_comparison_0004` to establish the new controlled comparison baseline.
- [ ] Following Slice 2 acceptance, address OKF multi tenant schema redesign (Slice 3) by introducing composite workspace partition keys before graph integration.

## References

- ADR-001: Baseline Publication Independent of Enrichment (`docs/reference/ADR/ADR-001-baseline-vs-enrichment.md`)
- ADR-002: Structured Execution Outside RRF (`docs/reference/ADR/ADR-002-structured-outside-rrf.md`)
- ADR-003: Knowledge Map as Routing Layer (`docs/reference/ADR/ADR-003-knowledge-map.md`)
- Spec 0004: Core engine walking skeleton and benchmarks (`docs/specs/0004-core-engine-walking-skeleton.md`)
- Spec 0003: Snapshot registry, publication, and atomic deletion (`docs/specs/0003-snapshot-registry-publication-atomic-deletion.md`, deferred)
- Scope Definition: Slice 2 PageIndex Integration (`docs/scope/scope.md`)
