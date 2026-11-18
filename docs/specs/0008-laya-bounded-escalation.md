# 0008. Bounded LAYA Escalation and Query Expansion

**Date**: 2026-11-18
**Status**: Proposed

## Summary

This specification defines bounded LAYA escalation for the query execution pipeline. When initial baseline discovery fails to retrieve sufficient supporting evidence or leaves request coverage incomplete, LAYA activates as a bounded decision model. It never writes user facing answer prose. Instead, it inspects a structured description of missing requirements and proposes one targeted expansion route, such as lexical, dense, structural, graph, or tabular execution. Code strictly validates the proposed route against active permissions, readiness, and budget limits before executing. The pipeline preserves baseline evidence, guarantees at most one answer generation call per query, and returns a structured refusal if evidence remains unsupported.

## Context

The Knowledge Retrieval Engine currently executes query processing through a compiled LangGraph workflow. That workflow runs hybrid baseline discovery (BM25 lexical search, vector similarity search, PageIndex layout navigation, and Open Knowledge Framework property lookups), followed by optional Knowledge Graph traversal, reciprocal rank fusion reranking, and deterministic tabular math. If deterministic math does not answer the question, the pipeline passes fused candidates to context compression, evidence fidelity verification, and answer synthesis.

However, when initial discovery retrieves weak, off topic, or incomplete evidence, `run_fidelity` raises a coverage error. Today, that coverage error immediately terminates query processing with `final_answer="NOT_FOUND"`. In many realistic questions, the missing evidence exists in the corpus under synonymous wording, a specific document section, or an unlinked table cell, but discovery missed it because the original query phrasing was ambiguous or vocabulary mismatched.

Unconstrained multi turn agent loops and open ended search again loops introduce severe operational risks:
1. Unpredictable latency: autonomous agent loops can loop indefinitely, exhausting system thread pools and violating caller timeouts.
2. Uncontrolled model costs: generating repeated intermediate reasoning traces and speculative answers multiplies billable token expenditures.
3. Hallucination amplification: models that re query repeatedly on low confidence contexts often drift into fabricated claims or speculative answers.
4. Loss of baseline evidence: speculative re querying can discard or displace true factual baseline chunks found in initial passes.

Architecture Decision Record ADR-004 (Model Budgets) and Architecture Bible sections 17, 34, 35, and 36 establish the governing principles for bounded escalation. Escalation must be bounded by explicit attempt ceilings, hard deadlines, candidate caps, and no progress detection. LAYA must function strictly as a decision model that proposes a missing requirement route, never generating user answer prose. Code must validate all proposed routes before dispatch, and answer synthesis must execute at most once per query.

## Requirements

**User stories**:
- As a retrieval engineer, I want queries with vocabulary mismatch or missing section context to trigger one targeted expansion so that factual answers are retrieved without infinite search loops.
- As a system operator, I want escalation to enforce strict call, token, and deadline budgets so that complex queries never overrun latency targets or deplete worker capacity.
- As an enterprise tenant, I want escalation decisions and expanded retrievals to remain strictly isolated within my authorized workspace and active document snapshots, honoring deletion tombstones fail closed.
- As an API consumer, I want queries that remain unsupported after escalation to produce an explicit, structured refusal with zero citations, rather than speculative or unverified hallucinations.

