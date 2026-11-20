# 0008. Bounded LAYA Escalation and Query Expansion

**Date**: 2026-11-18
**Status**: In Progress

## Summary

This specification defines bounded LAYA escalation for the query execution pipeline. When initial baseline discovery fails to retrieve sufficient supporting evidence, leaves request coverage incomplete, or encounters missing calculation operands, LAYA activates as a bounded decision model. It never writes user facing answer prose. Instead, it inspects a structured description of missing requirements and proposes one targeted expansion route, such as lexical, dense, structural, graph, or tabular execution. The decision model executes through a dedicated provider adapter layer where the primary decision engine is the user's Cloudflare-hosted LAYA server (`/v1/systemone`), with Bedrock Claude Haiku maintained as an alternative provider selectable by explicit configuration (`LAYA_DECISION_PROVIDER`). Silent provider fallback is strictly prohibited. Code strictly validates the proposed route against active permissions, readiness, and budget limits before executing. The pipeline preserves baseline evidence, enforces authoritative resource budgets, guarantees at most one answer synthesis call per query while preserving the final verification reserve, and returns a structured refusal if evidence remains unsupported. Furthermore, free form generated text is never emitted without passing an authoritative final answer delivery gate.

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
- As a system operator, I want escalation to enforce strict call, token, admission, and deadline budgets so that complex queries never overrun latency targets or deplete worker capacity.
- As an enterprise tenant, I want escalation decisions and expanded retrievals to remain strictly isolated within my authorized workspace and active document snapshots, honoring opaque document versions and deletion tombstones fail closed.
- As an API consumer, I want queries that remain unsupported after escalation to produce an explicit, structured refusal with zero citations, rather than speculative or unverified hallucinations.

**Acceptance criteria**:
- **AC-1**: Bounded escalation triggers if and only if initial baseline discovery lacks sufficient evidence support (such as coverage error in fidelity verification, empty compressed context, or missing required tabular operands) and the query budget retains an unused expansion slot (`expansion_steps == 0` and `laya_calls < MAX_QUERY_LAYA_CALLS`). Escalation does not activate when baseline evidence is sufficient, when deterministic math answered the query, or when remaining deadline is below the minimum viable threshold.
- **AC-2**: LAYA operates strictly as a typed decision model and never emits user facing answer prose. It accepts a typed `LayaEscalationRequest` containing query identity, authorized workspace identity, a structured `MissingRequirementDescription`, and bounded baseline evidence locators with opaque string versions. It returns a typed `LayaEscalationDecision` specifying one chosen route (`lexical_expansion`, `dense_expansion`, `structural_expansion`, `graph_expansion`, `structured_expansion`, or `none`) alongside bounded route parameters and an explicit numeric decision confidence. The model enforces strict discriminator agreement between `route` and `parameters.route_type`. Missing, null, or out of bounds confidence is rejected, and routing confidence is never used to certify evidence sufficiency.
- **AC-3**: Code validates the proposed decision before execution through a deterministic state machine. The decision provider is chosen explicitly by configuration (`LAYA_DECISION_PROVIDER="cloudflare"`, `"bedrock"`, or `"test"`). When a provider is configured and encounters a connection failure, timeout, HTTP error, or unavailability, the system MUST NOT silently fall back to an alternate provider model. Instead, it records `provider_unavailable` telemetry, releases capacity, and routes directly to the deterministic missing requirement policy defined in Architecture Bible section 35. If LAYA proposes an unauthorized route, an inactive feature (such as `graph_expansion` while `GRAPH_ENABLED=false`), an unknown document, or malformed parameters, code likewise routes to the deterministic section 35 policy. If that fallback route is also unavailable, code transitions to clean refusal. All fallback and alternative paths share the single expansion allowance.
- **AC-4**: Authoritative resource budgets govern escalation under ADR-004. ADR-004 establishes the global policy limit `MAX_QUERY_LLM_CALLS = 2` across the query lifecycle (reserving 1 synthesis call and 1 final semantic verifier call for free form narrative generation). Slice 5 enforces tighter slice specific limits without redefining global policy: exactly one answer synthesis call (`MAX_SYNTHESIS_CALLS = 1`), at most 1 LAYA decision call per query (`MAX_QUERY_LAYA_CALLS = 1` for escalation), at most 1 targeted expansion step (`MAX_EXPANSION_STEPS = 1`), at most 1000 input prompt tokens, at most 250 output decision tokens, and a phase deadline derived as `min(query_remaining_ms, 2500.0)`. Pre reservation atomically checks slots and token estimates before dispatch, 429 throttling retries are charged as attempts, and worker concurrency is capped at 4 with capacity retention in Slice 5.1.
- **AC-5**: Workspace, document version, and tombstone isolation are enforced across escalation. LAYA receives only metadata and evidence bound to the active workspace and pinned snapshot context. Document identifiers are strings up to 128 characters, and document versions are opaque strings up to 64 characters, matching [boundary.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/boundary.py) and [knowledge_graph.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/knowledge_graph.py). Any expanded retrieval executes strictly within the authorized workspace. Active source or workspace tombstones in `WorkspaceBoundaryRepository` are checked dynamically before and after expansion, returning zero candidates immediately fail closed on tombstone detection.
- **AC-6**: Exactly one answer synthesis call is permitted per query. Answer synthesis (`run_llm`) executes only after all retrieval and verification stages have settled. LAYA decision tokens do not count toward or consume answer generation slots. If evidence remains unsupported after expansion, the pipeline bypasses answer synthesis entirely, returning a deterministic refusal with zero generation calls. Furthermore, free form generated wording cannot be delivered without passing the authoritative final answer delivery gate. If the semantic verifier is unconfigured or unavailable, free form delivery is explicitly blocked, falling back strictly to approved deterministic output or explicit refusal.
- **AC-7**: Expanded retrieval candidates pool into evidence collection alongside baseline candidates. For narrative routes, candidates are reranked (consuming the second rerank batch allowed under ADR-004) and recompressed. Structured expansion executes outside reciprocal rank fusion directly through TableStore into mandatory execution evidence. Stale refusal state is cleared before rechecking support. If secondary verification succeeds, answer synthesis proceeds; if verification fails, the engine emits a structured refusal (`final_answer="NOT_FOUND"`, `citations=[]`, `confidence_score=0.0`, `retrieval_incomplete=True`). Telemetry records execution details in `QueryResponse.retrieval_candidates`, explicitly distinguishing four operational states: `enabled` (`laya_enabled`), `invoked` (`laya_invoked`), `provider_success` (`laya_provider_success`), and `fallback` (`laya_fallback`), alongside `laya_status` and `laya_provider`.
- **AC-8**: Comprehensive offline ablation test harness verifies escalation behavior without paid provider calls. Offline tests use mocked LAYA decision doubles, verifying successful expansion, fallback on provider failure, code rejection of invalid routes, tenant isolation, tombstone halting, no progress suppression, queue saturation, caller timeout with capacity retention, and strict enforcement of the single answer synthesis invariant. A dedicated offline adapter suite tests the Cloudflare provider adapter using mocked HTTP responses without network calls, verifying schema translation, error handling, telemetry marking, and no silent Bedrock fallback. A separate, future live smoke check script verifies real Cloudflare endpoint connectivity strictly on demand with live credentials, separated completely from automated test runs. Slice 5.1 implements one thin executable vertical path through lexical expansion with unsupported routes explicitly unavailable.

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
- Enforces the final delivery gate so that unverified free form text is never emitted.

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

