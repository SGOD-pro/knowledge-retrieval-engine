# Open Knowledge Format (OKF) — Merged v0.1 + v0.2 Reference

When we [introduced the Open Knowledge Format](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing) (OKF) in June 2026, we asserted that the context that agents need (table schemas, metric definitions, runbooks) should live in a format, not in a proprietary service, and not scattered across unstructured text blobs. Accordingly, OKF v0.1 started simple: just markdown, YAML frontmatter, and a handful of conventions.

The strong response and contributions from the developer community informed our focus for the next version. Since launch, contributors opened extension proposals (typed relationship edges, agent-routing hint fields, an optional erasure conformance profile, an .okfignore convention, and more), sent new sample bundles, and begun cataloging OKF ecosystem tools built outside Google. Many of these contributions and contributor feedback reflected a larger concern about OKF: once agents are writing to the corpus, can it really be trusted?

The most valuable OKF bundles won't be written by hand once and then read forever. They're written continuously, by agents, and consumed by a different set of agents. A human-authored wiki page comes with an implicit guarantee: a person wrote it, and you can hold them accountable if it is wrong. When an agent generates ten thousand concepts overnight, that guarantee is gone. To provide accountability, a consumer (often another agent) has to judge each concept on explicit signals instead, and needs to answer five questions:

1. What was this created from? (**provenance**)
2. How much should I trust it? (**trust**)
3. Is it still true? (**freshness**)
4. Is it the current version? (**lifecycle**)
5. Was this number produced the way we said it must be? (**attestation**)

In OKF v0.2, it is now possible to answer all five of those questions from frontmatter, while the format remains as minimally opinionated as v0.1. It adds vocabulary, not rules: `type` is still the only always-required field, every new field is opt-in, custom keys are still preserved rather than rejected, and a bundle that adopts none of the additions is exactly as valid as it was under v0.1. Everything new below is optional, but its absence now carries meaning: an unverified concept is distinguishable from a verified one (although never rejected for the difference).                    

## **From describing to deciding**

v0.1 already kept metadata in frontmatter: `type, title, description, resource, tags`. Those fields describe a concept: what it is and what it points at. v0.2 adds a second kind of frontmatter field, the kind you use to decide something about a concept before you read it: who produced it, whether it has been verified, whether it is still current, and how a value it reports should be computed.

The reason these fields belong in frontmatter is that most interactions with a concept never actually progress to accessing the information in the body of the file. A consumer, whether a person, deterministic code, or an agent scanning during search and discovery, first has to decide whether a concept is relevant at all. Everything in a concept should be concise, but frontmatter has the narrower job of elevating exactly the signals needed to make decisions about relevance and trustworthiness, so it can be made cheaply and often, without spending tokens on prose. The content that must be read in full stays in the body, accessed only once a concept is chosen. Trust becomes something you can filter on *before* you commit to reading.

To make the sections that follow concrete, every example below draws from a small OKF v0.2 bundle we've prepared as a companion to this post: acme\_retail, a fictional US retail company's shared knowledge for AI-assisted analytics over BigQuery:

```
acme_retail/
```

```
├── index.md, log.md
```

```
├── tables/       orders.md
```

```
├── metrics/      revenue.md, gross-margin.md, gross-margin-legacy.md
```

```
├── computations/ revenue-ytd.md, gross-margin-period.md
```

```
├── skills/       run-on-bq.md
```

```
├── attesters/    sql_equality.py
```

```
└── policies/     revenue-recognition.md, margin-standard.md
```

Each section that follows shows the corresponding file. Here is tables/orders.md, a concept carrying the new signals:

```
---
```

```
type: BigQuery Table
```

```
title: Customer Orders
```

```
description: One row per completed customer order across web, mobile, and marketplace channels. The grain is the order, not the line item.
```

```
resource: https://bigquery.googleapis.com/v2/projects/acme/datasets/sales/tables/orders
```

```
tags: [sales, orders, revenue]
```

```
generated: { by: reference_agent/gemini-2.5-pro, at: 2026-06-30T14:00:00Z }
```

```
verified:
```

```
  - { by: human:kliu@acme, at: 2026-07-01T16:00:00Z }
```

```
status: stable
```

```
stale_after: 2026-12-31
```

```
sources:
```

```
  - id: warehouse-schema
```

```
    resource: https://wiki.acme.internal/data/warehouse/schemas/sales
```

```
    title: Acme Retail warehouse schema — sales dataset
```

```
    author: team:data-platform
```

```
    usage_count: 1240
```

```
    last_modified: 2026-06-15
```

```
  - id: revenue-policy
```

```
    resource: policies/revenue-recognition.md
```

```
    title: Revenue Recognition Policy (FY2026)
```

```
    author: human:jsmith@acme
```

```
    last_modified: 2026-06-15
```

```
---
```

```
​
```

```
# Schema
```

```
​
```

```
| Column         | Type          | Description                                                                            |
```

