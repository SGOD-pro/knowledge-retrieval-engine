# KRE Infrastructure Contract

## Existing Reusable Infrastructure
The repository currently contains FastAPI/LangGraph, DynamoDB, QdrantDB, Redis/ElastiCache, PDF extraction runtime, BGE runtime/service, provider adapters and a frontend.

## Logical Components
API/query runtime, ingestion workers, heavy parser runtime, embedding runtime, provider adapters, DynamoDB, QdrantDB, Redis, object storage, queue/workflow coordinator, observability.

## Principles
Stateless query workers where practical, durable artifacts, bounded concurrency, immutable artifacts, generation-checked publication, least privilege and private service communication where possible.