The contracts are defined as frozen Pydantic models with strict extra field forbidding. All generic Any fields are replaced with bounded, typed models carrying authoritative document, version, and location bindings:

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MissingRequirementDescription(BaseModel):
    """Structured characterization of missing evidence causing escalation."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: str = Field(..., min_length=1, max_length=64)
    missing_dimension: Literal[
        "location",
        "exact_term",
        "semantic_concept",
        "relationship",
        "tabular_operand",
    ]
    description: str = Field(..., min_length=1, max_length=300)
    failed_stage: Literal[
        "run_fidelity_coverage",
        "run_fidelity_empty_context",
        "run_deterministic_math_operands",
    ]
    detected_gap: str = Field(..., min_length=1, max_length=300)


class BaselineEvidenceSummary(BaseModel):
    """Authoritative summary of evidence retrieved during baseline discovery.
    
    Document identifiers are strings up to 128 characters.
    Document versions are opaque strings up to 64 characters, never integers.
    Coordinates enforce 1 indexed pages and normalized bounding boxes.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(..., min_length=1, max_length=64)
    document_id: str = Field(..., min_length=1, max_length=128)
    document_version: str = Field(..., min_length=1, max_length=64)
    workspace_id: str = Field(..., min_length=1, max_length=64, pattern=r"^ws_[a-zA-Z0-9_-]+$")
    snippet: str = Field(..., min_length=1, max_length=300)
    score: float = Field(..., ge=0.0, le=100.0)
    source_kind: Literal["text", "pdf_page", "table_cell", "graph_node"]
    page_number: int | None = Field(default=None, ge=1, le=10000)
    bbox: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def validate_normalized_bbox(self) -> "BaselineEvidenceSummary":
        if self.bbox is not None:
            if len(self.bbox) != 4:
                raise ValueError("bbox must contain exactly 4 coordinates")
            x0, y0, x1, y1 = self.bbox
            for coord in (x0, y0, x1, y1):
                if coord < 0.0 or coord > 1.0:
                    raise ValueError("bbox coordinates must be between 0.0 and 1.0")
            if x0 >= x1 or y0 >= y1:
                raise ValueError("bbox coordinates must satisfy x0 < x1 and y0 < y1")
        return self


