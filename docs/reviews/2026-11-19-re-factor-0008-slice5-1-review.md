# Review, re-factor (0008 Slice 5.1), 2026-11-19

**Reviewed by**: claude-opus-4-6-thinking (author on gemini)
**Scope**: 14 files, branch vs base (3c7142bc)
**Verdict**: Changes requested

## Summary
Slice 5.1 implements bounded LAYA escalation with lexical expansion, typed contracts, budget gating, concurrency admission, a delivery gate, and a comprehensive 21 test offline suite (all passing). The architecture is well decomposed across policy, gate, concurrency, service, executor, and pipeline integration layers, and the contracts match the spec precisely. Two correctness bugs need fixing: a `settle_spend` call with wrong keyword arguments that will crash on the InvalidUsageError path, and a double spend settlement for successful completions where the concurrency controller callback already reconciled tokens.

## Blockers

### 🔴 Wrong keyword arguments in `settle_spend` call, `backend/src/services/retrieval/laya_service.py:166-172`
**Problem**: The `InvalidUsageError` handler calls `ledger.settle_spend(in_tok=..., out_tok=...)` but `LayaBudgetLedger.settle_spend` has parameters named `input_tokens` and `output_tokens`. This will raise `TypeError: settle_spend() got an unexpected keyword argument 'in_tok'` at runtime.
**Why it matters**: When a provider returns malformed usage (a scenario the spec explicitly requires handling under AC-4 Scenario 18), the fail closed path will crash with an unhandled TypeError instead of gracefully falling back to the deterministic policy. The error will propagate up and may surface as an unhandled 500 to the caller, violating the fail closed invariant.
**Suggested fix**: Change the keyword arguments to `input_tokens=` and `output_tokens=` to match the `settle_spend` signature.

## Major

### 🟠 Double spend settlement on successful completions, `backend/src/services/retrieval/laya_service.py:130-172`
**Problem**: The `_spend_reconcile_cb` callback (line 130-134) is passed to `run_provider_call`, which invokes it at concurrency controller line 148-150 when the call completes on time. If `validate_provider_usage` then raises `InvalidUsageError`, the handler at line 166-172 calls `ledger.settle_spend` a second time for the same provider invocation. This double counts token spend in the ledger. Even on the happy path where usage validates, the spend is settled once via the callback. The inconsistency is that the InvalidUsageError path settles twice.
**Why it matters**: Double settlement inflates `total_input_tokens` and `total_output_tokens` in the budget ledger, causing incorrect telemetry reporting and potentially premature budget exhaustion on future queries if the ledger were reused (not the case per query today, but a latent accounting error).
**Suggested fix**: Remove the `settle_spend` call in the `InvalidUsageError` handler (lines 166-172) since the callback already settled the spend. If additional status tracking is needed for invalid usage, record it in `settled_history` without re adding token counts.

### 🟠 Hardcoded `missing_dimension="exact_term"` for all escalation entries, `backend/src/services/langgraph_pipeline.py:1308`
**Problem**: `run_laya_escalation` always constructs `MissingRequirementDescription` with `missing_dimension="exact_term"` regardless of the actual failure mode. The spec defines five dimensions (`location`, `exact_term`, `semantic_concept`, `relationship`, `tabular_operand`) and the deterministic fallback policy uses the dimension to choose the right route.
**Why it matters**: In Slice 5.1 where only lexical expansion is available, all dimensions fall back to lexical anyway, so this is not broken today. However, it means the LAYA model always receives `exact_term` as the missing dimension, which may cause suboptimal routing decisions and will be a correctness problem in Slice 5.2 when non lexical routes are enabled. Additionally, the fallback policy rationale string will always say `dim=exact_term` even when the actual gap is a location or relationship issue, producing misleading telemetry.
**Suggested fix**: Derive `missing_dimension` from the error type or failed stage. For example, `run_fidelity_empty_context` could map to `semantic_concept`, `run_deterministic_math_operands` to `tabular_operand`, and `run_fidelity_coverage` could inspect the error message for dimension hints. At minimum, accept it as an input or add a TODO comment documenting the gap for Slice 5.2.

## Minor

### 🟡 `run_laya_escalation` bypasses the LangGraph state machine by calling node functions directly, `backend/src/services/langgraph_pipeline.py:1394-1423`
**Problem**: The `run_laya_escalation` function calls `run_reranker(current_state)`, `run_compressor(current_state)`, `run_fidelity(current_state)`, and `run_llm(current_state)` as direct Python function calls rather than through the LangGraph graph traversal. This creates a mini pipeline inside a single node.
**Why it matters**: LangGraph state management, tracing, and checkpointing are bypassed for these sub calls. If any of these functions are updated to rely on LangGraph specific features (reducers, channels, interrupts), the inline calls will silently diverge from the graph path. The approach works correctly today because these functions operate as pure state transformers, but it couples the escalation node to the internal signatures of four other nodes.
**Suggested fix**: This is an accepted tradeoff for Slice 5.1 (the spec describes post expansion reranking and recompression as inline operations). Consider adding a comment explaining the deliberate choice and noting that Slice 5.2 may restructure this as a subgraph.

