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

## Related specs
* [docs/specs/0001-contracts-and-identity-envelope.md](../docs/specs/0001-contracts-and-identity-envelope.md)
* [docs/specs/0002-workspace-authorization-and-boundary-isolation.md](../docs/specs/0002-workspace-authorization-and-boundary-isolation.md)
* [docs/specs/0004-core-engine-walking-skeleton.md](../docs/specs/0004-core-engine-walking-skeleton.md)
* [docs/TECHNICAL_SPEC.md](../docs/TECHNICAL_SPEC.md)
* [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)
* [docs/REQUIREMENTS.md](../docs/REQUIREMENTS.md)

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