class LexicalExpansionParams(BaseModel):
    """Parameters for targeted BM25 keyword expansion."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["lexical_expansion"] = "lexical_expansion"

    query_terms: tuple[str, ...] = Field(..., min_length=1, max_length=5)
    document_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=5)

    @model_validator(mode="after")
    def validate_item_bounds(self) -> "LexicalExpansionParams":
        for term in self.query_terms:
            if not (1 <= len(term) <= 64):
                raise ValueError("query_terms items must be between 1 and 64 characters")
        for doc_id in self.document_ids:
            if not (1 <= len(doc_id) <= 128):
                raise ValueError("document_ids items must be between 1 and 128 characters")
        return self


class DenseExpansionParams(BaseModel):
    """Parameters for targeted vector similarity expansion."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["dense_expansion"] = "dense_expansion"

    concept_query: str = Field(..., min_length=3, max_length=200)
    document_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=5)

    @model_validator(mode="after")
    def validate_item_bounds(self) -> "DenseExpansionParams":
        for doc_id in self.document_ids:
            if not (1 <= len(doc_id) <= 128):
                raise ValueError("document_ids items must be between 1 and 128 characters")
        return self


class StructuralExpansionParams(BaseModel):
    """Parameters for targeted PageIndex section or page navigation."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["structural_expansion"] = "structural_expansion"

    document_id: str = Field(..., min_length=1, max_length=128)
    section_heading: str | None = Field(default=None, max_length=128)
    target_pages: tuple[int, ...] = Field(default_factory=tuple, max_length=5)

    @model_validator(mode="after")
    def validate_page_bounds(self) -> "StructuralExpansionParams":
        for page in self.target_pages:
            if not (1 <= page <= 10000):
                raise ValueError("target_pages items must be between 1 and 10000")
        return self


class GraphExpansionParams(BaseModel):
    """Parameters for bounded knowledge graph multi hop traversal."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["graph_expansion"] = "graph_expansion"

    seed_entity_id: str = Field(..., min_length=1, max_length=64)
    relation_types: tuple[str, ...] = Field(default_factory=tuple, max_length=5)
    max_hops: int = Field(default=1, ge=1, le=2)

    @model_validator(mode="after")
    def validate_item_bounds(self) -> "GraphExpansionParams":
        for rel in self.relation_types:
            if not (1 <= len(rel) <= 64):
                raise ValueError("relation_types items must be between 1 and 64 characters")
        return self


class StructuredExpansionParams(BaseModel):
    """Parameters for TableStore column or row operand lookups."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    route_type: Literal["structured_expansion"] = "structured_expansion"

    table_name: str = Field(..., min_length=1, max_length=128)
    required_columns: tuple[str, ...] = Field(..., min_length=1, max_length=10)
    filter_predicates: tuple[str, ...] = Field(default_factory=tuple, max_length=5)

    @model_validator(mode="after")
    def validate_item_bounds(self) -> "StructuredExpansionParams":
        for col in self.required_columns:
            if not (1 <= len(col) <= 64):
                raise ValueError("required_columns items must be between 1 and 64 characters")
        for pred in self.filter_predicates:
            if not (1 <= len(pred) <= 128):
                raise ValueError("filter_predicates items must be between 1 and 128 characters")
        return self


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

    query_id: str = Field(..., min_length=1, max_length=64)
    workspace_id: str = Field(..., min_length=1, max_length=64, pattern=r"^ws_[a-zA-Z0-9_-]+$")
    query: str = Field(..., min_length=1, max_length=1000)
    missing_requirement: MissingRequirementDescription
    available_routes: tuple[str, ...] = Field(default_factory=tuple, max_length=6)
    retrieved_summary: tuple[BaselineEvidenceSummary, ...] = Field(default_factory=tuple, max_length=10)
    caller_deadline_ms: float = Field(default=2500.0, ge=0.0)


class LayaEscalationDecision(BaseModel):
    """Typed decision output produced by LAYA and validated by code."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(..., min_length=1, max_length=64)
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
        "provider_unavailable",
    ] = "proposed"

    @model_validator(mode="after")
    def validate_route_parameter_agreement(self) -> "LayaEscalationDecision":
        """Enforce strict agreement between route discriminator and parameters.route_type."""
        if self.route != self.parameters.route_type:
            raise ValueError(
                f"Route discriminator mismatch: route '{self.route}' does not match "
                f"parameters.route_type '{self.parameters.route_type}'"
            )
        return self
