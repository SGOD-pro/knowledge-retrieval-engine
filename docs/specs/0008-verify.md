# Verify: Bounded LAYA Escalation and Query Expansion (spec 0008 Slice 5.1, updated 2026-11-20)
_Steps derived from spec 0008 acceptance criteria. /check verify runs these; /test locks the durable ones._

## Verification Metadata
* **Tested Commit**: `5d05e5c709e99eb5962e26090c29ae022712dc26`
* **Branch**: `re-factor`
* **Repository**: `SGOD-pro/knowledge-retrieval-engine`
* **Execution Date**: 2026-11-20
* **Environment**: Linux x86_64, Python 3.14 via uv, DynamoDB Local (port 8000), local FastAPI and Uvicorn server (port 8001)
* **Provider Modes**: Mocked Bedrock runtime client fixtures for CountTokens and Converse, mocked OpenRouter reranker. Zero paid provider calls made. All local mocks are documented as local test doubles and not mischaracterized as live AWS cloud verification.
* **Status**: Slice 5.1 fully verified and passing. Spec 0008 in progress (Slice 5.1 thin lexical vertical path and admission control complete, Slice 5.2 multi route thickening planned). Spec 0003 deferred. LAYA_ENABLED=false and GRAPH_ENABLED=false defaults preserved.

## Exact Test Commands and Results

### 1. Offline Regression Suite (Slice 5.1 Focused)
```bash
cd backend && uv run pytest tests/test_laya_escalation_slice5_1.py -v
```
Result: **40 passed, 0 skipped in 7.43s**
Breakdown:
* `test_laya_contracts_strict_discriminator_and_bounds`: PASSED (frozen contract immutability, extra field rejection, strict discriminator agreement, opaque version bounds)
* `test_baseline_evidence_summary_normalized_bbox`: PASSED (normalized bounding box coordinate order x0 < x1, y0 < y1 between 0.0 and 1.0)
* `test_payload_length_caps`: PASSED (payload bounds on term count, summary list length, query length)
* `test_deterministic_fallback_policy_section_35`: PASSED (Section 35 deterministic fallback rules mapping missing dimensions to lexical expansion)
* `test_budget_ledger_pre_reservation_and_phase_deadline`: PASSED (atomic pre reservation with single call cap, deadline bounding, abort below 500ms)
* `test_token_preflight_and_evidence_truncation`: PASSED (authoritative count_tokens preflight and deterministic snippet truncation below 1000 input tokens)
* `test_strict_provider_usage_validation`: PASSED (provider usage dictionary validation, output cap at 250 tokens, rejection of negative or missing usage)
* `test_concurrency_queue_saturation_load_shedding`: PASSED (concurrency controller 4 workers, 8 queue slots, immediate admission_rejected load shedding)
* `test_caller_timeout_capacity_retention_and_late_settlement`: PASSED (caller timeout retains worker slot until completion, late completion discarded, late spend reconciled)
* `test_429_throttling_retries_charged_as_attempts`: PASSED (HTTP 429 retries charged as physical attempts within same bounded operation)
* `test_max_tool_attempts_ceiling_blocks_further_retries`: PASSED (MAX_TOOL_ATTEMPTS ceiling blocks pre reservation and retry attempts)
* `test_happy_path_lexical_expansion_success`: PASSED (end to end pipeline lexical expansion on coverage gap, stale refusal clearing, rerank, single synthesis call)
* `test_empty_context_triggers_escalation`: PASSED (empty context triggers LAYA escalation rather than immediate crash)
* `test_code_rejection_of_unavailable_routes_in_slice5_1`: PASSED (code validation rejects unavailable routes in Slice 5.1 and falls back to lexical)
* `test_version_isolation_and_opaque_string_binding`: PASSED (exact opaque string version matching, stale version returns zero chunks)
* `test_dynamic_tombstone_halts_retrieval_fail_closed`: PASSED (active source tombstone halts retrieval immediately fail closed with zero candidates)
* `test_dynamic_tombstone_blocks_delivery_gate`: PASSED (source tombstone detected before delivery blocks delivery fail closed)
* `test_delivery_gate_blocks_unverified_free_form_delivery`: PASSED (delivery gate blocks unverified free form prose when verifier is absent)
* `test_delivery_gate_rejects_contradicted_verification`: PASSED (delivery gate blocks delivery when verifier reports contradicted support)
* `test_single_synthesis_call_invariant`: PASSED (single synthesis call invariant enforced, second run_llm call skipped with zero model invocations)
* `test_stale_refusal_clearing_explicit`: PASSED (stale refusal attributes error, final_answer, citations cleared before secondary verification)
* `test_no_progress_clean_refusal`: PASSED (no progress detector halts escalation when identical chunks returned, emits structured refusal)
* `test_invalid_usage_fallback_through_decision_service`: PASSED (invalid usage dict falls back safely through decision service)
* `test_missing_usage_dict_fallback_through_decision_service`: PASSED (missing usage dict falls back safely through decision service)
* `test_dynamic_missing_dimension_derivation_in_pipeline`: PASSED (dynamic missing dimension derivation maps runtime coverage failures to lexical fallback)
* `test_laya_policy_empty_terms_routes_to_none`: PASSED (policy with empty fallback terms cleanly routes to none expansion)
* `test_compiled_pipeline_tombstoned_replacement_excerpt_suppressed`: PASSED (delivery gate blocks tombstoned replacement excerpt fail closed)
* `test_compiled_pipeline_missing_boundary_authority_fails_closed`: PASSED (missing boundary authority fails closed to structured refusal)
* `test_compiled_pipeline_pinned_snapshot_negative_control`: PASSED (unpinned documents, mismatched versions, and conflicting metadata rejected)
* `test_count_tokens_capacity_retention_and_malformed_rejection`: PASSED (malformed count tokens response rejected and capacity preserved)
* `test_strict_usage_validation_zero_slack`: PASSED (zero slack validation on provider usage response)
* `test_compiled_pipeline_production_snapshot_no_kg_manifest`: PASSED (production snapshot resolution succeeds for baseline ready documents without KG manifest)
* `test_compiled_pipeline_snapshot_resolution_failure_stops_expansion`: PASSED (snapshot authority failure halts expansion fail closed)
* `test_compiled_pipeline_slow_retrieval_deadline_retains_capacity`: PASSED (slow expansion retrieval exceeding caller deadline retains capacity and discards late candidates)
* `test_compiled_pipeline_slow_reranking_deadline_retains_capacity`: PASSED (slow post expansion reranking exceeding caller deadline retains capacity and discards late candidates)
* `test_compiled_pipeline_slow_compression_retains_capacity_and_suppresses_late_result`: PASSED (slow post expansion compression exceeding caller deadline retains capacity and suppresses late output)
* `test_compiled_pipeline_slow_fidelity_retains_capacity_and_suppresses_late_result`: PASSED (slow secondary fidelity exceeding caller deadline retains capacity and triggers clean refusal)
* `test_compiled_pipeline_slow_synthesis_retains_capacity_suppresses_late_result_and_reconciles_spend`: PASSED (slow answer synthesis exceeding caller deadline retains capacity, suppresses late answer, and reconciles ledger spend)
* `test_provenance_validation_rejects_conflicting_metadata_and_accepts_valid_control`: PASSED (provenance validator compares all populated attributes and metadata across workspace, document, and version, rejecting any disagreement)
* `test_compiled_pipeline_provenance_conflicts_rejected_and_positive_control_expanded`: PASSED (compiled pipeline rejects chunks with conflicting metadata while expanding valid positive control chunk)

