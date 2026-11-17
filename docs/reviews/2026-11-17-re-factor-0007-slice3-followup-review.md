# Review, re-factor, 2026-11-17

**Reviewed by**: pro (author on sonnet)
**Scope**: 16 files, branch vs 7121c001a10572b27d89481fc58ad7b5fd1921c0
**Verdict**: Approve

## Summary
This follow-up review evaluates the resolution of findings from the prior Slice 3 review for Spec 0007 (Bounded Knowledge Graph Enrichment — Pipeline Fusion, Telemetry, and Offline Ablation) on the `re-factor` branch vs `7121c001a10572b27d89481fc58ad7b5fd1921c0`. All identified findings have been comprehensively and cleanly resolved: `GraphStatus.EMPTY` and `DocumentSnapshot.graph_status` now explicitly accept `"empty"` for zero-entity extractions with dedicated regression test coverage; `get_shared_knowledge_graph_repository` properly recreates and returns a fresh singleton instance upon `reset=True` matching static return type annotations; `KnowledgeGraphStrategy` provides robust backward compatibility for legacy callers passing `CloudRepository`; and `run_graph` safely handles `snapshot_context` propagation and dataclass replacement. The entire 142-test Knowledge Graph suite passes cleanly in 13.44s with zero cloud leaks or unbudgeted provider calls, satisfying all Slice 3 acceptance criteria (AC-3, AC-10, AC-11, AC-12, AC-13).

## Strengths
- **Complete and Robust Resolution of Review Findings**: Every finding from the prior review (the Major `graph_status` Pydantic validation error on empty manifest extractions, the two Minors in singleton reset behavior and legacy strategy initialization, and both nits in `run_graph`) was addressed cleanly with targeted fixes and direct regression tests in `test_knowledge_graph_ablation_slice3.py`.
- **RRF Soft Pooling and Counterexample Discovery Safety**: Confirms reciprocal rank fusion soft pooling with calibrated provisional weights (1.10, halved to 0.55 on ambiguity/conflict) without hard page exclusion or pruning, verified by tests showing an acquisition on Page 2 does not displace a factual answer on Page 18.
- **Fast-Path Bypass and ADR-001 Decoupling**: Fast-path queries cleanly bypass graph lookups with 0 tokens and 0ms latency, while document ingestion enrichment runs as an independent pass per ADR-001 that never fails baseline document publication or core query answering.
- **Comprehensive Candidate Telemetry Exposure**: Accurately surfaces all 9 graph telemetry fields (`graph_candidate_ids`, `graph_match_count`, `graph_latency_ms`, `graph_traversed_nodes`, `graph_matched_edges`, `graph_was_truncated`, `graph_truncation_reason`, `graph_entity_state`, `graph_relationship_state`) into `QueryResponse` and `retrieval_candidates` trace.
- **Strict Benchmark Comparison Barrier**: Enforces comparison barriers via `validate_benchmark_comparison_preconditions`, preventing unverified accuracy claims on non-canonical workspaces and prohibiting paid benchmark runs without explicit authorization.

## Test coverage
TESTS = configured. All 142 tests across the knowledge graph test suite (`test_knowledge_graph_ablation_slice3.py`, `test_knowledge_graph_slice1.py`, and `test_knowledge_graph_slice2.py`) pass in 13.44s. The previously untested logic gap around empty manifest extractions (`status="empty"`) is now explicitly tested in `test_documents_service_snapshot_metadata_ac12`, verifying that `DocumentSnapshot` instantiates and persists valid metadata when zero entities are discovered.