```

### Confidence Semantics

In `LayaEscalationDecision`, the `confidence` field is mandatory and has no default value. The caller must provide an explicit float between 0.0 and 1.0. If the field is missing or invalid, schema validation fails, and the pipeline routes immediately to the deterministic fallback policy.

Per Architecture Bible sections 36 and 2361, routing confidence indicates only the model's self reported confidence in its route choice. It does NOT certify evidence sufficiency. Evidence sufficiency is determined exclusively by mechanical and semantic support verification against retrieved text and table records.

### Provider Adapter Architecture and Explicit Selection

The LAYA decision boundary supports multiple backend providers through a dedicated adapter abstraction. The intended primary decision engine is the user's LAYA server exposed through Cloudflare, with Bedrock Claude Haiku (`anthropic.claude-3-haiku-20240307-v1:0`) retained as an alternative provider selectable by explicit configuration.

1. **Explicit Provider Selection**:
   - `LAYA_DECISION_PROVIDER`: `Literal["cloudflare", "bedrock", "test"]`. Default in production configuration is `"cloudflare"`. In test environments, `"test"` provides in-memory deterministic test doubles.
   - Provider selection is mutually exclusive and authoritative. The engine invokes strictly the configured provider.

2. **Strict Invariant: No Silent Bedrock Fallback**:
   - When `LAYA_DECISION_PROVIDER="cloudflare"` is active, if the Cloudflare server is unreachable, connection times out, DNS fails, or returns HTTP errors (4xx or 5xx), the system MUST NOT silently fall back to Bedrock.
   - Silently substituting another model violates operational predictability, introduces unbudgeted cloud expenditures, and masks infrastructure outages.
   - On provider failure, the adapter records `execution_status="provider_unavailable"`, sets `laya_status="provider_unavailable"`, retains or settles ledger spend, and executes the existing bounded Section 35 deterministic fallback (`get_deterministic_fallback_decision`).

3. **Cloudflare Server Interface and Adaptation**:
   - Endpoint: The server is reached at `/v1/systemone` under the configured base URL (such as `https://intent-diagram-garbage-title.trycloudflare.com/v1/systemone` discovered in `backend/tmp/laya_endpoint.py`).
   - Authentication: Injected via header `Authorization: Bearer <API_KEY>` from environment variable `LAYA_CLOUDFLARE_API_KEY`.
   - Request Adaptation: The adapter maps typed `LayaEscalationRequest` into the custom System One JSON format:
     ```json
     {
       "state": {
         "document": "<query and missing requirement context>"
       },
       "questions": {
         "route": {
           "type": "choice",
           "instructions": "Which expansion route should the retrieval engine take?",
           "criteria": {
             "lexical_expansion": "keyword search for missing terms",
             "dense_expansion": "semantic vector search for missing concepts",
             "structural_expansion": "page or section navigation",
             "graph_expansion": "entity graph traversal",
             "structured_expansion": "table or database lookup",
             "none": "no viable expansion possible"
           }
         },
         "complexity": {
           "type": "score",
           "instructions": "How complex is this retrieval gap?",
           "criteria": ["simple", "moderate", "complex"]
         }
       }
     }
     ```
   - Response Parsing: The adapter extracts the selected route from the response JSON, validates agreement with `available_routes`, derives or validates an explicit float confidence between 0.0 and 1.0, and instantiates the matching typed `LayaRouteParameters`.
   - Timeout Bounding: The request enforces `timeout = phase_deadline_ms / 1000.0`, strictly capped by the 2500ms phase allowance.

4. **Surface Missing Server Contract Details**:
   - Exact Model Identity: The endpoint masks the underlying weights or model name behind `/v1/systemone`.
   - Response JSON Schema: The exact key hierarchy returned by `/v1/systemone` is not yet documented in the repository.
   - Usage Accounting: Does `/v1/systemone` emit token usage in response headers or JSON payload, or must KRE compute token estimates exclusively through client-side token bounding?
   - Tunnel Stability: `trycloudflare.com` URLs are ephemeral tunnels. Production deployment requires a persistent Cloudflare Tunnel domain or fixed URL.
   - Error Response Schema: The exact error JSON payload emitted on 4xx/5xx responses is not yet documented.

### Telemetry and Four Explicit Outcome States

To provide complete operational transparency without ambiguity between rerankers, providers, and fallbacks, query execution telemetry in `QueryResponse.retrieval_candidates` must record four explicit boolean outcome flags and supporting attributes:

1. `laya_enabled` (bool): True if LAYA escalation was enabled for the query (`LAYA_ENABLED=True`).
2. `laya_invoked` (bool): True if a decision provider network call was actually dispatched (`ledger.laya_calls >= 1`).
3. `laya_provider_success` (bool): True if the configured provider returned a valid, schema-compliant route and confidence within deadline.
4. `laya_fallback` (bool): True if the pipeline executed the Section 35 deterministic fallback policy (due to provider failure, timeout, queue saturation, code rejection, or unavailable route).

Supporting telemetry attributes recorded alongside these flags:
- `laya_provider`: string identifier of configured provider (`"cloudflare"`, `"bedrock"`, or `"test"`).
- `laya_status`: execution status string (`"provider_success"`, `"provider_unavailable"`, `"timeout"`, `"admission_rejected"`, `"rejected_by_code"`, `"storage_failed"`).
- `laya_latency_ms`: total elapsed time across decision and validation.
- `laya_tokens`: dictionary with `input_tokens` and `output_tokens`.
- `laya_decision_id`: identifier of the active decision.
- `laya_route`: selected route name.
- `laya_confidence`: explicit numeric confidence score.

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
      +---> (evidence sufficient)
      |          |
      |          +---> (verifier unready or disabled) ---> [delivery_fallback_or_refusal] ---> [END]
      |          |
      |          +---> (verifier ready) --------------> [run_llm] ---> [final_answer_delivery_gate] ---> [END]
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
4. **Secondary Support Verification and Final Delivery Gate**:
   - The pipeline invokes `run_fidelity` on the updated context.
   - If fidelity succeeds (`error is None`), the pipeline evaluates the final answer delivery gate:
     - If the required final semantic verifier is configured, the pipeline invokes `run_llm` for single answer synthesis, followed by the verifier check.
     - If the semantic verifier is unconfigured or unavailable, free form delivery is explicitly blocked, and the engine emits approved deterministic excerpts or refusal.
   - If fidelity fails (`error` raised again) or if expansion produced no progress, the pipeline transitions to clean refusal (`final_answer = "NOT_FOUND"`, `citations = []`, `confidence_score = 0.0`, `retrieval_incomplete = True`), bypassing `run_llm` completely.

