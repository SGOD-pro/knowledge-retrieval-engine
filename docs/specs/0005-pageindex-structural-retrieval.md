# 0005. Bounded PageIndex Structural Retrieval Integration

**Date**: 2026-10-24  
**Status**: Proposed  

## Summary

This specification defines the bounded integration of PageIndex structural retrieval as the next verifiable milestone after accepted 0004. For this bounded slice, PageIndex provides deterministic document layout and page hierarchy navigation (sections, headings, tables, pages) to guide candidate retrieval without generative model costs. It replaces unscalable in memory chunk scans with a typed, workspace scoped hierarchy index persisted during document ingestion. Baseline BM25 and vector discovery remain fully independent and pooled with structural candidates so that a wrong structural match never excludes valid baseline evidence. Baseline retrieval remains the operational fallback per ADR-001 so that any PageIndex absence, storage overflow, or lookup timeout falls back immediately without degrading core queryability.

## Context

The repository has accepted specification 0004 (Core engine walking skeleton and benchmarks) and deferred specification 0003 (Snapshot registry, publication, and atomic deletion). The baseline walking skeleton provides reliable CSV and text PDF ingestion, deterministic structured aggregation without row truncation, hybrid lexical and vector retrieval, bounded reranking, and verified citation evidence.

Architecture Decision Record ADR-001 (Baseline Publication Independent of Enrichment) governs subsequent strategy integration. ADR-001 dictates that baseline retrieval serves as the guaranteed operational fallback, while optional enrichments (PageIndex, Open Knowledge Framework facts, and Knowledge Graph relations) are decoupled and publish asynchronously. Under no circumstances may an incomplete or failing enrichment block baseline queries.

To choose the next integration, existing strategy implementations were audited against their documented designs:

