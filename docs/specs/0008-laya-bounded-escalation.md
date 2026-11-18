# 0008. Bounded LAYA Escalation and Query Expansion

**Date**: 2026-11-18
**Status**: Proposed

## Summary

This specification defines bounded LAYA escalation for the query execution pipeline. When initial baseline discovery fails to retrieve sufficient supporting evidence, leaves request coverage incomplete, or encounters missing calculation operands, LAYA activates as a bounded decision model. It never writes user facing answer prose. Instead, it inspects a structured description of missing requirements and proposes one targeted expansion route, such as lexical, dense, structural, graph, or tabular execution. Code strictly validates the proposed route against active permissions, readiness, and budget limits before executing. The pipeline preserves baseline evidence, enforces authoritative resource budgets, guarantees at most one answer synthesis call per query while preserving the final verification reserve, and returns a structured refusal if evidence remains unsupported.

## Context

The Knowledge Retrieval Engine currently executes query processing through a compiled LangGraph workflow. That workflow runs hybrid baseline discovery (BM25 lexical search, vector similarity search, PageIndex layout navigation, and Open Knowledge Framework property lookups), followed by optional Knowledge Graph traversal, reciprocal rank fusion reranking, and deterministic tabular math. If deterministic math does not answer the question, the pipeline passes fused candidates to context compression, evidence fidelity verification, and answer synthesis.

However, when initial discovery retrieves weak, off topic, or incomplete evidence, `run_fidelity` raises a coverage error or detects empty context. Today, that error immediately terminates query processing with `final_answer="NOT_FOUND"`. In many realistic questions, the missing evidence exists in the corpus under synonymous wording, a specific document section, or an unlinked table cell, but discovery missed it because the original query phrasing was ambiguous or vocabulary mismatched. Similarly, queries requiring tabular operations can fail when required column or row operands are not retrieved during initial discovery.

Unconstrained multi turn agent loops and open ended search again loops introduce severe operational risks:
1. Unpredictable latency: autonomous agent loops can loop indefinitely, exhausting system thread pools and violating caller timeouts.
2. Uncontrolled model costs: generating repeated intermediate reasoning traces and speculative answers multiplies billable token expenditures.
3. Hallucination amplification: models that re query repeatedly on low confidence contexts often drift into fabricated claims or speculative answers.
4. Loss of baseline evidence: speculative re querying can discard or displace true factual baseline chunks found in initial passes.

Architecture Decision Record [ADR-004-model-budget-ledger.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reference/ADR/ADR-004-model-budget-ledger.md) and Architecture Bible sections 17, 34, 35, and 36 establish the governing principles for bounded escalation. Escalation must be bounded by explicit attempt ceilings, hard deadlines, candidate caps, and no progress detection. LAYA must function strictly as a decision model that proposes a missing requirement route, never generating user answer prose. Code must validate all proposed routes before dispatch, and answer synthesis must execute at most once per query while respecting the global model budget ledger and final verification reserve.

## Requirements

**User stories**:
- As a retrieval engineer, I want queries with vocabulary mismatch or missing section context to trigger one targeted expansion so that factual answers are retrieved without infinite search loops.
- As a system operator, I want escalation to enforce strict call, token, and deadline budgets so that complex queries never overrun latency targets or deplete worker capacity.
- As an enterprise tenant, I want escalation decisions and expanded retrievals to remain strictly isolated within my authorized workspace and active document snapshots, honoring deletion tombstones fail closed.
- As an API consumer, I want queries that remain unsupported after escalation to produce an explicit, structured refusal with zero citations, rather than speculative or unverified hallucinations.

