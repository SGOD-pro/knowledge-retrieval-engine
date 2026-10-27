# Backend

## Overview
FastAPI service providing parsing, structural indexing, retrieval orchestration, and deterministic execution for the Knowledge Retrieval Engine. It manages ingestion pipelines for multiple file formats, stores vector and metadata indexes, and verifies evidence to answer queries reliably.

## Key files
| File | Owns |
|---|---|
| `src/main.py` | FastAPI application entry point, CORS settings, and router mounting |
| `src/config.py` | Pydantic application settings, environment controls, and search thresholds |
| `src/api/routes.py` | Central API router mounting modules for auth, workspaces, documents, query, chat, and graph |
| `src/security/auth.py` | Workspace authentication context and test principal provider |
| `src/services/langgraph_pipeline.py` | LangGraph query execution graph orchestrating planning, retrieval, and evidence verification |
| `src/services/retrieval/deterministic_executor.py` | Deterministic structured data executor for tabular queries |
| `src/services/retrieval/strategy_router.py` | Strategy router directing retrieval across evidence sources |
| `src/ingestion/parse_service.py` | Document parser routing files to specialized format adapters |
| `src/db/database.py` | Database access repository for DynamoDB and related data stores |
| `src/db/workspace_boundary_repo.py` | Workspace boundary repository for DynamoDB, SQLite, and memory stores |
| `src/schemas/contracts/` | Shared frozen Pydantic contracts and identity envelopes |
| `src/schemas/contracts/page_index.py` | Typed structural layout hierarchy, element location, and lookup contracts |
| `src/db/page_index_repo.py` | PageIndex summary repository and S3 artifact store with S3 first write ordering |
| `src/ingestion/page_index_extractor.py` | Deterministic PDF layout and structural hierarchy extractor |
| `src/services/retrieval/page_index_lookup.py` | Bounded concurrent structural lookup service with capacity retention and timeout handling |
| `src/services/retrieval/page_index_retriever.py` | Synchronous and asynchronous PageIndex structural lookup adapter |
| `src/services/retrieval/strategies/page_index.py` | PageIndex structural evidence strategy with targeted label extraction and snapshot chunk retrieval |
| `src/schemas/contracts/okf.py` | Typed OKF entity records, property assertions, manifests, and lookup results |
| `src/db/okf_repo.py` | Partitioned OKF fact repository for DynamoDB, SQLite, and in memory stores with GSI1 and GSI2 |
| `src/ingestion/okf_builder.py` | Bounded OKF extraction with chunk grounding verification and curated table schemas |
| `src/services/retrieval/okf_lookup.py` | Bounded concurrent OKF storage lookup service with capacity retention |
| `src/services/retrieval/okf_retriever.py` | Authoritative OKF retriever enforcing workspace boundaries and deadlines |
| `src/services/benchmark/` | Benchmark runner, S3 artifact store, and recoverable metric persistence |

## Commands
```bash
# Install dependencies
uv sync

# Run development server
uv run uvicorn src.main:app --port 8001 --reload

# Run test suite
uv run pytest

# Run single test
uv run pytest tests/test_contracts_and_identity.py
```

## Conventions
* Use Python 3.12 with uv for dependency and environment management.
* Cross module messages carry explicit identity envelopes with workspace, query, source, and snapshot identifiers.
* Structured query results produce mandatory evidence and never compete in reciprocal rank fusion.
* Ingestion pipelines produce source backed representations without assuming source truth.
* Keep query time model work bounded and track token or call budgets across steps.
* Failed fast path extractions escalate to full retrieval with refreshed candidate state, single generation limits, and structured refusal when evidence is absent.