### 2. Live Runtime Verification Over HTTP and Backend Services
```bash
cd backend && uv run python ../verify_0008_live.py
```
Result: **20 passed, 0 failed** (evidence ledger saved to scratch area)
Breakdown:
* Live FastAPI server health check responds OK (`GET /health` -> 200)
* Create workspace for Alice over live HTTP (`POST /workspaces` -> 201)
* Cross workspace boundary rejection: Bob denied access to Alice workspace (`POST /workspaces/{ws_id}/query` -> 404)
* Frozen contracts, discriminator agreement, and strict numeric confidence
* Normalized bounding box ordering (x0 < x1, y0 < y1) and opaque string version bound
* Section 35 deterministic fallback maps missing dimension to lexical expansion
* Budget ledger atomic reservation (single call cap) and phase deadline abort (<500ms)
* Authoritative token preflight and deterministic snippet truncation (<=1000 tokens)
* Strict provider usage validation (caps output tokens at 250, rejects negative usage)
* Concurrency admission queue saturation and immediate load shedding (admission_rejected)
* HTTP 429 throttling retries charged as physical attempts within same bounded operation
* Pipeline lexical expansion: BM25 retrieval, stale refusal clearing, and candidate fusion
* Code validation rejects unavailable route proposal and executes Section 35 fallback
* Version isolation: matching opaque version retrieves chunk, stale version returns 0 chunks
* Dynamic tombstone halts expanded retrieval immediately fail closed (0 candidates)
* Dynamic tombstone before delivery blocks free form delivery fail closed (refusal)
* Final delivery gate blocks unverified free form text, falling back to deterministic excerpt or refusal
* Final delivery gate rejects contradicted verification, emitting clean refusal NOT_FOUND
* Single synthesis call invariant: second run_llm call returns empty dict with zero model calls
* No progress detection halts escalation into clean refusal with zero synthesis calls