**Acceptance criteria**:
- **AC-1**: Bounded escalation triggers if and only if initial baseline discovery lacks sufficient evidence support (such as coverage error in fidelity verification, empty compressed context, or missing required tabular operands) and the query budget retains an unused expansion slot (`expansion_steps == 0` and `laya_calls < MAX_QUERY_LAYA_CALLS`). Escalation does not activate when baseline evidence is sufficient, when deterministic math answered the query, or when remaining deadline is below the minimum viable threshold.
- **AC-2**: LAYA operates strictly as a typed decision model and never emits user facing answer prose. It accepts a typed `LayaEscalationRequest` containing query identity, authorized workspace identity, a structured `MissingRequirementDescription`, and summarized baseline evidence locators. It returns a typed `LayaEscalationDecision` specifying one chosen route (`lexical_expansion`, `dense_expansion`, `structural_expansion`, `graph_expansion`, `structured_expansion`, or `none`) alongside bounded route parameters and an explicit numeric decision confidence. Missing, null, or out of bounds confidence is rejected, and routing confidence is never used to certify evidence sufficiency.
- **AC-3**: Code validates the proposed decision before execution through a deterministic state machine. If LAYA proposes an unauthorized route, an inactive feature (such as `graph_expansion` while `GRAPH_ENABLED=false`), an unknown document, or malformed parameters, or if LAYA times out or encounters a provider error, code routes to the deterministic missing requirement policy defined in Architecture Bible section 35. If that fallback route is also unavailable, code transitions to clean refusal. All fallback and alternative paths share the single expansion allowance.
- **AC-4**: Authoritative resource budgets govern escalation under ADR-004. ADR-004 establishes the global policy limit `MAX_QUERY_LLM_CALLS = 2` across the query lifecycle (reserving 1 synthesis call and 1 final semantic verifier call for free form narrative generation). Slice 5 enforces tighter slice specific limits without redefining global policy: exactly one answer synthesis call (`MAX_SYNTHESIS_CALLS = 1`), at most 1 LAYA decision call per query (`MAX_QUERY_LAYA_CALLS = 1` for escalation), at most 1 targeted expansion step (`MAX_EXPANSION_STEPS = 1`), at most 1000 input prompt tokens, at most 250 output decision tokens, and a phase deadline derived as `min(query_remaining_ms, 2500.0)`. Pre reservation atomically checks slots and token estimates before dispatch, 429 throttling retries are charged as attempts, and worker concurrency is capped at 4 with capacity retention.
- **AC-5**: Workspace, document version, and tombstone isolation are enforced across escalation. LAYA receives only metadata and evidence bound to the active workspace and pinned snapshot context. Any expanded retrieval executes strictly within the authorized workspace. Active source or workspace tombstones in `WorkspaceBoundaryRepository` are checked dynamically before and after expansion, returning zero candidates immediately fail closed on tombstone detection.
- **AC-6**: Exactly one answer synthesis call is permitted per query. Answer synthesis (`run_llm`) executes only after all retrieval and verification stages have settled. LAYA decision tokens do not count toward or consume answer generation slots. If evidence remains unsupported after expansion, the pipeline bypasses answer synthesis entirely, returning a deterministic refusal with zero generation calls.
- **AC-7**: Expanded retrieval candidates pool into evidence collection alongside baseline candidates. For narrative routes, candidates are reranked (consuming the second rerank batch allowed under ADR-004) and recompressed. Structured expansion executes outside reciprocal rank fusion directly through TableStore into mandatory execution evidence. Stale refusal state is cleared before rechecking support. If secondary verification succeeds, answer synthesis proceeds; if verification fails, the engine emits a structured refusal (`final_answer="NOT_FOUND"`, `citations=[]`, `confidence_score=0.0`, `retrieval_incomplete=True`). Telemetry records execution details in `QueryResponse.retrieval_candidates`.
- **AC-8**: Comprehensive offline ablation test harness verifies escalation behavior without paid provider calls. Offline tests use mocked LAYA decision doubles, verifying successful expansion, fallback on provider failure, code rejection of invalid routes, tenant isolation, tombstone halting, no progress suppression, and strict enforcement of the single answer synthesis invariant. Slice 5.1 implements one thin executable vertical path through lexical expansion with unsupported routes explicitly unavailable.