### Final Answer Delivery Gate (ADR-004)

Under ADR-004, reserving a finishing capacity slot (1 synthesis call + 1 final semantic verifier call) is a necessary budget precondition, but **reserving a slot alone is insufficient to permit delivery of free form generated prose to the caller**.

The final answer delivery gate enforces the following operational rules:
1. **Verifier Interface and Ownership**:
   - The authoritative verification contract is [verification.py](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/schemas/contracts/verification.py) via `VerificationRecord`.
   - Ownership of final verification belongs to Slice 6 (Feature 9, Evidence verification and answer assembly), which runs the final verifier.
   - In Slice 5 (LAYA escalation), the pipeline guarantees at most one answer synthesis call (`MAX_SYNTHESIS_CALLS = 1`), preserving the remaining generation slot for this verifier under the ADR-004 global limit `MAX_QUERY_LLM_CALLS = 2`.
2. **Delivery Blocking Rule**:
   - Before free form synthesized prose can be emitted in `QueryResponse`, the output must pass either:
     (a) The configured final generative semantic verifier (`verifier_type="generative"` consuming the second generation call slot), or
     (b) An evaluated encoder verifier (`verifier_type="encoder"` under `MAX_QUERY_ENCODER_VERIFY_CALLS = 1`), alongside mandatory mechanical citation checks.
   - If the required semantic verifier is unconfigured, disabled, or unavailable in the environment, **free form generated delivery is explicitly blocked**.
   - In that event, the engine falls back strictly to approved deterministic outputs:
     (a) Deterministic verified factual excerpts (`verified_answer` from `extract_verified_fact`),
     (b) Deterministic table calculation templates from TableStore math, or
     (c) Explicit structured refusal (`final_answer="NOT_FOUND"`, `citations=[]`, `retrieval_incomplete=True`).
   - Unverified free form synthesized prose is never delivered to the caller.

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
5. **Authoritative Token Preflight and Usage Handling**:
   - Token preflight is bound to the configured provider adapter.
   - For Bedrock (`LAYA_DECISION_PROVIDER="bedrock"`), preflight invokes supported authoritative counting: `client.count_tokens(modelId=model_id, input={"converse": {"messages": messages, "system": system}})`. If unsupported, the pipeline evaluates a defensible token upper bound or fails closed.
   - For Cloudflare (`LAYA_DECISION_PROVIDER="cloudflare"`), preflight evaluates a defensible token upper bound over the serialized payload (`state` plus `questions`) to verify that prompt tokens do not exceed the 1000 token limit (`estimated_prompt_tokens <= 1000`). If exceeded, baseline evidence snippets are truncated deterministically to fit within 1000 tokens. If compliant prompt length cannot be achieved, the call is rejected before any provider call is made.
   - Per call output token cap is strictly 250 tokens (`max_output_tokens = 250`).
   - If usage information is returned by the provider, it is validated for positive integer input tokens and non negative integer output tokens. If missing or unsupported by the external API, the settled ledger spend uses the validated preflight estimate for input tokens and exact response payload length upper bound for output tokens, avoiding unmetered usage leaks.
6. **Dynamic Deadline Calculation**:
   - The escalation phase deadline is calculated as `phase_deadline_ms = min(query_remaining_ms, 2500.0)`.
   - If `phase_deadline_ms < 500.0` ms, escalation is aborted immediately, failing closed to refusal without calling the provider.
7. **Bounded Admission and Worker Capacity Retention (Slice 5.1)**:
   - Implemented in Slice 5.1 as `LayaProviderConcurrencyController` alongside the first provider invocation:
     - Global process admission queue caps active concurrent LAYA escalation workers at 4 (`max_concurrency = 4`).
     - Bounded wait queue caps waiting callers at 8 (`max_queue = 8`).
     - Load shedding: when the wait queue is full (8 callers queued), incoming escalation requests are rejected immediately with admission rejection, falling back to deterministic section 35 policy without blocking worker threads.
     - Capacity retention: when a caller deadline expires while waiting or during invocation (caller timeout), the underlying worker slot in the semaphore and thread pool is retained until the underlying provider call completes, preventing caller timeouts from flooding the provider.
     - Late completion discarding: if an underlying provider completion returns after caller timeout or cancellation, its result is discarded and does not mutate finished query state.

### Value sourcing

