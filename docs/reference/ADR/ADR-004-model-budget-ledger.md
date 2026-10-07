# ADR-004 — Global Model Budget Ledger & query-default-v3 Policy

## Decision
Every query-time model and tool invocation passes an atomic pre-reservation gate under a unified query ledger. Distinct typed counters prevent hidden model consumption, protect mandatory finishing capacity according to answer mode, and enforce bounded execution.

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

### Answer-Mode-Aware Model Reservations

Model capacity reservations are dynamically aware of the query's required answer mode:

- **Free-Form Generation:** When free-form narrative generation is required, the scheduler must reserve capacity for **1 synthesis call + 1 final semantic-verifier call** before dispatching any optional generative planning or speculative calls.
- **Deterministic Answers:** Two generation calls are **not** reserved unconditionally for exact deterministic answers. Bound lookups or deterministic calculations formatted via approved templates do not consume synthesis or generative verifier slots.

### Valid Generation Paths Under `query-default-v3`

The scheduler enforces the following valid generation execution paths:

| Path | Generation Calls | Execution & Validation Contract |
|---|---|---|
| **Fully bound exact lookup / calculation** | **0** | Bound exact lookup or AST calculation + approved deterministic template. Mechanically verified citations. Zero LLM calls required or reserved. |
| **Planning proposal + deterministic execution** | **1** | Validated planning proposal (1 LLM call) + code-executed AST aggregation + approved deterministic template. Code rigorously validates proposed filters/AST; template formats final answer deterministically. |
| **Free-form synthesis + generative verification** | **2** | Deterministic evidence retrieval/join + 1 synthesis call + 1 final semantic-verifier call. Delivers only verified, supported wording. |
| **Planning + synthesis + generative verification** | **3 (Disallowed)** | Planning (1) + synthesis (1) + generative verifier (1) totals 3 generation calls and is **strictly disallowed** under the default policy. The system must use deterministic template planning, return an approved deterministic template answer, or explicitly escalate under an administrative high-budget policy. |

### Answer Mode Transitions & Safety Invariant
- If the answer mode changes during query execution (e.g. an initial plan targeting a deterministic template discovers that synthesis is necessary to reconcile narrative evidence), the scheduler must reserve the required finishing capacity (1 synthesis + 1 generative verifier) **BEFORE** performing any optional or exploratory work.
- If the remaining budget cannot satisfy the 2-call finishing reserve (e.g., 1 call was already consumed by generative planning), free-form generation is prohibited. The engine must fall back to verified deterministic excerpts, approved templates, or report explicit insufficiency.
- **Validation Invariant:** Never deliver generated wording without its required final semantic and mechanical validation.

### Global Cost & Deadline Limits
- **Deadline:** Query deadline defaults to `15,000 ms`. Branch and tool timeouts are derived strictly from the remaining deadline.
- **Accounting:** The ledger atomically decrements call slots and estimates before invocation, settling actual tokens/costs afterwards.
- **Concurrency Guard:** Parallel branches cannot compete against an unreserved shared pool; all capacity must be reserved prior to branch execution.
