# Review, re-factor, 2026-11-17

**Reviewed by**: Gemini Pro (author on Gemini Pro)
**Scope**: 8 files, re-factor vs af02997
**Verdict**: Approve with nits

## Summary

This review evaluated the complete implementation of Spec 0007 Slice 2 (Grounded narrative PDF extraction and durable Bedrock budgeting) alongside carried forward Slice 1 nits. The changes introduce robust, fail closed extraction prompts and strict JSON parsing, verbatim directional syntax grounding validation, document scoped entity identity by default with verified cross document token joins, mandatory Bedrock `count_tokens` pre validation with per call token ceilings, atomic budget reservations and 15 second lease management with monotonic fencing on `BUDGET#LEDGER`, throttling retry accounting against the 2 call ceiling, and preservation of existing ready publications on attempt failures. The implementation exhibits high architectural discipline, complete test isolation, and verified atomic storage behavior across independent SQLite connections and live DynamoDB Local transactions.

## Nits

- ⚪ `backend/src/ingestion/graph_builder.py:531-532`: slicing raw text bytes at 2000 and decoding with `errors="ignore"` safely prevents UTF-8 decoding exceptions, but could discard trailing partial multibyte sequences; consider character-aware slicing if precise boundary preservation is needed.
- ⚪ `backend/src/ingestion/graph_builder.py:563`: fallback heuristic prompt token estimation (`len // 4`) applies only when no Bedrock client is provided; adding a brief comment clarifying this path is restricted to offline testing/doubles will assist future maintainers.
- ⚪ `backend/src/db/knowledge_graph_repo.py:1946`: in DynamoDB `commit_manifest`, the post-transaction lease clearing `update_item` swallows exceptions silently; logging unexpected exceptions at debug or warning level would improve operational observability.

## Strengths

- **Exemplary grounding counterexample rejection**: `validate_relationship_grounding` systematically rejects reversed active voice, reversed passive voice (`was <pred> by`), explicit sentence negation assertions (`not`, `neither ... nor`, etc.), co-occurrence across disjoint sentences, and co-occurrence alone without directional predicate, preventing hallucinated relational edges.
- **Strict document-scoped identity semantics**: Entities default strictly to document-scoped identities (`{workspace}:DOC#{document_id}:{type}:{name}`), eliminating homonym false joins across documents while supporting explicit verified disambiguation tokens (`{workspace}:{type}:{name}:{token}`).
- **Mandatory CountTokens pre-dispatch validation**: Bedrock Converse calls are strictly gated behind `count_tokens` validation; any missing capability, service error, non-positive count, or ceiling overrun (> 2000 prompt tokens) fails closed with zero model network calls.
- **Atomic distributed budget coordination & lease fencing**: `BUDGET#LEDGER` reservations atomically enforce 15-second worker leases, monotonic fence token advancement, and token ceilings. Stale workers are fenced out and denied publication while their token expenditures are reconciled into cumulative totals.
- **Throttling retry ceiling enforcement**: HTTP 429 throttling retries require a second atomic permit from the budget ledger, strictly counting against the 2-call maximum per document version. Non-throttling errors fail closed with zero retries.
- **Ready-publication preservation**: Failed re-extraction attempts durably record spent budget while strictly preserving existing ready publication manifests, ensuring queries continue serving valid baseline graph evidence.
- **Inner hydration loop deadline protection**: TableStore evidence hydration loop incorporates an explicit inner deadline check, halting table row hydration immediately on deadline overrun without unconstrained row fetching.
- **Thorough regression suite**: The 26-test suite in `test_knowledge_graph_slice2.py` exhaustively tests all grounding counterexamples, CountTokens failure modes, concurrent lease acquisition, stale worker fencing, crash restart recovery, retry ceilings, independent SQLite connections, and live DynamoDB Local conditional transactions.

## Test coverage

TESTS = configured. All 26 tests in `backend/tests/test_knowledge_graph_slice2.py` pass cleanly in 6.28s. In addition, Slice 1 regressions (65 tests), OKF Slice 3.1 regressions (57 tests), and TableStore regressions (6 tests) all pass cleanly (154 total passing tests).