### 3. Integrated Pipeline and Boundary Regression Suites
```bash
cd backend && uv run pytest tests/test_laya_escalation_slice5_1.py tests/test_contracts_and_identity.py tests/test_workspace_boundary_isolation.py tests/test_phase2.py tests/test_phase3.py tests/test_deterministic_pipeline_wiring.py tests/test_mandatory_audit_guards.py -q
```
Result: **150 passed, 1 warning in 15.48s**

Integrated test count reconciliation:
* Historical count of 131 tests derived from a previous run before wiring and audit guard suites were added (35 slice tests plus 96 baseline tests).
* Intermediate count of 145 tests occurred after integrating the full baseline regression suites (35 slice tests plus 61 contracts, 22 boundary isolation, 14 phase 2, 8 phase 3, 7 deterministic wiring, 4 mandatory audit guards, totaling 110 baseline tests, 35 + 110 = 145).
* Current count of 150 tests reflects the 5 additional compiled application path regressions in `tests/test_laya_escalation_slice5_1.py` covering slow compression, slow secondary fidelity, slow answer synthesis with capacity retention and spend reconciliation, and cross field provenance disagreement gating, bringing slice tests to 40 and total integrated tests to 150 (40 + 110 = 150 passed).

## Acceptance Criteria Coverage (Slice 5.1)
- AC-1: Bounded escalation triggers when baseline discovery lacks sufficient evidence support (such as coverage error in fidelity verification or empty context) and the query budget retains an unused expansion slot (expansion_steps == 0 and laya_calls < MAX_QUERY_LAYA_CALLS). Escalation does not activate when baseline evidence is sufficient or remaining deadline is below the minimum viable threshold (500ms).
- AC-2: LAYA operates as a typed decision model and never emits user facing answer prose. It accepts a typed LayaEscalationRequest and returns a typed LayaEscalationDecision specifying route, parameters, and numeric confidence. Enforces strict discriminator agreement between route and parameters.route_type. Missing, null, or boolean confidence is rejected. Routing confidence is never used to certify evidence sufficiency.
- AC-3: Code validates the proposed decision before execution through a deterministic state machine. In Slice 5.1, non lexical routes (dense_expansion, structural_expansion, graph_expansion, structured_expansion) are explicitly unavailable; proposals for these routes trigger deterministic Section 35 fallback to lexical expansion or clean refusal. All fallback paths share the single expansion allowance.
- AC-4: Authoritative resource budgets govern escalation under ADR-004. Enforces exactly one answer synthesis call (MAX_SYNTHESIS_CALLS = 1), at most one LAYA decision call (MAX_QUERY_LAYA_CALLS = 1), at most one targeted expansion step (MAX_EXPANSION_STEPS = 1), at most 1000 input prompt tokens, at most 250 output decision tokens, and phase deadline min(query_remaining_ms, 2500.0). Pre reservation atomically checks slots before dispatch, 429 throttling retries are charged as attempts, and worker concurrency is capped at 4 workers and 8 queue slots with capacity retention on caller timeout.
- AC-5: Workspace, document version, and tombstone isolation are enforced across escalation. Document identifiers are strings up to 128 characters, and document versions are opaque strings up to 64 characters. Expanded retrieval executes strictly within the authorized workspace. Active source or workspace tombstones in WorkspaceBoundaryRepository are checked dynamically before retrieval and before delivery, returning zero candidates fail closed.
- AC-6: Exactly one answer synthesis call is permitted per query. If evidence remains unsupported after expansion, the pipeline bypasses answer synthesis entirely, returning a deterministic refusal with zero generation calls. Free form generated wording cannot be delivered without passing the authoritative final answer delivery gate; if the semantic verifier is unconfigured or unavailable, free form delivery is explicitly blocked in favor of approved deterministic output or explicit refusal.
- AC-7: Expanded retrieval candidates pool into evidence collection alongside baseline candidates. For narrative routes, candidates are reranked (consuming the second rerank batch allowed under ADR-004) and recompressed. Stale refusal state is cleared before rechecking support. If secondary verification succeeds, answer synthesis proceeds; if verification fails or no progress is detected, the engine emits a structured refusal (final_answer="NOT_FOUND", citations=[], confidence_score=0.0, retrieval_incomplete=True).
- AC-8: Comprehensive offline regression test harness verifies escalation behavior without paid provider calls. Offline tests use mocked LAYA decision doubles, verifying successful lexical expansion, fallback on unavailable routes or provider timeout, tenant isolation, tombstone halting, no progress suppression, queue saturation, caller timeout with capacity retention, and strict enforcement of the single answer synthesis invariant. Slice 5.1 implements the thin executable vertical path through lexical expansion with unsupported routes explicitly unavailable.
