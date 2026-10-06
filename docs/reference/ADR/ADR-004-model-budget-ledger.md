# ADR-004 — Global Model Budget Ledger & query-default-v3 Policy

## Decision
Every query-time model and tool invocation passes an atomic pre-reservation gate under a unified query ledger. Distinct typed counters prevent hidden model consumption, protect mandatory finishing capacity, and enforce bounded execution.

## Specification: `query-default-v3` Policy

The baseline query execution policy `query-default-v3` defines the following counter limits:

| Counter / Budget Dimension | Limit | Scope & Constraints |
|---|---|---|
| `MAX_QUERY_LLM_CALLS` | 2 | Total text-generating calls (planning, retrieval reasoning, synthesis, and generative verification). |
| `MAX_QUERY_RERANK_CALLS` | 2 | Bounded encoder batches: at most 1 for initial narrative retrieval and 1 for the single allowed expansion. |
| `MAX_QUERY_ENCODER_VERIFY_CALLS` | 1 | Bounded support-check batch (only if an evaluated encoder verifier is configured). |
| `MAX_QUERY_EMBED_CALLS` | 2 | Bounded query-embedding batches. **Never embed documents during Q&A.** |
| `MAX_QUERY_LAYA_CALLS` | 2 | Decision calls across initial optional routing and escalation. |
| `MAX_EXPANSION_STEPS` | 1 | Single targeted expansion round (initial retrieval does not count as expansion). |
| `MAX_TOOL_ATTEMPTS` | 3 | Total attempts per retry-safe transient operation, charged against global deadline/cost. |

### Finishing Reservations
- **Mandatory Reserve:** Before dispatching any optional LLM planning or exploratory calls, the scheduler must reserve capacity for **1 synthesis call + 1 final verifier call**.
- **Optional Work Block:** If remaining budget cannot preserve the 2 finishing generation slots, optional generative planning or speculative calls are prohibited.
- **Three-Call Disallowance:** A sequence requiring LLM planning (1) + synthesis (1) + generative verifier (1) totals 3 generation calls and is **disallowed** under `query-default-v3`. It must use deterministic template planning, return a template answer, or explicitly escalate under an administrative high-budget policy.

### Global Cost & Deadline Limits
- **Deadline:** Query deadline defaults to `15,000 ms`. Branch and tool timeouts are derived strictly from the remaining deadline.
- **Accounting:** The ledger atomically decrements call slots and estimates before invocation, settling actual tokens/costs afterwards.
- **Concurrency Guard:** Parallel branches cannot compete against an unreserved shared pool; all capacity must be reserved prior to branch execution.