| Action | Value produced / displayed | Source |
|---|---|---|
| Trigger escalation | Escalation trigger decision | Derived in `run_fidelity` or `run_deterministic_math` when evidence is missing and `expansion_steps < 1` |
| Formulate missing requirement | `MissingRequirementDescription` | Derived from fidelity verification gap analysis or missing structured column operands |
| Decide expansion route | `LayaEscalationDecision` | Computed by LAYA model or fallback deterministic missing requirement policy |
| Validate route | Route approval / rejection | Computed by code validation against active settings, workspace permissions, discriminator agreement, and tombstones |
| Execute expansion | Expanded candidate chunks | Retrieved from existing BM25, Vector, PageIndex, OKF, or TableStore adapters |
| Verify delivery gate | Gate approval / block | Evaluated against verifier readiness; blocks free form prose if semantic verifier is unavailable |
| Answer synthesis | Final synthesis text | Synthesized in `run_llm` if and only if secondary evidence verification and delivery gate pass |
| Answer refusal | `final_answer = "NOT_FOUND"` | Emitted deterministically with empty citations when evidence remains insufficient |
| Telemetry logging | `laya_*` debug trace | Recorded in `QueryResponse.retrieval_candidates` from LAYA execution timings and tokens |

### Key invariants

1. **Strict Decision Boundary**: LAYA returns only routing decisions; it never generates answer text or user facing responses.
2. **At Most One Answer Synthesis**: Exactly one answer synthesis call can execute per query (`MAX_SYNTHESIS_CALLS = 1`). This respects ADR-004 by keeping total generation calls within `MAX_QUERY_LLM_CALLS = 2` while preserving the final semantic verification reserve.
3. **Final Delivery Gate Primacy**: Reserving a slot is not sufficient for delivery. Free form generated text is never emitted without passing final verification; if the verifier is unavailable, delivery is blocked in favor of approved deterministic output or refusal.
4. **Shared Single Expansion Allowance**: `MAX_EXPANSION_STEPS = 1`. All escalation paths (whether LAYA proposed or deterministic fallback, whether first choice or baseline alternative) share this single allowance. Escalation never chains into secondary or tertiary expansion loops.
5. **Code Validation Primacy**: Model proposed routes are untrusted suggestions. Code validates permissions, discriminator agreement, feature flags, opaque version bindings, and budgets before executing any expansion.
6. **Fail Closed Baseline Preservation**: Any error, timeout, or invalid proposal during escalation leaves baseline evidence intact and fails closed without throwing unhandled exceptions.
7. **No Progress Suppression**: If an expansion retrieves chunks that are already present in the candidate pool, escalation terminates immediately without further attempts.
8. **Structured Execution Independence**: Structured expansions execute directly through the approved TableStore executor outside reciprocal rank fusion, rejoining mandatory execution evidence.

### Security and Isolation Model

- Tenant boundary: All LAYA invocations pass explicit `workspace_id`. Prompt context contains only documents authorized in the current workspace.
- Version binding: Expansion lookups target only active document versions pinned in `PinnedSnapshotContext`. Document versions are preserved as opaque strings (max 64 chars) and are never converted to integers.
- Dynamic tombstone check: `WorkspaceBoundaryRepository.has_tombstone` is re verified before dispatching any expanded retrieval. If a source was deleted during query execution, the expansion returns zero candidates immediately fail closed.

### Critical test scenarios