```
|----------------|---------------|----------------------------------------------------------------------------------------|
```

```
| `order_id`     | STRING        | Globally unique order id. [^warehouse-schema]                                          |
```

```
| `order_ts`     | TIMESTAMP     | Order placement time in UTC; drives fiscal-year assignment. [^revenue-policy]          |
```

```
| `order_status` | STRING        | Revenue is recognized only at `'delivered'` and after the 30-day return window. [^revenue-policy] |
```

```
| `net_amount`   | NUMERIC(18,4) | `gross_amount - discount_amount`. The recognized-revenue amount per policy. [^revenue-policy] |
```

Each of those families answers one of the five questions.

### Provenance: sources, not a score

The new `sources` field records the materials a concept derives from: an external doc, a bundle-relative path, or even a scope descriptor like "all queries in project X." At the same time, an entry can carry objective **credibility signals**: `author, usage_count, last_modified.`

The deliberate choice here is what we *didn't* add. OKF records the signals, not a credibility score. A score is subjective, doesn't port across consumers, and goes stale the moment it's written. Instead, credibility is inferred from the signals by whoever is consuming (and can be dynamically scored by the consumer, if desired), the same way you'd trust a heavily used, recently updated, authoritatively authored source more than an anonymous one. And when the body cites a specific source, it does so with an ordinary markdown footnote keyed to the source `id` (`[^export-schema]`), so attribution is per-claim instead of a dangling list at the bottom.

### Trust: `generated`, `verified`

Trust is established using two fields, kept deliberately distinct, because *who wrote something need not be who confirmed it*:

- `generated: { by, at }`: how the current content was produced, and when it last meaningfully changed.
- `verified: [ { by, at } ]`: a list of independent confirmations against the sources or the underlying resource; a human sign-off, a nightly finance process, or both.

From `verified`, a consumer derives a **trust tier**: no `verified` key is unverified; confirmation by machine actors only is machine-confirmed; confirmation by a `human:<id>` actor is human-reviewed. Tiers are advisory signals, not access control, but they let a consumer say "only surface human-reviewed metrics in the executive dashboard" as a frontmatter filter.

In **acme\_retail:** metrics/revenue.md is authored by the reference agent and verified by the VP of Finance, which places it in the human-reviewed tier. An executive-dashboard consumer configured with a trust-tier filter surfaces it; a throwaway testing environment can accept lower-tier concepts:

```
type: Metric
```

```
title: Revenue
```

```
generated: { by: reference_agent/gemini-2.5-pro, at: 2026-06-30T14:00:00Z }
```

```
verified:
```

```
  - { by: human:jsmith@acme, at: 2026-07-01T09:00:00Z }
```

### Freshness and lifecycle: stale\_after, status

OKF v0.2 establishes freshness and lifecycle with the `stale_after` and `status` fields. `status` moves a concept through `draft → stable → deprecated `(absent means stable). `stale_after` is a single absolute date. We chose an absolute date over a relative TTL on purpose: staleness becomes a plain date comparison with no reference to *when* the concept happened to be read, which is exactly the kind of determinism a non-LLM consumer wants.

**In acme\_retail:** metrics/revenue.md and metrics/gross-margin.md both carry `stale_after`: `2026-12-31` because Acme's finance team re-approves the underlying policies every January. On 2027-01-01 both concepts require re-verification against the FY2027 policy before serving.

And metrics/gross-margin-legacy.md is` status: deprecated`. Acme changed its cost allocation standard in Feb 2026 (the old formula excluded shipping and fulfillment from its cost of goods sold). The legacy definition is preserved for historical query reproducibility but not surfaced to new work:

```
type: Metric
```

```
title: Gross Margin (legacy, pre-FY2026)
```

```
status: deprecated
```

### Attestation: Was this number computed the sanctioned way?

Provenance answers where a claim came from. Attestation answers a harder question that matters the moment an agent reports a dollar figure: was this number produced the way we said it must be, or did the agent improvise its own SQL?

OKF v0.2 introduces a new concept type, `Attested Computation`. It carries not just what a value means but a sanctioned way to compute it, and the means to check that the sanctioned thing actually ran. Here is acme\_retail/computations/revenue-ytd.md:

```
---
```

```
type: Attested Computation
```

```
title: Revenue for a fiscal year
```

```
runtime: bigquery
```

```
parameters:
```

```
  - { name: year, type: integer, required: true }
```

```
executor:
```

```
  resource: skills/run-on-bq.md
```

```
  receipt: [job_id, executed_sql, result]
```

```
attester:
```

```
  resource: attesters/sql_equality.py
```

```
generated: { by: reference_agent/gemini-2.5-pro, at: 2026-06-30T14:00:00Z }
```

```
verified:
```

```
  - { by: human:jsmith@acme, at: 2026-07-01T09:00:00Z }
```

```
status: stable
```

```
stale_after: 2026-12-31
```

```
sources:
```