## Gotchas
* Local development defaults to SQLite or in memory TableStore and LocalStack DynamoDB, while Qdrant runs via local container or cloud.
* Setting `MODEL_PROVIDER` determines whether Bedrock or external test providers are called.
* Table retrieval requires fidelity checks, so lower fidelity thresholds when working with dense tabular data.
* Query embedding reuse during escalation requires verified model provenance matching the full index; vectors without reliable provenance are replaced through the configured full embedding path.
* TableStore streams and persists all rows without row truncation; vector chunking caps at 5000 chunks for vector search, so vector chunk count in snapshots or vector storage must not be confused with TableStore completeness.
* Structured TableStore query execution requires defensible source binding before execution; if multiple tables remain equally compatible or without sufficient score separation (gap under 2.0), the engine raises AmbiguousBindingError instead of returning the first candidate that runs successfully.
* Structured aggregate queries bind requested entity and temporal constraints to typed store predicates before execution. If a requested filter cannot be resolved in schema or stored rows, the engine raises UnresolvedFilterError rather than computing an unconstrained whole table total, while valid filters matching zero rows return zero count and zero total distinct from binding failures.
* PageIndex structural extraction operates as an enrichment pass; enrichment errors preserve baseline chunks and snapshots without blocking ingestion.
* PageIndex lookup bounds admission capacity and retains worker capacity until background storage calls finish, distinguishing caller timeouts from storage cancellation and disabling SDK retries explicitly. Actual pipeline lookups route through this controller and bound total stage work across documents and labels with PAGEINDEX_STAGE_TIMEOUT_MS to prevent unbounded query delays. The stage deadline covers all storage work in the pipeline, including snapshot discovery, tombstone checks, manifest reads, and candidate chunk retrieval inside bounded workers, guaranteeing fail closed tombstone behavior and preventing late candidate publication.
* PageIndex persistence measures complete summary sizing including cross references, definitions, sections, and metadata, rechecking after overflow transformation to guarantee persisted summaries remain within configured byte bounds.
* PageIndex candidates are soft pooled into reciprocal rank fusion with a calibrated weight of 1.1 (divided by 2 for ambiguous definitions); candidate page scopes and chunk exclusions remain unconstrained so vector and BM25 discovery search across the full active workspace without hard gating.
* OKF extraction operates as an enrichment pass; extraction failures or missing properties preserve baseline chunks and fall back cleanly per ADR-001. Ingestion extraction enforces a single document level budget across attempts and retries with upfront reservation, defensible byte upper bounds on input tokens, and bounded provider call concurrency across documents with capacity retention. Attempt reservations are persisted to durable storage before provider calls, surviving single worker crashes, while atomic multi process distributed locking across distinct hosts remains an open limitation. Relational grounding rejects assertions attributing real values to the wrong entity or ungrounded property relationships.
* OKF lookups enforce a 50ms caller deadline via OkfConcurrencyController with SDK retries disabled and storage worker capacity retention; lookups carry authorized document and active version constraints, failing closed on snapshot metadata errors to prevent obsolete or unready manifest assertions from leaking into retrieval. Candidate chunks are soft pooled into reciprocal rank fusion with a calibrated weight of 1.15 (divided by 2 for ambiguous entities or conflicting properties).
* Benchmark accuracy comparison claims remain strictly blocked by validate_benchmark_comparison_preconditions until controlled comparison runs execute on ws_canonical_comparison_0004. Requiring ws_canonical_comparison_0004 is an additional guard, not proof of comparability or a replacement for manifest and configuration validation; post repair canonical evaluation remains pending, with zero measured accuracy improvement or live AWS readiness claimed.

## Related specs
* [docs/specs/0001-contracts-and-identity-envelope.md](../docs/specs/0001-contracts-and-identity-envelope.md)
* [docs/specs/0002-workspace-authorization-and-boundary-isolation.md](../docs/specs/0002-workspace-authorization-and-boundary-isolation.md)
* [docs/specs/0004-core-engine-walking-skeleton.md](../docs/specs/0004-core-engine-walking-skeleton.md)
* [docs/specs/0005-pageindex-structural-retrieval.md](../docs/specs/0005-pageindex-structural-retrieval.md)
* [docs/specs/0006-okf-integration.md](../docs/specs/0006-okf-integration.md)
* [docs/TECHNICAL_SPEC.md](../docs/TECHNICAL_SPEC.md)
* [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)
* [docs/REQUIREMENTS.md](../docs/REQUIREMENTS.md)

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
