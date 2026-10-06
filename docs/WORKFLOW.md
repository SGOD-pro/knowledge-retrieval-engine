# KRE Workflow

# KRE Workflow

## Master Workflow
```text
Upload → Authorize/Register → Validate → Immutable Source
→ Extract → Canonical Evidence
  ├─→ Baseline QA → Baseline Stores → Minimal Map → Baseline Snapshot (Immediately Queryable)
  └─→ [Asynchronous Branch] Optional Enrichment (PageIndex/OKF/Graph) 
        → Validate Support & Compatibility → Generation-Checked Publication → Extended Snapshot

Query Workflow:
Query → Authorize/Pin Snapshot → Requirements Built
→ Per-Requirement Route → Dispatch Structured Execution / Discovery Branches
→ Fan-In Barrier (Branch Join + Status/Deadline) → Compatible RRF / Rerank
→ Requirement Join (Mandatory Execution + Discovery Evidence)
→ Exact Evidence Fetch → Mechanical + Semantic Support Verification
→ Coverage & Completeness Decision
→ (Complete OR PartialAnswerReady) → AnswerMode (Deterministic Template OR Bounded Synthesis)
→ Final Semantic Validation → Final Mechanical/Access Validation → Delivered Result
```

## Query State Machine
```mermaid
stateDiagram-v2
    [*] --> Authorized
    Authorized --> SnapshotPinned
    SnapshotPinned --> RequirementsBuilt
    RequirementsBuilt --> ClarificationRequired: material ambiguity
    RequirementsBuilt --> Unsupported: illegal operation/unauthorized
    RequirementsBuilt --> PathsRunning: valid bindings
    PathsRunning --> RequirementJoin: all selected terminate or deadline
    RequirementJoin --> Verification
    Verification --> CoverageDecision
    CoverageDecision --> Complete: supported + complete
    CoverageDecision --> Expansion: missing + progress possible
    CoverageDecision --> StopDecision: deadline or no progress
    Expansion --> PathsRunning: bounded ready route & reserved budget
    Expansion --> StopDecision: budget exhausted or no progress
    StopDecision --> PartialAnswerReady: some independently supported requirements
    StopDecision --> NoSupport: scoped search complete without support
    StopDecision --> Incomplete: failed/timed-out required coverage
    Complete --> AnswerMode
    PartialAnswerReady --> AnswerMode
    AnswerMode --> DeterministicAnswer: approved template
    AnswerMode --> LLMSynthesis: synthesis & final verifier reserved
    LLMSynthesis --> FinalSemanticValidation
    FinalSemanticValidation --> FinalMechanicalValidation: supported wording
    FinalSemanticValidation --> SafeFallback: unsupported wording
    SafeFallback --> FinalMechanicalValidation: verified template or excerpts
    SafeFallback --> Incomplete: no safe supported output
    DeterministicAnswer --> FinalMechanicalValidation
    FinalMechanicalValidation --> Delivered: citations and access valid
    FinalMechanicalValidation --> ExplicitFailure: invalid or revoked access
    Delivered --> [*]
    ClarificationRequired --> [*]
    Unsupported --> [*]
    NoSupport --> [*]
    Incomplete --> [*]
    ExplicitFailure --> [*]
```

