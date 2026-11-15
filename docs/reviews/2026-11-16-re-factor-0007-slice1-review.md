# Review, re-factor, 2026-11-16

**Reviewed by**: Gemini Pro (author on Gemini Flash)
**Scope**: 19 files, re-factor vs 0c6bf35
**Verdict**: Approve with nits

## Summary

This re-review evaluated the resolution of all findings identified in the prior review of Spec 0007 Slice 1 (Deterministic tabular graph and traversal). The author has thoroughly resolved both Major findings (SQLite TOCTOU race in manifest commit and repository singleton bleed across tests) and all Minor findings (inner traversal deadline propagation, regex word-boundary grounding validation, DynamoDB edge pagination consolidation, parse_service version preservation, and TableStore thread-lock safety). The implementation demonstrates high architectural rigor, fail-closed correctness, and strong test isolation, with 65 unit and regression tests passing cleanly.

## Nits

- ⚪ `backend/src/db/knowledge_graph_repo.py:540`, `WORKSPACE_SQLITE_PATH` fallback for `GRAPH_SQLITE_PATH` remains a coupling to an unrelated config key; prefer falling back directly to the dedicated default database path.
- ⚪ `backend/src/services/retrieval/knowledge_graph_retriever.py:746`, TableStore row evidence hydration loop could explicitly check `(time.perf_counter() - t_start) >= rem_s` to halt immediately if the deadline is exceeded during table evidence synthesis.
- ⚪ `backend/src/db/knowledge_graph_repo.py:982`, hardcoded dummy credentials `aws_access_key_id="test"` for DynamoDB Local are standard but could benefit from a brief clarifying comment for static security scanners.

## Strengths

- **Authoritative atomic commit & elimination of TOCTOU**: The SQLite `commit_manifest` now performs a single authoritative check within the exclusive `BEGIN IMMEDIATE` transaction, cleanly closing the race condition without redundant reads.
- **Strict test repository isolation**: Eliminating `_DEFAULT_KG_REPO` ensures every test execution in test/memory environments receives a completely clean, isolated `InMemoryKnowledgeGraphRepository`, preventing subtle state bleed.
- **Robust regex word boundary grounding**: `validate_relationship_grounding` now uses word-boundary iterators (`\b` + escaped entity + `\b`) across all entity occurrences, eliminating substring collision bugs for compound entity names.
- **Deadline-aware traversal & prioritized chunk hydration**: Traversal now actively checks remaining deadline budget (`rem_s`) prior to Hop 2 and chunk hydration, while sorting hydrated chunks by `(confidence DESC, hop_distance ASC, chunk_id ASC)`.
- **Targeted hydration without table scans**: Chunk hydration is capped at 15 items via exact point lookups (`DOC#{did}`, `CHUNK#{cid}`) with zero calls to `get_all_chunks`, and TableStore row fetching uses targeted index lookups with `LIMIT 1`.
- **Exemplary regression testing**: The 65-test suite in `test_knowledge_graph_slice1.py` comprehensively covers contract immutability, optimistic concurrency, fencing, homonym disambiguation, dynamic tombstoning, and pagination limits across Memory, SQLite, and DynamoDB Local.

## Test coverage

TESTS = configured. All 65 tests in `backend/tests/test_knowledge_graph_slice1.py` pass in 4.04s. The suite thoroughly exercises the newly resolved code paths, including `test_factory_isolation_in_test_environment`, `test_validate_relationship_grounding_word_boundary_overlap`, and `test_resolve_mentions_past_30_items`.