**Acceptance criteria**:
- **AC-1**: Bounded escalation triggers if and only if initial baseline discovery lacks sufficient evidence support (such as coverage error in fidelity verification, empty candidate pool, or missing required operands) and the query budget retains an unused expansion slot (`expansion_steps == 0` and `laya_calls < MAX_QUERY_LAYA_CALLS`). Escalation does not activate when baseline evidence is sufficient, when deterministic math answered the query, or when remaining deadline is below the minimum threshold.
- **AC-2**: LAYA operates strictly as a typed decision model and never emits user facing answer prose. It accepts a typed `LayaEscalationRequest` containing query identity, authorized workspace identity, a structured `MissingRequirementDescription`, and summarized baseline evidence locators. It returns a typed `LayaEscalationDecision` specifying one chosen route (`lexical_expansion`, `dense_expansion`, `structural_expansion`, `graph_expansion`, `structured_expansion`, or `none`) alongside bounded route parameters and a numeric decision confidence.
- **AC-3**: Code validates the proposed decision before execution. If LAYA proposes an unauthorized route, an inactive feature (such as `graph_expansion` while `GRAPH_ENABLED=false`), an unknown document, or malformed parameters, code rejects the proposal fail closed and invokes the deterministic missing requirement policy defined in Architecture Bible section 35. If LAYA times out, throws an exception, or returns invalid syntax, the pipeline catches the error gracefully, sets `laya_execution_status="storage_failed"` or `"timeout"`, retains baseline evidence intact, and bypasses further expansion.
- **AC-4**: Authoritative resource budgets govern escalation under ADR-004. Escalation is limited to at most 1 LAYA decision call per query (`MAX_QUERY_LAYA_CALLS = 1`), at most 1 targeted expansion step (`MAX_EXPANSION_STEPS = 1`), at most 1000 input prompt tokens, at most 250 output decision tokens, and a hard 2.5 second total wall clock deadline. Retries on model errors, bad requests, or timeouts are strictly zero; at most 1 retry is permitted on HTTP 429 throttling if remaining time permits. Worker concurrency is governed by an admission queue capping active escalation tasks at 4.
- **AC-5**: Workspace, document version, and tombstone isolation are enforced across escalation. LAYA receives only metadata and evidence bound to the active workspace and pinned snapshot context. Any expanded retrieval executes strictly within the authorized workspace. Active source or workspace tombstones in `WorkspaceBoundaryRepository` are checked dynamically before and after expansion, returning zero candidates immediately fail closed on tombstone detection.
- **AC-6**: Exactly one answer generation call is permitted per query. Answer synthesis (`run_llm`) executes only after all retrieval and verification stages have settled. LAYA decision tokens do not count toward or consume answer generation slots (`MAX_QUERY_LLM_CALLS = 1`). If evidence remains unsupported after escalation, the pipeline bypasses answer synthesis entirely, returning a deterministic refusal with zero generation calls.
- **AC-7**: Expanded retrieval candidates pool into evidence collection alongside baseline candidates. If post expansion verification succeeds, the answer is synthesized with exact chunk citations. If evidence remains insufficient, the engine emits a structured refusal (`final_answer="NOT_FOUND"`, `citations=[]`, `confidence_score=0.0`, `retrieval_incomplete=True`). Telemetry records `laya_triggered`, `laya_route`, `laya_latency_ms`, `laya_tokens`, `laya_execution_status`, and candidate count deltas in `QueryResponse.retrieval_candidates`.
- **AC-8**: Comprehensive offline ablation test harness verifies escalation behavior without paid provider calls. Offline tests use mocked LAYA decision doubles, verifying successful expansion, fallback on provider failure, code rejection of invalid routes, tenant isolation, tombstone halting, no progress suppression, and strict enforcement of the single answer generation invariant.

## Options considered

### Option 1: Bounded Typed LAYA Decision Node with Code Validated Targeted Expansion (Recommended)

Integrate LAYA as a specialized decision node in the LangGraph query pipeline between initial evidence verification and final synthesis. LAYA accepts a structured missing requirement description and outputs a strictly typed JSON routing decision. Code validates the route and dispatches a single targeted retrieval expansion across lexical, dense, structural, graph, or tabular adapters. Expanded evidence rejoins the candidate pool and undergoes secondary verification before single pass answer synthesis.

**Pros**:
- Strictly bounds execution to at most one decision call and at most one expansion step, preventing latency runaway.
- Decouples routing intelligence from answer generation, ensuring LAYA never hallucinates answer text.
- Reuses existing BM25, vector, PageIndex, OKF, and TableStore retrieval adapters without building new search engines.
- Code validation acts as an infallible safety gate, rejecting unauthorized or disabled features.
- Preserves baseline discovery chunks when expansion fails or produces zero additional evidence.
- Guarantees at most one answer generation call per query.

**Cons**:
- Does not solve highly exploratory multi question research tasks requiring three or more iterative search hops (accepted, as multi hop exploratory research belongs to offline agent workflows, not low latency query serving).

### Option 2: Autonomous ReAct Agent Loop with Dynamic Re-querying

