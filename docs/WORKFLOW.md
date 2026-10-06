# KRE Workflow

## Master Workflow
```text
Upload → Authorize/Register → Validate → Immutable Source
→ Extract → Canonical Evidence
→ Baseline QA → Baseline Stores → Minimal Map → Baseline Snapshot
→ Optional Enrichment → Enriched Snapshot
→ Query → Authorize/Pin Snapshot → Requirements
→ Route → Execute/Discover → Join
→ Exact Evidence Fetch → Mechanical + Semantic Verification
→ Support + Completeness
→ Deterministic Answer OR Bounded Synthesis
→ Final Validation
```

## Query State Machine
```mermaid
stateDiagram-v2
    [*] --> Authorized
    Authorized --> SnapshotPinned
    SnapshotPinned --> RequirementsBuilt
    RequirementsBuilt --> ClarificationRequired: ambiguity
    RequirementsBuilt --> PathsRunning: valid
    PathsRunning --> RequirementJoin: all selected terminate or deadline
    RequirementJoin --> Verification
    Verification --> Complete: supported + complete
    Verification --> Expansion: missing + progress possible
    Verification --> NoSupport: scoped search complete without support
    Verification --> Incomplete: failed/timed-out required coverage
    Expansion --> PathsRunning: bounded ready route
    Expansion --> Incomplete: budget/deadline/no-progress
    Complete --> AnswerMode
    AnswerMode --> DeterministicAnswer: approved template
    AnswerMode --> LLMSynthesis: finishing budget reserved
    LLMSynthesis --> FinalSemanticValidation
    FinalSemanticValidation --> FinalMechanicalValidation: supported
    FinalSemanticValidation --> SafeFallback: unsupported wording
    SafeFallback --> FinalMechanicalValidation: safe supported output
    DeterministicAnswer --> FinalMechanicalValidation
    FinalMechanicalValidation --> Delivered: pass
    FinalMechanicalValidation --> Failed: invalid/revoked
```