## Options considered

### Option 1: Bounded Typed LAYA Decision Node with Code Validated Targeted Expansion (Recommended)

Integrate LAYA as a specialized decision node in the LangGraph query pipeline between initial evidence verification and final synthesis. LAYA accepts a structured missing requirement description and outputs a strictly typed JSON routing decision. Code validates the route through a deterministic state machine and dispatches a single targeted retrieval expansion across lexical, dense, structural, graph, or tabular adapters. Expanded evidence rejoins the candidate pool and undergoes secondary verification before single pass answer synthesis.

**Pros**:
- Strictly bounds execution to at most one decision call and at most one expansion step, preventing latency runaway.
- Decouples routing intelligence from answer generation, ensuring LAYA never writes answer prose.
- Reuses existing BM25, vector, PageIndex, OKF, and TableStore retrieval adapters without building new search engines.
- Code validation acts as an authoritative verification gate, rejecting unauthorized or disabled features.
- Preserves baseline discovery chunks when expansion fails or produces zero additional evidence.
- Guarantees at most one answer synthesis call per query while preserving the global verification reserve under ADR-004.

**Cons**:
- Does not solve highly exploratory multi question research tasks requiring three or more iterative search hops (accepted, as multi hop exploratory research belongs to offline agent workflows, not low latency query serving).

### Option 2: Autonomous ReAct Agent Loop with Dynamic Repeated Querying

Deploy an autonomous agent loop (such as LangChain ReAct or LangGraph multi agent supervisor) that grants the language model direct tool calling access to search APIs, allowing it to inspect partial results, reformulate queries, and iterate until satisfied.

**Pros**:
- Flexible for complex open ended research tasks.

**Cons**:
- Unpredictable latency and high variance, frequently exceeding strict interactive query deadlines.
- High token cost caused by repeated verbose reasoning traces.
- Prone to infinite tool loops and ungrounded arguments when source evidence is absent.
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

We will implement LAYA as a bounded, code validated decision node that triggers only when baseline discovery lacks required evidence support or structured operands. It evaluates a structured missing requirement description, proposes one targeted expansion route, and relies on code to validate and execute the expansion within strict budget and isolation boundaries.

## Rationale

Option 1 directly satisfies Architecture Bible sections 17, 34, 35, and 36 while honoring Architecture Decision Records [ADR-001-baseline-publication-independent-of-enrichment.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reference/ADR/ADR-001-baseline-publication-independent-of-enrichment.md) and [ADR-004-model-budget-ledger.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reference/ADR/ADR-004-model-budget-ledger.md).

By restricting LAYA to a decision model that returns typed JSON choices rather than answer prose, we bound model behavior to structured routing choices and require mechanical and semantic verification of evidence before any factual claim is emitted. By placing code as an authoritative verification gate between the model's proposal and retrieval execution, the system prevents unauthorized cross tenant access, respects feature flags (such as disabled Knowledge Graph), and enforces hard resource ceilings.

Furthermore, gating escalation on verified evidence insufficiency ensures that the fast path and clean baseline discovery queries incur zero LAYA latency and zero extra token expense. Restricting escalation to exactly one expansion step guarantees that interactive query latency remains bounded within predictable service level agreement limits.

## Feature design

### Typed Contracts

The contracts are defined as frozen Pydantic models with strict extra field forbidding. All generic Any fields are replaced with bounded, typed models carrying authoritative document and version bindings:

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, ConfigDict, Field


class MissingRequirementDescription(BaseModel):
    """Structured characterization of missing evidence causing escalation."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: str = Field(..., max_length=64)
    missing_dimension: Literal[
        "location",
        "exact_term",
        "semantic_concept",
        "relationship",
        "tabular_operand",
    ]
    description: str = Field(..., max_length=300)
    failed_stage: Literal[
        "run_fidelity_coverage",
        "run_fidelity_empty_context",
        "run_deterministic_math_operands",
    ]
    detected_gap: str = Field(..., max_length=300)


class BaselineEvidenceSummary(BaseModel):
    """Authoritative summary of evidence retrieved during baseline discovery."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=64)
    document_version: int = Field(..., ge=1)
    workspace_id: str = Field(..., max_length=64)
    snippet: str = Field(..., max_length=300)
    score: float = Field(..., ge=0.0)
    source_kind: Literal["text", "pdf_page", "table_cell", "graph_node"]