### 🟡 Defensive `rerank` return value unpacking is broader than needed, `backend/src/services/langgraph_pipeline.py:922-927`
**Problem**: The diff adds a defensive `isinstance(rerank_res, tuple)` check to handle the case where `rerank()` with `return_mode=True` might not return a tuple. The existing `rerank` function with `return_mode=True` always returns a tuple; this defensive guard obscures the real contract.
**Why it matters**: If `rerank` changes its return shape, this silently degrades to a wrong `reranker_mode` value instead of failing fast. The `getattr(rerank_res, "reranker_mode", ...)` fallback on a list of chunks will never find the attribute, silently defaulting to `BYPASSED`.
**Suggested fix**: Either assert the tuple contract (`assert isinstance(rerank_res, tuple)`) or revert to the direct unpacking and handle any structural change to `rerank` at the call site where it is introduced.

### 🟡 `LayaBudgetLedger._lock` not declared in model fields, `backend/src/services/retrieval/laya_gate.py:62-64`
**Problem**: `LayaBudgetLedger` is a Pydantic `BaseModel` with `arbitrary_types_allowed=True`. The `_lock` attribute is set in `__init__` via `self._lock = threading.Lock()` after `super().__init__()`, bypassing Pydantic field declaration. This relies on Pydantic v2 allowing private attributes to be set post init but is fragile if the model is serialized or copied.
**Why it matters**: Calling `model_copy()` or `model_dump()` on the ledger will not preserve the lock. If the ledger were ever round tripped through serialization (not the case today), the lock would be lost, breaking thread safety.
**Suggested fix**: Declare `_lock` as a `PrivateAttr` using `from pydantic import PrivateAttr` and `_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)` to follow Pydantic v2 conventions.

### 🟡 Unbounded `settled_history` list, `backend/src/services/retrieval/laya_gate.py:60`
**Problem**: `settled_history: list[dict[str, Any]]` grows without bound as spend events are appended. While each query creates a fresh ledger and Slice 5.1 caps at one LAYA call, the data structure has no inherent cap.
**Why it matters**: Not a problem today given the single call cap, but if the cap were raised in a future slice or the ledger were reused, the list could grow without bound. A defensive cap (e.g., `max_length=10`) would align with the bounded design philosophy.
**Suggested fix**: Consider adding a list length cap or making this a bounded deque. Low priority given per request ledger lifecycle.

### 🟡 `query_service.py` uses `getattr(response, ...)` instead of direct attribute access, `backend/src/modules/query/query_service.py:347-354`
**Problem**: The new telemetry fields are accessed via `getattr(response, "laya_calls", 0)` even though these fields are now declared on `QueryResponse` with defaults. Direct attribute access would be clearer and type safe.
**Why it matters**: `getattr` with defaults silently swallows missing attributes, which could mask a schema mismatch. Since the fields are now declared on the model, `response.laya_calls` would work and would fail fast if the attribute were accidentally removed.
**Suggested fix**: Use direct attribute access: `response.laya_calls`, `response.expansion_steps`, etc.

## Nits
- ⚪ `backend/src/services/langgraph_pipeline.py:14`, `import uuid` is placed after the third party imports block; move it to the stdlib imports section for PEP 8 compliance.
- ⚪ `backend/src/services/retrieval/laya_gate.py:268`, usage validation allows `in_tokens > estimated_prompt_tokens + 100` but the docstring (line 236) says `+ 50 slack`. The 100 token variance is more conservative but contradicts the docstring.
- ⚪ `backend/src/services/retrieval/laya_policy.py:88`, the `if terms:` guard is always true because `extract_fallback_query_terms` guarantees at least `("query",)` as a fallback, making the else branch at line 104 dead code.
- ⚪ `backend/src/schemas/contracts/laya.py:219-224`, the `validate_confidence_not_bool` field validator and the `execution_status` field declaration are visually entangled. The `execution_status` field appears to be declared after the validator method body without a blank line separator, making it look like part of the validator.
- ⚪ `backend/src/services/retrieval/laya_concurrency.py:41-44`, the `ThreadPoolExecutor` is created in `__init__` but never shut down. For test cleanup, consider adding a `shutdown` method or using it as a context manager.

## Strengths
- The contract layer (`laya.py`) is excellent: frozen models, strict `extra="forbid"`, discriminator agreement validation, boolean confidence rejection, and normalized bbox checks match the spec precisely and form a strong defensive boundary against malformed model outputs.
- The concurrency controller design (bounded semaphore, capacity retention on caller timeout, late completion discarding with spend reconciliation) is a sophisticated and correct implementation of a tricky concurrency pattern. The threading model is sound and well tested with the timing based integration test.
- The test suite (21 tests, all passing) covers a wide range of scenarios including edge cases like queue saturation, caller timeout with capacity retention, tombstone blocking, delivery gate blocking, discriminator mismatch, and no progress detection. The mock test doubles are clean and well structured.
- The delivery gate correctly enforces fail closed behavior: tombstone recheck before delivery, verifier absence blocking free form prose, and CONTRADICTED verification blocking delivery are all correctly handled with structured refusal fallback.

## Test coverage
Tests are `configured` and 21 offline tests pass, covering all 8 build plan items for Slice 5.1. Coverage is strong across contracts (AC-2), policy fallback (AC-3), budget gating (AC-4), concurrency admission (AC-4), usage validation (AC-4), tombstone isolation (AC-5), delivery gate (AC-6), and no progress refusal (AC-7). The `InvalidUsageError` path with the wrong keyword arguments (Blocker 1) is not tested end to end, which is why the bug was not caught. Consider adding a test that exercises the `validate_provider_usage` rejection path through `LayaDecisionService.decide()` to verify the full fail closed flow.