- **Scenario 1 (Happy Path Lexical Expansion)**: Query with synonymous terminology fails initial discovery. LAYA analyzes the missing term, proposes `lexical_expansion` with targeted keywords. BM25 retrieves the missing chunk. Secondary verification succeeds. Single answer generation produces verified answer with exact citation. (AC-1, AC-2, AC-6, AC-7)
- **Scenario 2 (Happy Path Dense Expansion)**: Query with abstract semantic gap fails initial discovery. LAYA proposes `dense_expansion` with reformulated concept query. Vector store retrieves supporting chunk. Verification succeeds and answer is synthesized. (AC-2, AC-3)
- **Scenario 3 (Code Rejection of Disabled Route and Deterministic Fallback)**: LAYA proposes `graph_expansion` while `GRAPH_ENABLED=false`. Code validation rejects the proposal, falls back to deterministic section 35 lexical expansion, consumes the single expansion step, and succeeds or terminates cleanly without graph access. Baseline retrieval is preserved. (AC-3, AC-5)
- **Scenario 4 (Provider Timeout and Deterministic Policy)**: LAYA provider call times out. Pipeline catches the timeout fail closed, executes the deterministic section 35 route within remaining deadline, or emits clean refusal if time is exhausted. (AC-3, AC-4)
- **Scenario 5 (HTTP 429 Attempt Charging and Termination)**: LAYA encounters an HTTP 429 response. Attempt is charged to budget. Single allowed retry succeeds within deadline, or fails and falls back to deterministic policy without budget leak. (AC-4)
- **Scenario 6 (Authoritative Token Preflight and Truncation)**: Preflight evaluates the complete request via `count_tokens`. An oversized evidence summary is truncated deterministically to stay within 1000 input tokens before billable provider invocation. If unavailable, defensible bound or fail closed logic is exercised. (AC-4)
- **Scenario 7 (Deadline Bounding and Early Abort)**: Query has only 300 ms remaining when fidelity fails. Escalation calculates `phase_deadline_ms = 300 < 500` ms, aborts immediately without calling LAYA, and emits clean refusal. (AC-1, AC-4)
- **Scenario 8 (Missing Structured Operands Entry and Non RRF Execution)**: Structured math query misses required columns. Entry path routes to LAYA, proposing `structured_expansion`. TableStore fetches columns directly outside reciprocal rank fusion, math evaluates deterministically to final answer, and `run_llm` is not invoked. (AC-1, AC-6, AC-7)
- **Scenario 9 (Empty Context Entry Path)**: Discovery retrieves zero chunks (`compressed_text=""`). Fidelity detects empty context and triggers escalation rather than immediate failure. Lexical expansion finds chunks, secondary fidelity passes, and single synthesis executes. (AC-1, AC-7)
- **Scenario 10 (Invalid Confidence Rejection)**: LAYA returns decision with missing confidence or confidence equal to 1.5. Code validation rejects decision, executes deterministic fallback, and asserts routing confidence is not treated as evidence sufficiency. (AC-2, AC-3)
- **Scenario 11 (Tombstoned Source Rejection)**: LAYA proposes structural expansion on a document that has an active deletion tombstone. Code detects tombstone, suppresses expansion, and returns zero additional candidates. (AC-5)
- **Scenario 12 (No Progress Refusal)**: LAYA expansion returns chunks identical to those already retrieved. No progress detector halts escalation. Pipeline refuses cleanly with `NOT_FOUND` and zero answer generation calls. (AC-6, AC-7)
- **Scenario 13 (Route Discriminator Mismatch Rejection)**: Decision specifies `route="lexical_expansion"` but passes `DenseExpansionParams`. Schema validation rejects decision with discriminator mismatch error, routing to deterministic fallback. (AC-2, AC-3)
- **Scenario 14 (Oversized Payload Rejection)**: Request payload passes `retrieved_summary` with 11 items, `query_terms` with 6 items, term length > 64, or page number 0. Pydantic validation rejects the malformed payload fail closed. (AC-2, AC-5)
- **Scenario 15 (Queue Saturation and Load Shedding)**: Concurrency controller receives 9th queued escalation request when 8 slots are already occupied. Admission is rejected immediately with load shedding, falling back to deterministic policy without thread blocking. (AC-4)
- **Scenario 16 (Caller Timeout with Capacity Retention and Late Completion Discarding)**: Caller deadline expires after 1.0s while underlying Bedrock call takes 1.8s. Controller retains worker slot until completion, preventing thread leak or provider stampede. Late completion arriving after caller exit is discarded without mutating state. (AC-4)
- **Scenario 17 (Final Delivery Gate Blocks Free Form Delivery)**: Expansion and secondary fidelity pass, but generative semantic verifier is unconfigured in the environment. Delivery gate blocks free form synthesis, falling back to verified deterministic excerpt or refusal, proving that reserving a slot alone does not emit unverified text. (AC-6)
- **Scenario 18 (Missing or Invalid Provider Usage Handling)**: Bedrock response returns missing usage dict, negative input tokens, or output tokens exceeding 250. Gate catches invalid usage, fails closed, settles spent tokens, and executes deterministic fallback. (AC-4)
- **Scenario 19 (Cloudflare Provider Unavailable and No Bedrock Fallback)**: `LAYA_DECISION_PROVIDER="cloudflare"` is active. The Cloudflare endpoint returns HTTP 502 Bad Gateway or connection timeout. The adapter records `laya_status="provider_unavailable"`, `laya_provider_success=False`, `laya_fallback=True`, settles spent capacity, and executes Section 35 deterministic fallback. Bedrock Converse is never called, verifying no silent model substitution. (AC-3, AC-7)
- **Scenario 20 (Cloudflare Provider Success and Outcome State Telemetry)**: The Cloudflare adapter returns a valid route decision within the 2500ms phase deadline. Telemetry records `laya_enabled=True`, `laya_invoked=True`, `laya_provider_success=True`, `laya_fallback=False`, `laya_status="provider_success"`, and `laya_provider="cloudflare"`. (AC-2, AC-7)
- **Scenario 21 (Offline Adapter Isolation and Manual Smoke Check)**: Automated unit tests exercise Cloudflare adapter serialization, headers, status handling, and fallback with mocked HTTP transport. A separate manual script (`backend/scripts/smoke_cloudflare_laya.py`) tests live endpoint connectivity strictly on demand, with zero execution in automated test suites. (AC-8)

## Build plan (Tracer Bullet Approach)

The build plan follows a vertical Tracer Bullet slice, implementing the thinnest end to end path through the layers first:

