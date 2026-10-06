# KRE Refactor Migration Map

## Existing Repository Reviewed
The current repository includes root design/evaluation docs, FastAPI routers, a unified repository facade, DynamoDB/Qdrant/Redis, ingestion adapters, providers, `modules/`, schemas, retrieval services, LangGraph orchestration, frontend workspace/chat/library/viewer/graph pages and benchmark/evaluation assets.

Current implementation also contains BM25, vector, PageIndex, OKF, graph, reranking, compression/fidelity and deterministic execution.

## Known Legacy Conflicts
1. Current query orchestration assumes staged BM25/PageIndex/vector behavior; target architecture is per-requirement capability routing.
2. Current LangGraph `_rrf_merge()` combines BM25/vector/graph chunks; target architecture keeps structured execution out of RRF and treats graph/OKF as optional enrichment/baseline fallback where ready.
3. Current OKF can participate as a pre-retrieval signal; target architecture does not require OKF for baseline readiness.
4. Existing docs contain historical phase/benchmark claims that require re-verification; do not import those claims as new acceptance evidence.
5. Existing configuration contains older provider/threshold assumptions; target contracts make readiness/coverage/budget explicit.
6. Existing direct ingestion is the current v1 path; target large-corpus workflow adds durable/bounded asynchronous processing without requiring every ordinary upload to become synchronous.

## Reuse First
FastAPI route families, repository/storage abstractions, compatible adapters, provider integrations, frontend components, benchmark utilities.

## Replace/Refactor First
Pipeline state/orchestration, planner contract, capability/snapshot registry, publication logic, structured/RRF boundary, verification interfaces, model-budget scheduler and ingestion/enrichment publication flow.
