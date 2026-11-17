# Review, re-factor, 2026-11-17

**Reviewed by**: pro (author on sonnet)
**Scope**: 14 files, branch vs 7121c001a10572b27d89481fc58ad7b5fd1921c0
**Verdict**: Changes requested

## Summary
This review evaluates the implementation of Spec 0007 Slice 3: Bounded Knowledge Graph Enrichment — Pipeline Fusion, Telemetry, and Offline Ablation (`re-factor` branch vs `7121c001a10572b27d89481fc58ad7b5fd1921c0`). The change successfully integrates `KnowledgeGraphRetriever` into the retrieval pipeline and reciprocal rank fusion with calibrated provisional weights (1.10, halved to 0.55 on ambiguity/conflict), retires catastrophic `get_all_chunks` calls in favor of targeted point hydration, exposes comprehensive candidate telemetry in `QueryResponse`, and enforces strict baseline decoupling per ADR-001 alongside benchmark comparison barriers. However, a Major contract discrepancy was identified: when a document yields zero graph entities, `extract_and_persist_graph` emits manifest status `"empty"`, which causes `DocumentSnapshot` construction to raise a Pydantic `ValidationError` and fail snapshot persistence for standard documents. Addressing this status mapping issue along with minor singleton reset and legacy strategy initialization contracts will bring the feature to merge readiness.

## Major
### 🟠 Invalid `graph_status` on `DocumentSnapshot` for empty extractions, `backend/src/modules/documents/documents_service.py:133`
**Problem**: When document ingestion produces zero graph entities or relationships (e.g. tabular data without foreign keys or narrative text with zero recognized entities), `extract_and_persist_graph` emits a `KnowledgeGraphManifest` with `status="empty"`. In `DocumentsService._enrich_graph_snapshot_metadata` (line 133), `stat` evaluates to `"empty"` and returns `("empty", 0, 0)`. Then in `background_ingest` (line 357) and `ingest_file` (line 616), `DocumentSnapshot` is instantiated with `graph_status="empty"`. Because `DocumentSnapshot.graph_status` is typed as `Literal["pending", "processing", "ready", "failed", "unavailable"] | None`, passing `"empty"` triggers a `pydantic_core.ValidationError` (`Input should be 'pending', 'processing', 'ready', 'failed' or 'unavailable'`).
**Why it matters**: Any ingested file lacking extractable graph entities fails `DocumentSnapshot` validation during enrichment (`Could not persist enriched snapshot for ...`), preventing the updated document snapshot from being saved into `WorkspaceBoundaryRepository`.
**Suggested fix**: Map `"empty"` manifest status to `"unavailable"` in `_enrich_graph_snapshot_metadata` (or extend `DocumentSnapshot.graph_status` and `GraphStatus` in `backend/src/schemas/contracts/knowledge_graph.py` to accept `"empty"` if distinguishing empty extractions from unavailable graphs is required).

## Minor
### 🟡 Return type annotation and reset behavior inconsistency in `get_shared_knowledge_graph_repository`, `backend/src/db/knowledge_graph_repo.py:2760`
**Problem**: `get_shared_knowledge_graph_repository` is annotated with return type `-> KnowledgeGraphRepositoryBase`, but when called with `reset=True`, it clears `_SHARED_KG_REPO = None` and executes `return None`. In `test_knowledge_graph_ablation_slice3.py:77-81`, calling `kg_repo = get_shared_knowledge_graph_repository(reset=True)` yields `None` into the test fixture.
**Why it matters**: Violates static type contracts for callers expecting a repository instance, and diverges from `get_shared_table_store(reset=True)`, which recreates and returns a fresh repository instance upon reset.
**Suggested fix**: If returning `None` on reset is intended, annotate the return type as `KnowledgeGraphRepositoryBase | None`. Otherwise, when `reset=True`, re-initialize and return a fresh repository instance matching configured backend settings.

