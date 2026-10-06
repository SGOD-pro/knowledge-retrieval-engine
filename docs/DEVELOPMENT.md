# KRE Development Guide

## Documentation Loading Policy
For ordinary implementation tasks, an AI coding agent should load:
- `PROJECT.md`
- `REQUIREMENTS.md`
- `ARCHITECTURE.md`
- `TECHNICAL_SPEC.md`
- the relevant `reference/*.md`
- only the diagrams relevant to the current task

Do not inject the entire documentation tree unless the task is an architecture review.

## Refactor Order
```text
1 contracts/identity
2 snapshot/publication
3 baseline ingestion/evidence
4 structured executor
5 BM25/vector baseline
6 minimal map/capability registry
7 planner/routing/joins/budgets
8 verification/answer
9 LAYA
10 optional enrichment
11 API/UI
12 corpus-scale hardening
```

## Module Definition of Done
- matches contract
- identity/version enforced
- explicit errors
- budget/deadline honored
- provenance retained
- success/failure/boundary tests
- no undocumented state mutations

## Existing Code
Reuse compatible FastAPI, repository, adapters, providers, storage, frontend and benchmark code. Architecture contracts win over legacy orchestration.