class LexicalExpansionParams(BaseModel):
    """Parameters for targeted BM25 keyword expansion."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["lexical_expansion"] = "lexical_expansion"

    query_terms: tuple[str, ...] = Field(..., min_length=1, max_length=10)
    document_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=5)


class DenseExpansionParams(BaseModel):
    """Parameters for targeted vector similarity expansion."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["dense_expansion"] = "dense_expansion"

    concept_query: str = Field(..., min_length=3, max_length=500)
    document_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=5)


class StructuralExpansionParams(BaseModel):
    """Parameters for targeted PageIndex section or page navigation."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["structural_expansion"] = "structural_expansion"

    document_id: str = Field(..., max_length=64)
    section_heading: str | None = Field(default=None, max_length=128)
    target_pages: tuple[int, ...] = Field(default_factory=tuple, max_length=5)


class GraphExpansionParams(BaseModel):
    """Parameters for bounded knowledge graph multi hop traversal."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["graph_expansion"] = "graph_expansion"

    seed_entity_id: str = Field(..., max_length=64)
    relation_types: tuple[str, ...] = Field(default_factory=tuple, max_length=5)
    max_hops: int = Field(default=1, ge=1, le=2)


class StructuredExpansionParams(BaseModel):
    """Parameters for TableStore column or row operand lookups."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["structured_expansion"] = "structured_expansion"

    table_name: str = Field(..., max_length=64)
    required_columns: tuple[str, ...] = Field(..., min_length=1, max_length=10)
    filter_predicates: tuple[str, ...] = Field(default_factory=tuple, max_length=5)


class NoneExpansionParams(BaseModel):
    """Parameters when LAYA decides no expansion should occur."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["none"] = "none"

    reason: str = Field(default="", max_length=200)


LayaRouteParameters = Annotated[
    Union[
        LexicalExpansionParams,
        DenseExpansionParams,
        StructuralExpansionParams,
        GraphExpansionParams,
        StructuredExpansionParams,
        NoneExpansionParams,
    ],
    Field(discriminator="route_type"),
]