Deploy an autonomous agent loop (such as LangChain ReAct or LangGraph multi agent supervisor) that grants the language model direct tool calling access to search APIs, allowing it to inspect partial results, re formulate queries, and iterate until satisfied.

**Pros**:
- Flexible for complex open ended research tasks.

**Cons**:
- Unpredictable latency and high variance, frequently exceeding strict interactive query deadlines.
- High token cost caused by repeated verbose reasoning traces.
- Prone to infinite tool loops and hallucinated arguments when source evidence is absent.
- Bypasses deterministic workspace, version, and tombstone security guarantees unless wrapped in complex guards.
- Violates Architecture Bible section 17 budget caps and the single generation invariant.

### Option 3: Static Multi Query Expansion without Decision Model

Expand all queries at query start by generating 3 to 5 synthetic paraphrases or sub queries using deterministic heuristics or an initial language model call, executing all variants in parallel across discovery.

**Pros**:
- Conceptually simple pipeline without conditional escalation branches.

**Cons**:
- Imposes heavy cost and latency penalties on every query, including simple factual lookups that succeed on the first pass.
- Floods reciprocal rank fusion with noisy, redundant candidates, diluting top ranked precision.
- Does not incorporate knowledge of what specific requirement or evidence dimension was missing.

## Decision

**Chosen option**: Option 1: Bounded Typed LAYA Decision Node with Code Validated Targeted Expansion.

We will implement LAYA as a bounded, code validated decision node that triggers only when baseline discovery lacks required evidence support. It evaluates a structured missing requirement description, proposes one targeted expansion route, and relies on code to validate and execute the expansion within strict budget and isolation boundaries.

## Rationale

Option 1 directly satisfies Architecture Bible sections 17, 34, 35, and 36 while honoring Architecture Decision Records ADR-001 (Baseline Independent Retrieval) and ADR-004 (Model Budgets).

By restricting LAYA to a decision model that returns typed JSON choices rather than answer prose, we eliminate the risk of model generated hallucinations reaching the user. By placing code as an authoritative gate between the model's proposal and retrieval execution, the system prevents unauthorized cross tenant access, respects feature flags (such as disabled Knowledge Graph), and enforces hard resource ceilings.

Furthermore, gating escalation on verified evidence insufficiency ensures that the fast path and clean baseline discovery queries incur zero LAYA latency and zero extra token expense. Restricting escalation to exactly one expansion step guarantees that interactive query latency remains bounded within predictable SLA limits.

## Feature design

### Typed Contracts Sketch

The contracts are defined as frozen Pydantic models with strict extra field forbidding:

```python
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

class MissingRequirementDescription(BaseModel):
    """Structured characterization of missing evidence causing escalation."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: str = Field(..., max_length=64)
    missing_dimension: Literal["location", "exact_term", "semantic_concept", "relationship", "tabular_operand"]
    description: str = Field(..., max_length=300)
    failed_stage: str = Field(..., max_length=64)
    detected_gap: str = Field(..., max_length=300)


class LayaEscalationRequest(BaseModel):
    """Input payload provided to the LAYA decision model."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str = Field(..., max_length=64)
    workspace_id: str = Field(..., max_length=64)
    query: str = Field(..., max_length=1000)
    missing_requirement: MissingRequirementDescription
    available_routes: tuple[str, ...] = Field(default_factory=tuple)
    retrieved_summary: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    caller_deadline_ms: float = Field(default=2500.0, ge=0.0)


class LayaEscalationDecision(BaseModel):
    """Typed decision output produced by LAYA and validated by code."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(..., max_length=64)
    route: Literal[
        "lexical_expansion",
        "dense_expansion",
        "structural_expansion",
        "graph_expansion",
        "structured_expansion",
        "none",
    ]
    parameters: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=200)
    execution_status: Literal[
        "proposed",
        "approved",
        "rejected_by_code",
        "budget_exhausted",
        "timeout",
        "storage_failed",
    ] = "proposed"
```

### State Machine and Pipeline Topology

In `langgraph_pipeline.py`, LAYA is integrated via conditional routing after initial evidence evaluation:

```
[route_query]
      |
[baseline discovery: BM25 / PageIndex / Vector / OKF / Graph]
      |
[run_reranker (RRF pool)]
      |
[run_deterministic_math] ----(answered)----> [END]
      |
  (unanswered)
      |
[run_compressor]
      |
[run_fidelity (evidence check)]
      |
      +---> (evidence sufficient) ------------> [run_llm (synthesis)] ---> [END]
      |
      +---> (evidence missing & budget available)
                 |
          [run_laya_decision]
                 |
          [validate_and_dispatch_expansion]
                 |
          [merge_expanded_evidence]
                 |
          [recheck_fidelity]
                 |
                 +---> (sufficient) ----------> [run_llm (synthesis)] ---> [END]
                 +---> (still insufficient) ---> [refusal_exit] ----------> [END]
```

### Value sourcing

| Action | Value produced / displayed | Source |
|---|---|---|
| Trigger escalation | Escalation trigger decision | Derived in `run_fidelity` from coverage check failure and budget ledger `expansion_steps < 1` |
| Formulate missing requirement | `MissingRequirementDescription` | Derived from fidelity verification gap analysis and requirement status |
| Decide expansion route | `LayaEscalationDecision` | Computed by LAYA model or fallback deterministic missing requirement policy |
| Validate route | Route approval / rejection | Computed by code validation against active settings, workspace ACLs, and tombstones |
| Execute expansion | Expanded candidate chunks | Retrieved from existing BM25, Vector, PageIndex, OKF, or TableStore adapters |
| Answer generation | Final synthesis text | Synthesized in `run_llm` if and only if evidence verification passes |
| Answer refusal | `final_answer = "NOT_FOUND"` | Emitted deterministically with empty citations when evidence remains insufficient |
| Telemetry logging | `laya_*` debug trace | Recorded in `QueryResponse.retrieval_candidates` from LAYA execution timings and tokens |

### Key invariants

1. **Strict Decision Boundary**: LAYA returns only routing decisions; it never generates answer text or user facing responses.
2. **At Most One Answer Generation**: Exactly one answer synthesis call can execute per query. If evidence is missing, answer generation is bypassed and the engine refuses cleanly.
3. **At Most One Expansion Step**: `MAX_EXPANSION_STEPS = 1`. Escalation never chains into secondary or tertiary expansion loops.
4. **Code Validation Primacy**: Model proposed routes are untrusted suggestions. Code validates permissions, feature flags, version bindings, and budgets before executing any expansion.
5. **Fail Closed Baseline Preservation**: Any error, timeout, or invalid proposal during escalation leaves baseline evidence intact and fails closed without throwing unhandled exceptions.
6. **No Progress Suppression**: If an expansion retrieves chunks that are already present in the candidate pool, escalation terminates immediately without further attempts.

### Security and Isolation Model

- Tenant boundary: All LAYA invocations pass explicit `workspace_id`. Prompt context contains only documents authorized in the current workspace.
- Version binding: Expansion lookups target only active document versions pinned in `PinnedSnapshotContext`.
- Dynamic tombstone check: `WorkspaceBoundaryRepository.has_tombstone` is re verified before dispatching any expanded retrieval. If a source was deleted during query execution, the expansion returns zero candidates immediately fail closed.

### Critical test scenarios

- **Scenario 1 (Happy Path Lexical Expansion)**: Query with synonymous terminology fails initial discovery. LAYA analyzes the missing term, proposes `lexical_expansion` with targeted keywords. BM25 retrieves the missing chunk. Secondary verification succeeds. Single answer generation produces verified answer with exact citation. (AC-1, AC-2, AC-6, AC-7)
- **Scenario 2 (Happy Path Dense Expansion)**: Query with abstract semantic gap fails initial discovery. LAYA proposes `dense_expansion` with reformulated concept query. Vector store retrieves supporting chunk. Verification succeeds and answer is synthesized. (AC-2, AC-3)
- **Scenario 3 (Code Rejection of Disabled Route)**: LAYA proposes `graph_expansion` while `GRAPH_ENABLED=false`. Code validation rejects the proposal, falls back to deterministic policy or terminates cleanly without graph access. Baseline retrieval is preserved. (AC-3, AC-5)
- **Scenario 4 (Provider Timeout and Failure)**: LAYA provider call times out or throws an exception. Pipeline catches error fail closed, records `laya_execution_status="timeout"`, retains baseline evidence, and emits clean refusal without crashing. (AC-3, AC-4)
- **Scenario 5 (Tombstoned Source Rejection)**: LAYA proposes structural expansion on a document that has an active deletion tombstone. Code detects tombstone, suppresses expansion, and returns zero additional candidates. (AC-5)
- **Scenario 6 (No Progress Refusal)**: LAYA expansion returns chunks identical to those already retrieved. No progress detector halts escalation. Pipeline refuses cleanly with `NOT_FOUND` and zero answer generation calls. (AC-6, AC-7)