```
  - id: revenue-policy
```

```
    resource: policies/revenue-recognition.md
```

```
    title: Revenue Recognition Policy (FY2026)
```

```
    author: human:jsmith@acme
```

```
    last_modified: 2026-06-15
```

```
---
```

```
​
```

```
# Computation
```

```
​
```

```
SELECT
```

```
  SUM(
```

```
    CASE
```

```
      WHEN o.currency = 'USD' THEN o.net_amount
```

```
      ELSE o.net_amount * fx.rate_to_usd
```

```
    END
```

```
  ) AS revenue_usd
```

```
FROM `acme.sales.orders` AS o
```

```
LEFT JOIN `acme.finance.fx_daily_rates` AS fx
```

```
  ON fx.currency = o.currency
```

```
  AND fx.rate_date = DATE(o.order_ts)
```

```
WHERE o.order_status = 'delivered'
```

```
  AND DATE_DIFF(CURRENT_DATE(), DATE(o.order_ts), DAY) >= 30
```

```
  AND EXTRACT(YEAR FROM o.order_ts) = @year
```

The agent may only fill the declared `parameters`; it must never author or edit the computation. A consumer runs the computation through the `executor`, which returns a `receipt` (here: a BigQuery `job_id`, the SQL that was actually executed, and the result). Then a deterministic, no-LLM `attester` inspects that receipt and returns a verdict: did the query that ran equal the sanctioned computation bound with the claimed parameters, and does the displayed value match the receipt's authoritative source? Because the comparison is mechanical, a rewritten query, a swapped computation file, or a mutated dependency fails the check.

In **acme\_retail:** the attester at attesters/sql\_equality.py canonicalizes both SQLs (strips comments, collapses whitespace, uppercases known keywords) and refuses to return ok if the canonical forms differ. A swapped table name, an added filter, a dropped JOIN all fail attestation. The consumer refuses to display the value when the verdict comes back false.

While we used BigQuery (and SQL) in this example, the abstraction is deliberately flexible. Attested computations can correspond to invoking semantic models (in Looker, AtScale, or other systems), querying structured knowledge graphs, or even making arbitrary API calls.

Crucially, OKF records the computation and how to check it; it never executes anything itself. And attestation is distinct from verification: `verified` confirms the definition still matches policy (slow, doc-level, stored in the bundle); attestation confirms a single run produced the value correctly (per-call, runtime, never stored in the bundle). A stale definition can still attest cleanly; a freshly-verified definition still needs attestation on every run. That's why both exist.

### What we're releasing with v0.2

As with v0.1, the reference implementations are deliberately proofs of concept; nothing about OKF requires them. Here’s what’s changing in the Github repo:

- **reference\_agent** now emits the provenance and trust families as it generates, so a freshly-minted bundle arrives with generated sources, and citations already in place.
- **The static visualizer** surfaces trust tier, status, and staleness alongside the concept graph, so these signals are visible, not just parseable.
- **Updated sample bundles** (GA4 e-commerce, Stack Overflow, Bitcoin, and the acme retail example used in this blog post) carry the v0.2 fields.
- **A Knowledge Catalog demo** shows a bundle round-tripping through Google Cloud's [Knowledge Catalog](https://cloud.google.com/products/knowledge-catalog) (formerly Dataplex): clean OKF on disk, trust and provenance signals preserved through the catalog and back.

### Compatibility

v0.2 is a minor version bump that is additive, backward-compatible, with two deliberate renames: `timestamp` is superseded by `generated.at`, and the body `# Citations` list is superseded by `sources`; in both cases a v0.2 consumer can fall back to the v0.1 form. A v0.1 bundle drops in unchanged.
---

# Appendix — Official OKF v0.1 → v0.2 Compatibility Notes

This appendix preserves the officially documented compatibility information from the public OKF release materials.

## v0.2 is backward-compatible

- v0.2 is an additive minor version.
- A v0.1 bundle remains consumable by a v0.2 consumer.
- The format still uses a directory of Markdown files with YAML frontmatter.
- `type` remains the only required frontmatter field.

## Official changes from v0.1

### Breaking renames

| v0.1 | v0.2 |
|------|------|
| `timestamp` | `generated.at` |
| Body `# Citations` list | `sources` frontmatter |

### Additive features

- `sources` (provenance)
- `generated`
- `verified`
- `status`
- `stale_after`
- `Attested Computation`
- `runtime`
- `parameters`
- `executor`
- `attester`
- `# Computation` conventional heading

## Reference implementations shipped with OKF

- BigQuery enrichment agent
- Static HTML graph visualizer
- Sample bundles (GA4, Stack Overflow, Bitcoin, Acme Retail)
- Knowledge Catalog ingestion support

## Official references

- Google Cloud — Introducing Open Knowledge Format (v0.1)
- Google Cloud — OKF v0.2 adds trust signals
- GoogleCloudPlatform/knowledge-catalog — OKF v0.2 SPEC.md
