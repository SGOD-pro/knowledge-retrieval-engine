# KRE Project Definition

## Status
Pre-development refactor baseline. This document defines product intent and scope; it does not claim production validation.

## What KRE Is
KRE (Knowledge Retrieval Engine) is a source-grounded document intelligence system. It ingests authorized documents and structured files into source-backed representations, publishes a usable baseline independently of optional enrichment, and answers questions through the minimum sufficient execution/retrieval path.

Core ideas:
- source-backed evidence and exact provenance
- baseline lexical/vector/structured retrieval
- optional PageIndex/OKF/graph enrichment
- Knowledge Map routing
- deterministic structured execution
- bounded model/tool usage
- mechanical + semantic evidence verification
- explicit completeness and partial/failure states

## Product Goal
For a query over an authorized workspace, KRE should:
1. identify user requirements,
2. select the least expensive ready capability per requirement,
3. retrieve or execute against source-backed data,
4. verify support and completeness,
5. return only validated claims/results with provenance.

## Users
Analysts, researchers, engineers and organizations working over private mixed-format corpora where inspectable evidence matters.

## In Scope
PDF, DOCX, PPTX, CSV, XLSX, Parquet, JSON/records, captured HTML/web and supported visual/OCR sources; workspace/source/versioning; baseline retrieval; optional enrichment; Knowledge Map; structured execution; evidence verification; LAYA; API/UI/cloud integration.

## Out of Scope
Unbounded autonomous browsing, arbitrary code execution, treating generated summaries/maps as source truth, unbounded loops, and any claim of mathematically guaranteed zero hallucination.

## Non-Negotiables
- Baseline retrieval must not wait for optional enrichment.
- Structured results do not compete in RRF.
- Failed search is not proof that an answer does not exist.
- Material ambiguity is clarified, not guessed.
- Correlation is not causation.
- Query-time model/tool work is bounded and pre-reserved.
- Version, authorization and provenance travel with requests/results.

## Migration Context
The current repository already contains FastAPI/LangGraph, DynamoDB, QdrantDB, Redis, source adapters, retrieval services, structured execution, frontend pages and benchmark assets. The refactor replaces conflicting base contracts/orchestration while reusing compatible infrastructure.

## Documentation Authority
`PROJECT.md`, `REQUIREMENTS.md`, `ARCHITECTURE.md` and `TECHNICAL_SPEC.md` form the core design contract. `DIAGRAMS.md` and `diagrams/` are projections, not independent architecture decisions.
