# Review, re-factor, 2026-10-29

**Reviewed by**: Senior Code Reviewer
**Scope**: 4 files, uncommitted changes on branch `re-factor`
**Verdict**: Accepted (all findings resolved)

## Summary

This review assesses the uncommitted changes addressing client configuration, token bounding, offline regression testing, and Qdrant point resumption. The core token bounding logic in [`backend/src/ingestion/okf_builder.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py) is mathematically sound and guarantees request input tokens remain below or equal to 2000 across single and multiple chunk batches. The custom configuration support in [`backend/src/aws/infra.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/aws/infra.py) cleanly passes custom timeouts and retry settings through to boto3 clients while preserving caching. The fallback credentials session in [`OkfBuilder._get_bedrock_client`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L869-L895) correctly passes profile and region settings. The offline test suite in [`backend/tests/test_okf_client_and_token_bounds.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_okf_client_and_token_bounds.py) executes cleanly with isolated temporary storage without touching live production data.

All review findings, including the missing `sys.path` entry on standalone script execution and default wait configuration, have been resolved and verified with offline tests.

## Blockers

None.

## Major (Resolved)

### 🟢 Missing backend source directory on sys.path in resume script, [`backend/scripts/resume_canonical_qdrant_points.py:37`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L37)
**Resolution**: Resolved. Added directory resolution logic before application imports, prepending `backend/src` to `sys.path` dynamically. Verified standalone execution from repository root and backend directory.

## Minor (Resolved)

### 🟢 Default wait flag value in resume script, [`backend/scripts/resume_canonical_qdrant_points.py:83`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L83)
**Resolution**: Resolved. Configured `--wait` to default to `True` with dedicated 120s timeout and batch size 25, matching repository patterns and guaranteeing synchronous commit acknowledgement. Added eventual consistency polling loop for post execution scroll verification.

## Nits (Resolved)

* 🟢 [`backend/scripts/resume_canonical_qdrant_points.py:113`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/scripts/resume_canonical_qdrant_points.py#L113), replaced hardcoded filename with `csv_path.name`.
* ⚪ [`backend/src/aws/infra.py:170`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/aws/infra.py#L170), note recorded. Builder level caching limits client instantiations effectively.

## Strengths

* Robust token bounding: The byte length derivation in [`_invoke_bedrock_extraction`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L1441-L1466) strictly subtracts system prompt bytes, tag overhead, and envelope framing from the budget token limit. Because byte level BPE tokenizers encode at least 1 byte per token, this mathematical upper bound ensures total prompt tokens never exceed the 2000 token limit.
* Safe idempotent resume design: The resume script defaults to dry run mode, preventing accidental paid embedding calls without explicit authorization. Point identifiers use deterministic UUID5 derivation matching production vector store storage, and existing points are retrieved before any write operation.
* Clean fallback credential handling: Fallback Bedrock initialization in [`OkfBuilder._get_bedrock_client`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L869-L895) accurately resolves both profile and region settings from application configuration, preventing silent credential failures.
* Isolated offline regression suite: The test suite in [`backend/tests/test_okf_client_and_token_bounds.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_okf_client_and_token_bounds.py) uses mock sessions and temporary SQLite databases, guaranteeing zero impact on production `kre_okf.db` data while achieving full test execution in under one second.

## Test Coverage

* Test runner status: configured via pytest.
* Existing and new logic: New tests in [`test_okf_client_and_token_bounds.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_okf_client_and_token_bounds.py) thoroughly cover custom client config passing, client initialization, fallback credential passing, single chunk token limits from 50 to 10000 characters, multiple chunk batch limits, supported model validation, and repository isolation. All 11 new tests pass. All 74 OKF related regression tests pass with zero regressions.