### 🟡 `KnowledgeGraphStrategy.__init__` parameter mismatch for legacy `repository` callers, `backend/src/services/retrieval/strategies/knowledge_graph.py:48`
**Problem**: `KnowledgeGraphStrategy.__init__` was refactored to `(retriever, repository, boundary_repo, documents_repo)` where `repository` expects a `KnowledgeGraphRepositoryBase`. The legacy implementation accepted `(repository: CloudRepository | None = None)`. If a caller passes `repository=repo` where `repo` is a `CloudRepository` (such as in `backend/scripts/evaluate_retrieval_strategies.py:334`), `CloudRepository` is bound to `self.repository`. `CloudRepository` lacks methods like `resolve_mentions` and `get_manifest`, causing a runtime `AttributeError` when `self.retriever.expand` is executed.
**Why it matters**: Existing evaluation scripts or external callers passing `repository=repo` (as `CloudRepository`) to `KnowledgeGraphStrategy` will fail during graph retrieval.
**Suggested fix**: In `KnowledgeGraphStrategy.__init__`, inspect if the provided `repository` lacks `resolve_mentions` (or is an instance of `CloudRepository`/`DocumentsRepository`), and if so, assign it to `documents_repo` while resolving `self.repository` via `get_shared_knowledge_graph_repository()`.

## Nits
- ⚪ `backend/src/services/langgraph_pipeline.py:755`, `dataclasses.replace` is guarded by `if hasattr(c, "graph_weight")` rather than `if dataclasses.is_dataclass(c)`. If a non-dataclass mock defining `graph_weight` is passed in tests, `dataclasses.replace` raises `TypeError`.
- ⚪ `backend/src/services/langgraph_pipeline.py:738`, `run_graph` does not pass `snapshot_context=state.get("snapshot_context")` to `retriever.expand`, preventing pre-pinned snapshot contexts from being injected through `PipelineState`.

## Strengths
- **ADR-001 Baseline Decoupling and Fast-Path Bypass**: `run_graph` and `KnowledgeGraphStrategy` cleanly bypass graph traversal with 0ms and 0 tokens on fast-path queries, and extraction/retrieval errors fall back gracefully without degrading baseline BM25/vector search or core query answering.
- **RRF Soft Pooling and Counterexample Discovery Safety**: Correctly implements reciprocal rank fusion soft pooling with provisional weight 1.10 (divided to 0.55 on ambiguity/conflicts) without hard page exclusion or pruning, validated by thorough counterexample tests proving an acquisition on Page 2 does not displace a factual answer on Page 18.
- **Permanent Elimination of Whole-Workspace Scans**: Replaced catastrophic `get_all_chunks` workspace hydration with targeted point hydration capped at 15 items via version-aware batch lookups.
- **Comprehensive Candidate Telemetry Exposure**: Accurately threads and surfaces all 9 graph telemetry fields (`graph_candidate_ids`, `graph_latency_ms`, `graph_match_count`, `graph_traversed_nodes`, `graph_matched_edges`, `graph_was_truncated`, `graph_truncation_reason`, `graph_entity_state`, `graph_relationship_state`) into `QueryResponse` and `retrieval_candidates` trace.

## Test coverage
TESTS = configured. The new 12-test ablation suite in `backend/tests/test_knowledge_graph_ablation_slice3.py` passes cleanly in 6.59s, and all 141 tests across the entire knowledge graph suite (`test_knowledge_graph*`) pass in 12.82s. The suite thoroughly verifies RRF soft pooling, counterexample discovery preservation, ambiguity/conflict weight division, fast path bypass, candidate telemetry exposure, recall/precision/latency ablation comparison, benchmark comparison preconditions, tenant boundary isolation, timeout fallback to baseline, and dynamic tombstones.

*Untested Logic Gap*: Ingestion extraction yielding zero entities/relations (manifest status `"empty"`) is currently untested; `test_documents_service_snapshot_metadata_ac12` only mocked `manifest["status"] = "ready"` and runtime exceptions (`"failed"`), missing the `"empty"` status transition that uncovered the Major finding.
