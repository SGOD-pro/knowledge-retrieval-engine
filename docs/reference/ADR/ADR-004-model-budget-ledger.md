# ADR-004 — Global Model Budget Ledger

## Decision
Every query-time model/tool invocation passes a pre-reservation gate. Typed counters cover generation, reranking, encoder verification, embeddings and LAYA.

## Rationale
Prevents hidden model work and protects mandatory finishing capacity.