## Build plan (Tracer Bullet Approach)

The build plan follows a vertical Tracer Bullet slice, implementing the thinnest end to end path through the layers first:

1. **Contracts and Error Envelopes**: Implement frozen Pydantic models `MissingRequirementDescription`, `LayaEscalationRequest`, and `LayaEscalationDecision` in `backend/src/schemas/contracts/laya.py`. Satisfies **AC-2**.
2. **Deterministic Missing Requirement Policy**: Implement Architecture Bible section 35 deterministic fallback rules in `backend/src/services/retrieval/laya_policy.py`, mapping missing dimensions to approved expansion routes. Satisfies **AC-3**.
3. **LAYA Decision Provider Boundary**: Implement `LayaDecisionService` in `backend/src/services/retrieval/laya_service.py` with Bedrock Converse structured output parsing, token estimation, and offline test double injection. Satisfies **AC-2**, **AC-4**.
4. **Code Validation and Pre Invocation Gate**: Implement validation logic checking feature availability, workspace boundaries, budget ceilings, and tombstone status before executing proposed routes. Satisfies **AC-3**, **AC-4**, **AC-5**.
5. **Targeted Lexical and Dense Expansion Handlers**: Wire targeted single pass retrieval handlers in `LayaExpansionExecutor` that execute approved lexical keyword searches or dense vector expansions without re running the entire discovery pipeline. Satisfies **AC-1**, **AC-5**.
6. **LangGraph Pipeline Integration**: Wire `run_laya_escalation` node into `backend/src/services/langgraph_pipeline.py` after `run_fidelity`, updating state, preserving baseline chunks, enforcing no progress detection, and enforcing the single generation limit. Satisfies **AC-1**, **AC-6**, **AC-7**.
7. **Offline Ablation and Acceptance Regression Suite**: Implement comprehensive offline test suite in `backend/tests/test_laya_escalation_slice5.py` exercising happy path expansions, code rejection, timeouts, tombstones, and refusal behavior with mocked providers. Satisfies **AC-8**.

## Consequences

**Positive**:
- Solves factual answer drops caused by simple vocabulary mismatches or missing section locators.
- Eliminates the risk of runaway agent loops by bounding escalation to exactly one decision call and one expansion step.
- Guarantees zero hallucinations by keeping LAYA strictly out of answer prose generation.
- Preserves predictable query latency and strictly enforces the single answer generation invariant.

**Negative / tradeoffs**:
- Slightly increases pipeline graph complexity by adding an escalation branch.
- Queries requiring more than one expansion step to resolve complex multi document research chains will terminate with partial answers or refusals.

**Neutral**:
- Reuses existing vector, lexical, structural, and tabular storage interfaces without schema alterations or database migrations.
- `LAYA_ENABLED=false` can serve as default until locally verified, preserving full operational safety.

## References

**Project sources**:
- `KRE_ARCHITECTURE_BIBLE_REFACTOR_READY_2026-10-06.md`: Authoritative architecture specification (Section 17 Query Model Budgets, Section 34 Bounded Escalation, Section 35 Routing Policy, Section 36 Decision State Machine).
- `docs/reference/ADR/ADR-001-baseline-publication-independent-of-enrichment.md`: Baseline independent operational resilience.
- `docs/reference/ADR/ADR-004-model-budget-ledger.md`: Query model budget counters and invocation ceilings.
- `docs/scope/scope.md`: Roadmap milestone Feature 8 (Slice 5 LAYA bounded escalation).
- `docs/specs/0007-knowledge-graph-enrichment.md`: Accepted specification for bounded enrichment and evidence contracts.

**Practices & standards**:
- Single pass answer generation constraint for low latency RAG systems.
- Fail closed security boundaries for multi tenant retrieval engines.
- Separation of routing decision models from generative synthesis models.