### Slice 5.1: Thin Executable Path (Lexical Expansion, Safety Gate, and Bounded Admission)
1. **Contracts and Error Envelopes**: Implement frozen Pydantic models `MissingRequirementDescription`, `BaselineEvidenceSummary`, `LayaEscalationRequest`, `LayaEscalationDecision`, and typed route parameter models with discriminator agreement and bounded payload lengths in `backend/src/schemas/contracts/laya.py`. Opaque string versions (max 64 chars) and document IDs (max 128 chars) match existing contracts. Satisfies **AC-2**, **AC-5**.
2. **Deterministic Missing Requirement Policy**: Implement Architecture Bible section 35 deterministic fallback rules in `backend/src/services/retrieval/laya_policy.py`, mapping missing dimensions to approved expansion routes. Satisfies **AC-3**.
3. **Pre Reservation and Preflight Gate**: Implement atomic pre reservation checks, authoritative `count_tokens` preflight estimation with defensible bound fallback, and deadline calculation `min(query_remaining_ms, 2500.0)` in `backend/src/services/retrieval/laya_gate.py`. Satisfies **AC-4**.
4. **Bounded Admission and Capacity Retention**: Implement `LayaProviderConcurrencyController` in `backend/src/services/retrieval/laya_concurrency.py` capping active workers at 4, queuing at 8, enforcing immediate load shedding on saturation, capacity retention on caller timeout, and late completion discarding. Satisfies **AC-4**.
5. **LAYA Decision Provider Boundary and Cloudflare Adapter**:
   - Define `LayaDecisionProviderAdapter` protocol in `backend/src/services/retrieval/laya_service.py` to abstract model backends.
   - Implement `CloudflareLayaAdapter` targeting `/v1/systemone` with Bearer auth, request adapting from `LayaEscalationRequest` to the Cloudflare schema, response parsing, and explicit timeout bounding.
   - Implement `BedrockLayaAdapter` preserving Bedrock Converse Claude Haiku as an alternative selectable provider.
   - Add configuration settings: `LAYA_DECISION_PROVIDER` (`"cloudflare"` | `"bedrock"` | `"test"`), `LAYA_CLOUDFLARE_BASE_URL`, `LAYA_CLOUDFLARE_API_KEY`, `LAYA_CLOUDFLARE_TIMEOUT_MS`.
   - Enforce fail safe behavior: if the configured provider fails or is unreachable, record `laya_status="provider_unavailable"` and route immediately to Section 35 deterministic fallback with ZERO silent fallback to Bedrock.
   - Emit explicit telemetry for the four outcome states: `enabled`, `invoked`, `provider_success`, `fallback`.
   - In Slice 5.1, unsupported routes (`structural_expansion`, `dense_expansion`, `graph_expansion`, `structured_expansion`) are explicitly registered as unavailable, causing code validation to reject them or fall back. Satisfies **AC-2**, **AC-3**, **AC-4**, **AC-7**.
6. **Targeted Lexical Expansion Handler**: Wire targeted single pass BM25 retrieval handler in `LayaExpansionExecutor`. Satisfies **AC-1**, **AC-5**.
7. **LangGraph Pipeline Integration and Final Delivery Gate**: Wire `run_laya_escalation` node into `backend/src/services/langgraph_pipeline.py` between `run_fidelity` and `run_llm`, clearing stale refusal state upon expansion, reranking, recompressing, enforcing the single synthesis call limit, and gating free form delivery against verifier availability. Satisfies **AC-1**, **AC-6**, **AC-7**.
8. **Offline Slice 5.1 Test Suite and Adapter Verification**:
   - Implement offline regression suite in `backend/tests/test_laya_escalation_slice5_1.py` exercising lexical expansion, code rejection of unavailable routes, deterministic fallback, token preflight, discriminator validation, queue saturation, caller timeout with capacity retention, late completion discarding, delivery gate blocking, and single synthesis limit with mocked providers.
   - Implement dedicated provider adapter suite in `backend/tests/test_cloudflare_laya_adapter.py` testing Cloudflare request formatting, response parsing, 502/timeout handling, `provider_unavailable` telemetry, and proof that Bedrock is not invoked when Cloudflare fails.
   - Provide standalone manual live smoke check script in `backend/scripts/smoke_cloudflare_laya.py` for manual verification outside CI. Satisfies **AC-8**.

### Slice 5.2: Bounded Multi Route Thickening
1. **Remaining Expansion Handlers**: Wire targeted handlers for `dense_expansion` (vector search), `structural_expansion` (PageIndex), `structured_expansion` (TableStore non RRF execution), and optional `graph_expansion` when `GRAPH_ENABLED=true`.
2. **Structured Math Entry Wiring**: Wire missing operand detection in `run_deterministic_math` to trigger escalation into `structured_expansion`.
3. **Comprehensive Offline Ablation Suite**: Implement `backend/tests/test_laya_escalation_slice5_2.py` covering multi route dispatch, 429 retry charging, and full telemetry reporting across all expanded paths.

## Consequences

**Positive**:
- Solves factual answer drops caused by simple vocabulary mismatches or missing section locators.
- Eliminates the risk of runaway agent loops by bounding escalation to exactly one decision call and one expansion step.
- Prevents LAYA from inventing answer claims by restricting it strictly to routing decisions and keeping it out of answer prose generation.
- Preserves predictable query latency and strictly enforces the single answer synthesis invariant while respecting global model budget policy and verification reserves.
- Protects free form answer delivery so that unverified model prose is never emitted to users without passing an authoritative semantic verifier gate.
- Protects system capacity through bounded admission, immediate load shedding, and capacity retention during caller timeouts.

**Negative / tradeoffs**:
- Slightly increases pipeline graph complexity by adding an escalation branch and final delivery check.
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
