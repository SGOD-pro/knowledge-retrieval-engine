# KRE Architecture

## Canonical Flow

```text
SOURCE
  ↓
CANONICAL SOURCE-BACKED EVIDENCE
  ├──────────────→ BASELINE PUBLICATION ─────→ BASELINE SNAPSHOT
  │
  └──────────────→ OPTIONAL ENRICHMENT ──────→ ENRICHED SNAPSHOT

SNAPSHOT
  ↓
QUERY REQUIREMENTS
  ↓
KNOWLEDGE MAP + CAPABILITY ROUTING
  ├── STRUCTURED EXECUTION → MANDATORY EXECUTION EVIDENCE
  └── DISCOVERY RETRIEVAL
        ├── BM25
        ├── DENSE
        ├── PAGEINDEX
        ├── GRAPH / OKF
        └── VISUAL
              ↓
        COMPATIBLE RRF
              ↓
        BOUNDED RERANK
              ↓
      DISCOVERY EVIDENCE
              ↓
    REQUIREMENT JOIN
              ↓
 MECHANICAL + SEMANTIC VERIFY
              ↓
 SUPPORT + COMPLETENESS
              ↓
 ANSWER / LAYA
```

## Planes
1. **Ingestion:** immutable sources, adapters, extraction QA, canonical evidence.
2. **Knowledge:** capability manifest, baseline/enrichment artifacts, locators, Knowledge Map, snapshots.
3. **Execution:** structured operations and discovery retrieval.
4. **Answer:** requirement joins, exact evidence fetch, verification, answer assembly.

## Baseline vs Enrichment
Baseline must be independently usable. Enrichment is additive and asynchronous. Missing enrichment is not missing source evidence.

## Source of Truth
1. immutable source
2. canonical source-backed evidence
3. verified structured execution
4. Knowledge Map/OKF/structure
5. indexes
6. cache
7. LLM-generated language

Ingestion produces source-backed representations; it does not prove the source is true.

## Knowledge Map
Routing metadata answering:
- what exists
- where it is
- which version
- which capability can reach it
- coverage/readiness/exclusions

It is not answer proof.

## Structured vs Discovery
Structured execution is mandatory evidence and is never an RRF candidate. RRF only merges compatible ranked discovery candidates.

## LLM
Query-time model operations share a global ledger. Mandatory finishing capacity is reserved before optional work. No model output is authoritative.

## Lifecycle
Immutable artifacts plus generation-checked atomic publication. Deletion is an immediate access barrier. Enrichment failures preserve valid baseline snapshots.

## Large Corpus
Every expensive stage has explicit bounds, batching, pagination, checkpointing, retries, backpressure, deadlines, cancellation and no-progress termination.