1. **Open Knowledge Framework (OKF)**:
   * Documented intent: Extract and index canonical entity properties and structured facts for rapid property lookups.
   * Implementation audit: Ingestion invokes an unbudgeted model completion call per document batch ([`backend/src/ingestion/okf_builder.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py)). Persisted facts are written to global DynamoDB tables (`okf_entities`, `okf_properties`, `okf_relations`) that lack workspace identifiers in their partition keys. In retrieval, [`backend/src/services/retrieval/okf_retriever.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/okf_retriever.py#L23) calls `self.repository.get_okf_properties()`, which does not exist on [`backend/src/db/database.py:CloudRepository`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/database.py) (it is defined on [`backend/src/modules/graph/graph_repository.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/modules/graph/graph_repository.py#L90)), causing runtime `AttributeError` exceptions that were caught and swallowed by an empty exception block in [`backend/src/services/langgraph_pipeline.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L182).
   * Assessment: OKF suffers from severe multi tenant data leakage, broken runtime repository contracts, and unbudgeted model extraction expenses. Integrating OKF requires a comprehensive cross cutting rewrite of the global entity schema to introduce workspace partition keys, version scoping, and tombstoning.

2. **Knowledge Graph (KG)**:
   * Documented intent: Perform bounded multi hop relational traversals to uncover connected evidence across documents.
   * Implementation audit: Knowledge Graph builds directly upon the global, unisolated `okf_relations` table. [`backend/src/services/retrieval/graph_retriever.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/graph_retriever.py#L25) calls `self.repository.expand_graph()`, which also does not exist on `CloudRepository`. Furthermore, [`backend/src/modules/graph/graph_repository.py:get_workspace_graph`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/modules/graph/graph_repository.py#L27) runs an unpartitioned scan (`okf_entities_table.scan(Limit=50)`), returning nodes belonging to unrelated tenants. The strategy router ([`backend/src/services/retrieval/strategy_router.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/strategy_router.py#L126)) explicitly sets `graph_isolation_verified = False`, preventing graph routing because multi tenant isolation is unverified.
   * Assessment: Knowledge Graph inherits all tenant leakage flaws of OKF, possesses broken caller contracts, and cannot be isolated until underlying entity relations are partitioned by workspace and document version.

3. **PageIndex**:
   * Documented intent: Extract hierarchical document layout structure (document, section, page, paragraph, table, figure) during ingestion, allowing queries with structural or page constraints to scope candidate retrieval deterministically.
   * Implementation audit: The scoring heuristic in [`backend/src/ingestion/page_index_service.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/page_index_service.py) evaluates element weights and section depth decay cleanly. However, in [`backend/src/services/retrieval/strategies/page_index.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/strategies/page_index.py#L53), execution calls `self.repository.get_all_chunks(workspace_id=workspace_id)`, loading all workspace chunks into memory on every query, which is unscalable. In [`backend/src/services/langgraph_pipeline.py:run_page_index`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/langgraph_pipeline.py#L263), the node only rescores candidates already returned by BM25 rather than looking up document layout directly. No persistent, typed hierarchy index exists.
   * Assessment: Unlike OKF and Knowledge Graph, PageIndex in this bounded slice operates deterministically with zero model generation calls (zero token budget), operates on verified document layout properties already parsed from PDFs, has no cross tenant entity entanglement, and can be scoped strictly to workspace and document version.

PageIndex is therefore the single sound candidate for Slice 2. It introduces structural navigation while preserving the operational baseline and strict isolation.

## Requirements

**User stories**:
- As a retrieval engineer, I want queries with structural constraints (such as table numbers, figure titles, or section references) to lookup document layout directly so that candidate retrieval is enriched without scanning every chunk in the workspace.
- As a retrieval engineer, I want PageIndex candidates to merge into the discovery pool alongside independent BM25 and vector results so that a non matching or wrong structural candidate never excludes valid factual evidence found by baseline retrieval.
- As a system operator, I want PageIndex to be fully decoupled from baseline retrieval with enforceable storage deadlines so that any missing index, corrupt hierarchy, overflow, or slow storage call falls back immediately to BM25 and vector search without delaying queries.
- As a tenant administrator, I want PageIndex structural indexes to be partitioned strictly by workspace and document version so that document layout metadata never leaks across workspace boundaries or surfaces tombstoned content.

**Acceptance criteria**:
- **AC-1**: Ingestion extracts and persists a typed `PageIndexManifest` per document version, capturing document section hierarchy, heading paths, page ranges, and verified structural definition targets without row or page truncation.
- **AC-2**: For this bounded slice, PageIndex construction and query execution are completely deterministic, incurring exactly zero language model completion calls, zero embedding calls, and zero remote provider invocations. Future slices may explore generative layout extensions if justified, but this slice remains deterministic.
- **AC-3**: PageIndex candidate merging preserves independent BM25 and vector discovery. Structural candidates are pooled and soft boosted in reciprocal rank fusion without hard excluding candidate pages or pruning baseline chunks. If PageIndex matches a wrong structural target, valid baseline candidates from other pages remain present in the candidate pool.
- **AC-4**: PageIndex storage handles DynamoDB item size limits safely. Manifests up to 250KB store inline in `PageIndexSummary`; manifests exceeding 250KB store section summaries in DynamoDB and offload full structural element topology to S3 using the established S3 first write sequence from 0004 AC-10. Version readiness is tracked via `page_index_status` (`ready` or `failed`) on `DocumentSnapshot` without implementing deferred 0003 publication transactions or distributed garbage collection.
- **AC-5**: PageIndex lookups enforce hard client deadlines using dedicated socket configuration (connect timeout 30ms, read timeout 40ms, 0 retries) and a 50ms wall clock deadline. A bounded thread pool (maximum 4 workers) prevents queued task accumulation under load. Expired or failing lookups drop structural candidates cleanly and log a structured warning.
- **AC-6**: Structural target parsing distinguishes definition locations from textual cross references. Outlines, headings, and formal caption starts (`Table N`, `Figure N`, `Exhibit N`) are indexed as definitions. Mentions inside paragraph body text or table cells are classified as cross references and excluded from definition targets. Ambiguous targets (such as duplicate table numbers across appendices) and documents lacking structural headings are recorded as explicit states (`ambiguous` and `missing_layout`).
- **AC-7**: PageIndex storage and lookups are strictly partitioned by `workspace_id`, `document_id`, and `document_version`. Cross workspace queries return empty results with zero tenant leakage. Active source tombstones in `WorkspaceBoundaryRepository` immediately block PageIndex retrieval for that source or workspace, returning zero structural candidates without throwing storage errors.
- **AC-8**: Dedicated administrative HTTP endpoints are deferred from this slice to keep the attack surface minimal and prevent contract drift. PageIndex readiness, latency, and hit statistics are exposed exclusively through existing `DocumentSnapshot` metadata, `retrieval_candidates` trace, and request telemetry.
- **AC-9**: An offline ablation test suite compares candidate page recall, precision, and latency with PageIndex enabled versus disabled against the repaired BM25 and vector baseline across canonical evaluation documents, including a test proving that a wrong structural candidate does not prevent retrieval of the correct answer from an unindexed page.
- **AC-10**: Formal benchmark accuracy claims remain explicitly blocked until post repair canonical 60 question benchmark execution is run under controlled comparison conditions on `ws_canonical_comparison_0004`.

## Options considered

### Option 1: Bounded Soft Pooled PageIndex with Storage Safeguards (Recommended)

Build an immutable structural hierarchy index (`PageIndexManifest`) during document ingestion that maps section headings, page numbers, and verified definition targets for each PDF document version. In retrieval, PageIndex candidates merge into reciprocal rank fusion alongside independent BM25 and vector discovery candidates without hard page gating. Large hierarchy payloads exceeding DynamoDB limits offload to S3 using 0004 storage patterns, while client timeouts strictly cap lookup latency at 50ms.

**Pros**:
- Completely deterministic with zero model generation calls and zero token cost.
- Preserves independent baseline discovery; a wrong structural candidate never excludes the correct page.
- Safely handles DynamoDB 400KB item limits through summary partitioning and S3 overflow storage.
- Enforces strict socket timeouts and bounded worker pools, preventing latency spikes.
- Adheres strictly to ADR-001 by acting as an optional enrichment with guaranteed baseline fallback.

**Cons**:
- Soft pooling requires tuning reciprocal rank fusion weight for structural candidates.
- S3 overflow requires storage client coordination for large documents.

### Option 2: Hard Page Filtering PageIndex

Extract layout hierarchy and use PageIndex results to strictly restrict vector search to only the candidate pages identified by the structural match.

**Pros**:
- Narrows vector candidate pools aggressively.

**Cons**:
- Critical failure mode: If the query mentions a table that PageIndex misidentifies (or references an introductory table), hard page gating blinds vector search and guarantees query failure on the true answer page.
- Violates ADR-001 principles of non blocking baseline discovery.

### Option 3: OKF and Knowledge Graph Integration

Repair broken repository caller contracts in `okf_retriever.py` and `graph_retriever.py`, and attempt to partition global entity tables by workspace.

**Pros**:
- Addresses multi hop entity questions.

**Cons**:
- Requires extensive data model migration to fix global unpartitioned DynamoDB tables.
- Ingestion extraction incurs high language model completion costs.
- High risk of multi tenant security leakage before single document layout navigation is sound.

## Decision

**Chosen option**: Option 1: Bounded Soft Pooled PageIndex with Storage Safeguards.

We will build a bounded, deterministic PageIndex vertical slice that persists document hierarchy during ingestion and pools structural candidates into reciprocal rank fusion alongside independent BM25 and vector discovery, with strict storage overflow protection, enforceable client timeouts, and guaranteed baseline fallback.

## Rationale

PageIndex is the sound progression for Slice 2. Unlike OKF and Knowledge Graph, which have broken runtime caller contracts and global DynamoDB tables lacking workspace partition keys, PageIndex is structural. For this slice, it requires zero remote model calls, adding zero cost to document ingestion and query execution.

Crucially, PageIndex candidates must soft pool into rank fusion rather than hard gate vector search. If a user query mentions a table that happens to match an introductory table on page 3 while the required fact resides on page 14, hard page filtering would guarantee a retrieval failure. Soft pooling preserves page 14 candidates from BM25 and vector discovery while granting a calibrated rank boost to structural candidates.

Durable storage is bounded against DynamoDB 400KB limits by storing summary headings inline and offloading detailed element mappings above 250KB to S3 using the established 0004 storage pattern. Enforceable socket timeouts (30ms connect, 40ms read) and a 50ms deadline prevent storage hiccups from accumulating latency or blocking caller threads.

Administrative HTTP endpoints are deferred from this slice to keep implementation surgical, and post repair canonical evaluation remains explicitly pending before claiming any accuracy improvement.

## Feature design

**Data model sketch**:

```
+-----------------------------------------------------------------------------------+
| PageIndexSummary Record (DynamoDB / SQLite)                                       |
+-----------------------------------------------------------------------------------+
| PK: WORKSPACE#{workspace_id}                                                      |
| SK: PAGEINDEX_SUMMARY#{document_id}#{document_version}                            |
| Attributes:                                                                       |
|   - workspace_id: string                                                          |
|   - document_id: string                                                           |
|   - document_version: string                                                      |
|   - total_pages: integer                                                          |
|   - layout_status: "ready" | "missing_layout" | "failed"                          |
|   - root_sections: list[SectionSummary]                                           |
|   - has_s3_overflow: boolean                                                      |
|   - s3_overflow_key: string | null                                                |
|   - inline_definitions: map[string, list[ElementLocation]] (if payload <= 250KB)  |
|   - created_at: string (ISO 8601)                                                 |
+-----------------------------------------------------------------------------------+

+-----------------------------------------------------------------------------------+
| PageIndexDetail Artifact (S3, used only when payload > 250KB)                     |
+-----------------------------------------------------------------------------------+
| S3 Key: pageindex/{workspace_id}/{document_id}/{document_version}/hierarchy.json   |
| Payload:                                                                          |
|   - sections: full nested SectionNode tree                                        |
|   - definitions: complete map of verified target elements (tables, figures, notes)|
|   - cross_references: map of textual cross references                             |
+-----------------------------------------------------------------------------------+
```

Each `SectionSummary` contains:
* `title`: Section or heading text (string)
* `level`: Heading level (1 to 6)
* `page_start`: First page number (integer)
* `page_end`: Last page number (integer)

Each `ElementLocation` contains:
* `element_id`: Chunk identifier (string)
* `page_number`: Page number (integer)
* `element_type`: `"table"` | `"figure"` | `"heading"` | `"caption"`
* `label`: Normalized label (e.g. `"table 8"`, `"figure 2"`)
* `title`: Caption or heading title text (string)

**Target Discrimination Rules**:
* **Definition Target**: An element is indexed as a structural definition target if and only if `element_type in ("table", "figure")` OR its chunk begins with a formal caption pattern: `^(Table|Figure|Exhibit|Box|Chart)\s+([A-Za-z0-9.-]+)[:.]?` with `element_type == "caption"`.
* **Cross Reference**: Mentions of tables or figures inside running paragraphs or table cells (e.g. "...as shown in Table 8...") are tagged as cross references and omitted from the structural definition index.
* **Ambiguity Handling**: When multiple distinct definition targets share the same label (e.g. two separate tables titled "Table 1" in different appendices), PageIndex marks the query target state as `ambiguous` and includes all matching candidate pages with divided weight rather than arbitrarily picking one.
* **Missing Layout Handling**: If a PDF contains no outline bookmarks, table elements, or recognized heading chunks, PageIndex records `layout_status: "missing_layout"` and returns empty candidates immediately without throwing an error.

**Candidate Merging and Discovery Preservation**:
1. PageIndex evaluates the query for structural target references (such as table, figure, or section patterns).
2. If structural definitions match, PageIndex retrieves chunks matching the target page numbers and assigns each a calibrated structural score.
3. Structural candidates are added to the reciprocal rank fusion candidate pool alongside baseline BM25 candidates and vector candidates:
   ```python
   # Reciprocal Rank Fusion pooling (soft boost, zero hard exclusions)
   merged = _rrf_merge(
       bm25_candidates=bm25_chunks,
       vector_candidates=vector_chunks,
       structural_candidates=page_index_chunks,
       structural_weight=1.1,
       k=60,
   )
   ```
4. Baseline vector search continues to search across the full active workspace document scope. Baseline candidates are never pruned or gated based on PageIndex outputs.

**Timeout and Concurrency Controls**:
* **Storage Client Settings**: DynamoDB client uses `botocore.config.Config(connect_timeout=0.030, read_timeout=0.040, retries={'max_attempts': 1})`.
* **Deadline Enforcement**: The lookup wraps in an enforceable 50ms wall clock deadline:
  `asyncio.wait_for(page_index_lookup(), timeout=0.050)`.
* **Worker Bounds**: A dedicated `ThreadPoolExecutor(max_workers=4, thread_name_prefix="page_index")` handles storage reads. If the pool saturates or timeout expires, the lookup drops out cleanly, logs `page_index.timeout_ms=50.0 fallback=baseline`, and baseline discovery continues without delay.

**API surface**:
Administrative HTTP endpoints are deferred from this slice. PageIndex status and statistics are exposed exclusively through existing internal mechanisms:
* `DocumentSnapshot`: records `page_index_status: "ready" | "missing_layout" | "failed"` and `page_index_elements_count`.
* Query Response Debug Trace: `retrieval_candidates` records `structural_candidate_ids`, `structural_match_count`, and `page_index_latency_ms`.

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| Ingest PDF | `PageIndexSummary` | Extracted from PDF outline, bookmarks, and heading element chunks |
| Ingest PDF | `inline_definitions` | Built from chunks matching definition patterns when payload <= 250KB |
| Ingest PDF | `s3_overflow_key` | S3 path generated when hierarchy payload exceeds 250KB threshold |
| Route Query | `structural_candidates` | Resolved by looking up query structural targets in definition index |
| Route Query | `page_index_latency_ms` | Measured wall clock duration of PageIndex lookup (hard capped at 50ms) |
| Route Query | `structural_target_state` | Computed status: `"unique"` | `"ambiguous"` | `"missing_layout"` | `"none"` |

**Key invariants**:
- For this bounded slice, PageIndex lookup never invokes any remote language model or embedding provider (0 tokens, $0 cost).
- PageIndex candidates are always soft pooled; a non empty structural candidate set never excludes or prunes baseline BM25 and vector candidates.
- PageIndex lookups enforce hard client deadlines (30ms connect, 40ms read, 50ms total wall clock); expired calls fall back to baseline retrieval immediately.
- When `WorkspaceBoundaryRepository.has_tombstone(workspace_id, "source", document_id)` returns true, PageIndex lookups for that document return empty immediately.
- PageIndex storage items always include `workspace_id` in their partition key. Cross workspace access is blocked at the repository boundary.
- An absent, corrupt, or failed PageIndex manifest never causes document ingestion to fail or query execution to return an error (ADR-001).

**Security model**:
- Ingestion and query operations validate workspace authorization through existing identity checks.
- Document and workspace deletion tombstones take immediate precedence over cached or persisted PageIndex manifests.

**Configuration required**:
- `PAGEINDEX_ENABLED`: boolean (default true)
- `PAGEINDEX_TIMEOUT_MS`: integer (default 50)
- `PAGEINDEX_OVERFLOW_THRESHOLD_BYTES`: integer (default 250000)
- `PAGEINDEX_STRUCTURAL_WEIGHT`: float (default 1.1)

**Critical test scenarios**:
- Soft pooling discovery preservation: Query with structural entity "Table 1" matches a definition on page 3, while the factual answer resides in paragraph text on page 14. Verify that page 14 candidates from vector search are preserved in the merged pool and that the final answer correctly cites page 14, verifying **AC-3**.
- Deterministic budget: Verify that PageIndex construction and query resolution make 0 Bedrock completion calls and 0 embedding calls, verifying **AC-2**.
- Definition versus cross reference discrimination: Ingest a document containing Table 8 on page 42 and paragraphs on page 10 and 22 mentioning Table 8. Verify that only page 42 is indexed as the definition target, while pages 10 and 22 are classified as cross references, verifying **AC-6**.
- Ambiguity and missing layout handling: Query for "Table 1" in a document containing two distinct tables titled "Table 1"; verify `structural_target_state == "ambiguous"` and both pages are pooled. Query a flat unformatted PDF; verify `layout_status == "missing_layout"` and baseline discovery runs without error, verifying **AC-6**.
- Storage overflow handling: Ingest a large synthetic PDF producing a hierarchy payload exceeding 250KB; verify that summary metadata is persisted to DynamoDB and full hierarchy is stored in S3, verifying **AC-4**.
- Client deadline enforcement: Inject a 100ms storage delay into PageIndex lookup; verify that the 50ms deadline fires, logs a warning, drops structural candidates, and allows baseline BM25 and vector retrieval to complete within normal latency budgets, verifying **AC-5**.
- Multi tenant boundary isolation: Attempt to lookup PageIndex manifest using another workspace identifier; verify empty response with 0 cross tenant leakage, verifying **AC-7**.
- Tombstone enforcement: Insert a deletion tombstone for a document; verify PageIndex lookups immediately return empty without error, verifying **AC-7**.
- Offline ablation comparison: Run ablation harness across canonical test corpus comparing candidate page recall, precision, and latency with PageIndex enabled versus disabled against the repaired BM25 and vector baseline, verifying **AC-9**.
- Benchmark gate: Confirm that canonical benchmark accuracy delta claims remain blocked until formal benchmark run executes on a dedicated comparison workspace, verifying **AC-10**.

## Build plan

Following the Tracer Bullet build approach, this slice is implemented in thin, vertically verified steps:

1. **Hierarchy Schema and Storage with Overflow**: Define `PageIndexSummary`, `SectionSummary`, `ElementLocation`, and repository adapter storing inline definitions under 250KB and offloading larger payloads to S3, satisfying **AC-1**, **AC-4**, **AC-7**.
2. **Deterministic Layout Extraction and Target Discrimination**: Add layout hierarchy extraction to PDF adapter, distinguishing definition targets from body text cross references, and recording `ambiguous` or `missing_layout` states, satisfying **AC-1**, **AC-2**, **AC-6**.
3. **Boundary and Tombstone Enforcement**: Implement workspace partition key validation and tombstone filtering in `PageIndexRepository`, ensuring deleted documents return zero items, satisfying **AC-7**.
4. **Deadline Enforced Lookup and Soft Fusion**: Implement `PageIndexRetriever` with explicit socket timeouts (30ms/40ms) and 50ms deadline, merging structural candidates into reciprocal rank fusion (`_rrf_merge`) alongside independent baseline discovery without hard page exclusion, satisfying **AC-3**, **AC-5**.
5. **Offline Ablation and Counterexample Regressions**: Build an offline ablation regression suite comparing candidate page recall, precision, and latency with PageIndex enabled versus disabled against the repaired baseline, including the counterexample regression where a wrong structural candidate does not prevent retrieval of the correct answer from an unindexed page, satisfying **AC-3**, **AC-9**.
6. **Benchmark Comparison Preservation**: Enforce comparator rules ensuring no benchmark accuracy claims are made until controlled canonical comparison run executes on `ws_canonical_comparison_0004`, satisfying **AC-10**.

## Consequences

**Positive**:
- Eliminates the unscalable `get_all_chunks(workspace_id)` memory scan in `strategies/page_index.py`.
- Preserves independent baseline discovery; a wrong structural candidate never excludes valid factual evidence from other pages.
- Enforces strict socket timeouts (30ms/40ms) and a 50ms deadline, protecting query latency.
- Protects DynamoDB from item size overflow by offloading large hierarchies (>250KB) to S3.
- Avoids implementing deferred 0003 publication machinery by using existing `DocumentSnapshot` version readiness.
- Strictly honors ADR-001 by preserving baseline retrieval as the operational fallback.

**Negative / tradeoffs**:
- Adds one summary item per ingested PDF document version (and an optional S3 artifact for large documents).
- Soft pooling requires tuning the relative rank fusion weight of structural candidates against lexical and vector ranks.

**Neutral**:
- CSV files and unstructured short text notes skip PageIndex extraction, relying on structured table and vector stores.
- Future slices remain free to evaluate generative layout or tree summarization extensions without being constrained by this slice's deterministic scope.

## Follow Up

- [ ] Execute post repair canonical 60 question benchmark on `ws_canonical_comparison_0004` to establish the new controlled comparison baseline.
- [ ] Following Slice 2 acceptance, address OKF multi tenant schema redesign (Slice 3) by introducing composite workspace partition keys before graph integration.

## References

- ADR-001: Baseline Publication Independent of Enrichment ([`docs/reference/ADR/ADR-001-baseline-vs-enrichment.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reference/ADR/ADR-001-baseline-vs-enrichment.md))
- ADR-002: Structured Execution Outside RRF ([`docs/reference/ADR/ADR-002-structured-outside-rrf.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reference/ADR/ADR-002-structured-outside-rrf.md))
- ADR-003: Knowledge Map as Routing Layer ([`docs/reference/ADR/ADR-003-knowledge-map.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reference/ADR/ADR-003-knowledge-map.md))
- Spec 0004: Core engine walking skeleton and benchmarks ([`docs/specs/0004-core-engine-walking-skeleton.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0004-core-engine-walking-skeleton.md))
- Spec 0003: Snapshot registry, publication, and atomic deletion ([`docs/specs/0003-snapshot-registry-publication-atomic-deletion.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/specs/0003-snapshot-registry-publication-atomic-deletion.md), deferred)
- Scope Definition: Slice 2 PageIndex Integration ([`docs/scope/scope.md`](file:///home/swyra/projects/knowledge-retrieval-engine/docs/scope/scope.md))