class LayaEscalationRequest(BaseModel):
    """Input payload provided to the LAYA decision model."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str = Field(..., max_length=64)
    workspace_id: str = Field(..., max_length=64)
    query: str = Field(..., max_length=1000)
    missing_requirement: MissingRequirementDescription
    available_routes: tuple[str, ...] = Field(default_factory=tuple)
    retrieved_summary: tuple[BaselineEvidenceSummary, ...] = Field(default_factory=tuple)
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
    parameters: LayaRouteParameters
    confidence: float = Field(..., ge=0.0, le=1.0)
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

### Confidence Semantics

In `LayaEscalationDecision`, the `confidence` field is mandatory and has no default value. The caller must provide an explicit float between 0.0 and 1.0. If the field is missing or invalid, schema validation fails, and the pipeline routes immediately to the deterministic fallback policy.

Per Architecture Bible sections 36 and 2361, routing confidence indicates only the model's self reported confidence in its route choice. It does NOT certify evidence sufficiency. Evidence sufficiency is determined exclusively by mechanical and semantic support verification against retrieved text and table records.

### State Machine and Pipeline Topology

In `langgraph_pipeline.py`, LAYA is integrated via conditional routing after initial evidence evaluation:

```
[route_query]
      |
[baseline discovery: BM25 / PageIndex / Vector / OKF / Graph]
      |
[run_reranker (initial RRF pool)]
      |
[run_deterministic_math] ----(answered)----> [END]
      |
      +---> (missing operands) ---> [prepare_missing_operand_requirement] ---> [run_laya_escalation]
      |
  (unanswered, proceed to text generation path)
      |
[run_compressor]
      |
[run_fidelity (evidence check)]
      |
      +---> (evidence sufficient) ------------> [run_llm (single synthesis)] ---> [END]
      |
      +---> (coverage error or empty context)
                 |
                 +---> (expansion budget exhausted or deadline expired) ---> [refusal_exit] ---> [END]
                 |
                 +---> (budget and deadline available) ---> [run_laya_escalation]
```

Inside `run_laya_escalation`, the state machine follows Architecture Bible section 36:

```
[MissingRequirementKnown]
           |
   (atomic pre reservation)
           |
    +------+-------------------------------------------------+
    |                                                        |
(reservation succeeded)                             (reservation failed)
    |                                                        |
[DecisionRequested]                                     [StopOutcome] (clean refusal)
    |
    +---> (valid JSON decision returned) ---> [CodeValidation]
    |                                               |
    +---> (timeout, error, or malformed)            +---> (route authorized and ready) ---> [SelectedExpansion]
                 |                                  |
                 v                                  +---> (route disabled or invalid)   ---> [BaselineAlternative]
          [DefaultPolicy]                                                                       |
                 |                                                                              +---> (ready baseline exists)   ---> [SelectedExpansion]
                 +------------------------> [CodeValidation]                                   +---> (no alternative possible) ---> [StopOutcome]
```

### Post Expansion State Transitions

When `SelectedExpansion` executes and finishes:
1. **Candidate Merging**:
   - For narrative routes (`lexical_expansion`, `dense_expansion`, `structural_expansion`, `graph_expansion`), retrieved chunks are appended to the existing candidate list.
   - For `structured_expansion`, retrieved records execute directly through the approved TableStore executor interface into mandatory execution evidence. They are **NOT** passed through reciprocal rank fusion. If the calculation can now be computed deterministically, it returns the final answer directly and terminates at `END` without invoking `run_llm`.
2. **Reranking and Recompression**:
   - Narrative candidates pass to `run_reranker`, consuming the second allowed rerank batch under ADR-004 (`MAX_QUERY_RERANK_CALLS = 2`).
   - The top reranked chunks pass to `run_compressor` to construct an updated `compressed_text`.
3. **Clearing Stale Refusal State**:
   - Stale refusal attributes recorded during the initial failure (`state["error"] = None`, `state["final_answer"] = None`, `state["citations"] = []`) are cleared before rechecking support.
4. **Secondary Support Verification**:
   - The pipeline invokes `run_fidelity` on the updated context.
   - If fidelity succeeds (`error is None`), the pipeline transitions to `run_llm` for single answer synthesis.
   - If fidelity fails (`error` raised again) or if expansion produced no progress, the pipeline transitions to clean refusal (`final_answer = "NOT_FOUND"`, `citations = []`, `confidence_score = 0.0`, `retrieval_incomplete = True`), bypassing `run_llm` completely.

### Resource Budget Accounting and Invocations

Escalation adheres strictly to the budget governance model established in ADR-004:

1. **Global Model Ledger Policy**:
   - Global query policy `query-default-v3` in ADR-004 establishes `MAX_QUERY_LLM_CALLS = 2` total generation calls across the query lifecycle.
   - In free form narrative generation, the scheduler must reserve capacity for **1 synthesis call + 1 final semantic verifier call** before dispatching optional generative planning or speculative calls.
2. **Slice 5 Specific Limits**:
   - Slice 5 enforces tighter slice specific limits without redefining global policy.
   - Exactly one answer synthesis call (`run_llm`) is permitted (`MAX_SYNTHESIS_CALLS = 1`). LAYA decision calls consume 0 generation slots. This guarantees that the second generation slot remains preserved as a final verification reserve when configured.
   - If evidence remains unsupported following escalation, answer synthesis is bypassed entirely, consuming 0 generation calls.
3. **Atomic Pre Reservation and Settlement**:
   - Before invoking LAYA, the pipeline atomically checks and decrements available capacity: 1 decision call (`laya_calls += 1` against `MAX_QUERY_LAYA_CALLS = 1` for escalation), estimated 1000 input tokens, estimated 250 output tokens, and 1 expansion step (`expansion_steps += 1` against `MAX_EXPANSION_STEPS = 1`).
   - If any counter is already exhausted, escalation terminates immediately to refusal.
   - After invocation, actual billable tokens, latency, and status are settled into the ledger.
4. **429 Throttling Attempt Charging**:
   - Provider retries are counted and charged against query budgets.
   - If an HTTP 429 rate limit is encountered, the attempt is charged against `MAX_TOOL_ATTEMPTS = 3`.
   - At most 1 retry is permitted for HTTP 429 if the remaining phase deadline permits. If the retry fails or times out, execution transitions to the deterministic fallback policy.
5. **Authoritative Token Preflight**:
   - Before making a network call, prompt text is evaluated using local tokenizer `count_tokens`.
   - If input exceeds 1000 tokens, baseline evidence snippets are truncated deterministically to fit within 1000 tokens. If compliant prompt length cannot be achieved, the call is rejected before any billable provider call is made.
6. **Dynamic Deadline Calculation**:
   - The escalation phase deadline is calculated as `phase_deadline_ms = min(query_remaining_ms, 2500.0)`.
   - If `phase_deadline_ms < 500.0` ms, escalation is aborted immediately, failing closed to refusal without calling the provider.
7. **Admission Queue and Worker Capacity Retention**:
   - An in process admission queue caps active concurrent LAYA escalation workers at 4, with a bounded wait queue of 8 slots.
   - When the admission queue is full, incoming escalation requests do not block worker threads; they shed immediately, retaining core engine capacity, and fall back to the deterministic section 35 policy or clean refusal.

### Value sourcing

| Action | Value produced / displayed | Source |
|---|---|---|
| Trigger escalation | Escalation trigger decision | Derived in `run_fidelity` or `run_deterministic_math` when evidence is missing and `expansion_steps < 1` |
| Formulate missing requirement | `MissingRequirementDescription` | Derived from fidelity verification gap analysis or missing structured column operands |
| Decide expansion route | `LayaEscalationDecision` | Computed by LAYA model or fallback deterministic missing requirement policy |
| Validate route | Route approval / rejection | Computed by code validation against active settings, workspace permissions, and tombstones |
| Execute expansion | Expanded candidate chunks | Retrieved from existing BM25, Vector, PageIndex, OKF, or TableStore adapters |
| Answer synthesis | Final synthesis text | Synthesized in `run_llm` if and only if secondary evidence verification passes |
| Answer refusal | `final_answer = "NOT_FOUND"` | Emitted deterministically with empty citations when evidence remains insufficient |
| Telemetry logging | `laya_*` debug trace | Recorded in `QueryResponse.retrieval_candidates` from LAYA execution timings and tokens |

### Key invariants

1. **Strict Decision Boundary**: LAYA returns only routing decisions; it never generates answer text or user facing responses.
2. **At Most One Answer Synthesis**: Exactly one answer synthesis call can execute per query (`MAX_SYNTHESIS_CALLS = 1`). This respects ADR-004 by keeping total generation calls within `MAX_QUERY_LLM_CALLS = 2` while preserving the final semantic verification reserve.
3. **Shared Single Expansion Allowance**: `MAX_EXPANSION_STEPS = 1`. All escalation paths (whether LAYA proposed or deterministic fallback, whether first choice or baseline alternative) share this single allowance. Escalation never chains into secondary or tertiary expansion loops.
4. **Code Validation Primacy**: Model proposed routes are untrusted suggestions. Code validates permissions, feature flags, version bindings, and budgets before executing any expansion.
5. **Fail Closed Baseline Preservation**: Any error, timeout, or invalid proposal during escalation leaves baseline evidence intact and fails closed without throwing unhandled exceptions.
6. **No Progress Suppression**: If an expansion retrieves chunks that are already present in the candidate pool, escalation terminates immediately without further attempts.
7. **Structured Execution Independence**: Structured expansions execute directly through the approved TableStore executor outside reciprocal rank fusion, rejoining mandatory execution evidence.

### Security and Isolation Model

- Tenant boundary: All LAYA invocations pass explicit `workspace_id`. Prompt context contains only documents authorized in the current workspace.
- Version binding: Expansion lookups target only active document versions pinned in `PinnedSnapshotContext`.
- Dynamic tombstone check: `WorkspaceBoundaryRepository.has_tombstone` is re verified before dispatching any expanded retrieval. If a source was deleted during query execution, the expansion returns zero candidates immediately fail closed.

### Critical test scenarios

- **Scenario 1 (Happy Path Lexical Expansion)**: Query with synonymous terminology fails initial discovery. LAYA analyzes the missing term, proposes `lexical_expansion` with targeted keywords. BM25 retrieves the missing chunk. Secondary verification succeeds. Single answer generation produces verified answer with exact citation. (AC-1, AC-2, AC-6, AC-7)
- **Scenario 2 (Happy Path Dense Expansion)**: Query with abstract semantic gap fails initial discovery. LAYA proposes `dense_expansion` with reformulated concept query. Vector store retrieves supporting chunk. Verification succeeds and answer is synthesized. (AC-2, AC-3)
- **Scenario 3 (Code Rejection of Disabled Route and Deterministic Fallback)**: LAYA proposes `graph_expansion` while `GRAPH_ENABLED=false`. Code validation rejects the proposal, falls back to deterministic section 35 lexical expansion, consumes the single expansion step, and succeeds or terminates cleanly without graph access. Baseline retrieval is preserved. (AC-3, AC-5)
- **Scenario 4 (Provider Timeout and Deterministic Policy)**: LAYA provider call times out. Pipeline catches the timeout fail closed, executes the deterministic section 35 route within remaining deadline, or emits clean refusal if time is exhausted. (AC-3, AC-4)
- **Scenario 5 (HTTP 429 Attempt Charging and Termination)**: LAYA encounters an HTTP 429 response. Attempt is charged to budget. Single allowed retry succeeds within deadline, or fails and falls back to deterministic policy without budget leak. (AC-4)
- **Scenario 6 (Token Preflight Truncation)**: Request with oversized evidence summary is truncated deterministically by token preflight to stay within 1000 input tokens before billable provider invocation. (AC-4)
- **Scenario 7 (Deadline Bounding and Early Abort)**: Query has only 300 ms remaining when fidelity fails. Escalation calculates `phase_deadline_ms = 300 < 500` ms, aborts immediately without calling LAYA, and emits clean refusal. (AC-1, AC-4)
- **Scenario 8 (Missing Structured Operands Entry and Non RRF Execution)**: Structured math query misses required columns. Entry path routes to LAYA, proposing `structured_expansion`. TableStore fetches columns directly outside reciprocal rank fusion, math evaluates deterministically to final answer, and `run_llm` is not invoked. (AC-1, AC-6, AC-7)
- **Scenario 9 (Empty Context Entry Path)**: Discovery retrieves zero chunks (`compressed_text=""`). Fidelity detects empty context and triggers escalation rather than immediate failure. Lexical expansion finds chunks, secondary fidelity passes, and single synthesis executes. (AC-1, AC-7)
- **Scenario 10 (Invalid Confidence Rejection)**: LAYA returns decision with missing confidence or confidence equal to 1.5. Code validation rejects decision, executes deterministic fallback, and asserts routing confidence is not treated as evidence sufficiency. (AC-2, AC-3)
- **Scenario 11 (Tombstoned Source Rejection)**: LAYA proposes structural expansion on a document that has an active deletion tombstone. Code detects tombstone, suppresses expansion, and returns zero additional candidates. (AC-5)
- **Scenario 12 (No Progress Refusal)**: LAYA expansion returns chunks identical to those already retrieved. No progress detector halts escalation. Pipeline refuses cleanly with `NOT_FOUND` and zero answer generation calls. (AC-6, AC-7)

## Build plan (Tracer Bullet Approach)

The build plan follows a vertical Tracer Bullet slice, implementing the thinnest end to end path through the layers first:

### Slice 5.1: Thin Executable Path (Lexical Expansion and Safety Gate)
1. **Contracts and Error Envelopes**: Implement frozen Pydantic models `MissingRequirementDescription`, `BaselineEvidenceSummary`, `LayaEscalationRequest`, `LayaEscalationDecision`, and typed route parameter models in `backend/src/schemas/contracts/laya.py`. Satisfies **AC-2**.
2. **Deterministic Missing Requirement Policy**: Implement Architecture Bible section 35 deterministic fallback rules in `backend/src/services/retrieval/laya_policy.py`, mapping missing dimensions to approved expansion routes. Satisfies **AC-3**.
3. **Pre Reservation and Preflight Gate**: Implement pre reservation checks, token preflight estimation, and deadline calculation `min(query_remaining_ms, 2500.0)` in `backend/src/services/retrieval/laya_gate.py`. Satisfies **AC-4**.
4. **LAYA Decision Provider Boundary**: Implement `LayaDecisionService` in `backend/src/services/retrieval/laya_service.py` with Bedrock Converse structured output parsing and offline test double injection. In Slice 5.1, unsupported routes (`structural_expansion`, `dense_expansion`, `graph_expansion`, `structured_expansion`) are explicitly registered as unavailable, causing code validation to reject them or fall back. Satisfies **AC-2**, **AC-3**, **AC-4**.
5. **Targeted Lexical Expansion Handler**: Wire targeted single pass BM25 retrieval handler in `LayaExpansionExecutor`. Satisfies **AC-1**, **AC-5**.
6. **LangGraph Pipeline Integration**: Wire `run_laya_escalation` node into `backend/src/services/langgraph_pipeline.py` between `run_fidelity` and `run_llm`, clearing stale refusal state upon expansion, reranking, recompressing, and enforcing the single synthesis call limit. Satisfies **AC-1**, **AC-6**, **AC-7**.
7. **Offline Slice 5.1 Test Suite**: Implement offline regression suite in `backend/tests/test_laya_escalation_slice5_1.py` exercising lexical expansion, code rejection of unavailable routes, deterministic fallback, token preflight, and single synthesis limit with mocked providers. Satisfies **AC-8**.

### Slice 5.2: Bounded Multi Route Thickening and Concurrency
1. **Remaining Expansion Handlers**: Wire targeted handlers for `dense_expansion` (vector search), `structural_expansion` (PageIndex), `structured_expansion` (TableStore non RRF execution), and optional `graph_expansion` when `GRAPH_ENABLED=true`.
2. **Admission Queue Concurrency Guard**: Implement 4 worker admission queue with bounded wait slots and capacity retention in `LayaAdmissionQueue`.
3. **Structured Math Entry Wiring**: Wire missing operand detection in `run_deterministic_math` to trigger escalation into `structured_expansion`.
4. **Comprehensive Offline Ablation Suite**: Implement `backend/tests/test_laya_escalation_slice5_2.py` covering multi route dispatch, admission queue shedding, 429 retry charging, and full telemetry reporting.

## Consequences

**Positive**:
- Solves factual answer drops caused by simple vocabulary mismatches or missing section locators.
- Eliminates the risk of runaway agent loops by bounding escalation to exactly one decision call and one expansion step.
- Prevents LAYA from inventing answer claims by restricting it strictly to routing decisions and keeping it out of answer prose generation.
- Preserves predictable query latency and strictly enforces the single answer synthesis invariant while respecting global model budget policy and verification reserves.

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
