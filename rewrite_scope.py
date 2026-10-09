import re

with open('docs/scope/scope.md', 'r') as f:
    content = f.read()

# Update At a glance table
table_pattern = re.compile(r'(\| # \| Feature \| Phase \| Status \|\n\|---\|---\|---\|---\|\n)(.*?)(?=\n## Brownfield enrollment)', re.DOTALL)

new_table_rows = """| A | Storage and database repositories | Existing | existing |
| B | File format adapters | Existing | existing |
| C | Model providers and environment settings | Existing | existing |
| D | Web application and document viewer shell | Existing | existing |
| E | Benchmark suite and test assets | Existing | existing |
| 1 | Contracts and identity envelope | Foundation | done |
| 2 | Workspace authorization and boundary isolation | Foundation | done |
| 3 | Snapshot registry, publication, and atomic deletion | Deferred | dropped |
| 4 | Core engine walking skeleton and benchmarks | Slice 1 | planned |
| 5 | PageIndex integration | Slice 2 | planned |
| 6 | OKF integration | Slice 3 | planned |
| 7 | Knowledge graph enrichment | Slice 4 | planned |
| 8 | LAYA bounded escalation | Slice 5 | planned |
| 9 | End to end interface integration | Slice 6 | planned |
| 10 | Production authentication, workspace membership, and session persistence | Slice 7 | planned |"""

content = table_pattern.sub(r'\1' + new_table_rows + '\n', content)

# Update feature 2 to include the honest gap note
feature2_note = """- [x] Document it: `/document workspace authorization and boundary isolation`
**Note:** Latest review (2026-10-09) is Blocked. Remaining gaps: test fixtures used in production routes, synchronous sleep blocks event loop in DynamoDB store, missing sys.path injection for test imports."""
content = content.replace("- [x] Document it: `/document workspace authorization and boundary isolation`", feature2_note)

# Replace the sections starting from 3 onwards
sections_pattern = re.compile(r'### 3\. Snapshot registry, publication, and atomic deletion.*?## References', re.DOTALL)

new_sections = """## Deferred

### 3. Snapshot registry, publication, and atomic deletion · needs a decision · GA
Implement CAS publication, baseline generation counters, active source version tracking, bounded manifest layout under DynamoDB limits, and immediate tombstone deletion barriers. (basis: Architecture Bible §3, §45, §47, §48, §65, §66, §67; docs/reference/DATABASE.md; docs/MIGRATION_MAP.md Traceability Matrix)
**Done when:** multi item atomic publication updates manifests with generation checks, reads never observe incomplete publications, and deletions block access immediately.
- [ ] Design it (spec): `/architect snapshot registry, publication, and atomic deletion`
*(Deferred: preserve design and dependencies; do not overwrite or renumber it)*

* **Advanced distributed scaling and optimization**: Multi node distributed partitioning, distributed worker clustering, and advanced cluster optimization (basis: Architecture Bible §8, §37, §56; docs/MIGRATION_MAP.md Traceability Matrix) · needs a decision · GA
* **Independent external security audit**: Formal third party compliance certification and independent external penetration testing (basis: docs/reference/SECURITY.md) · GA

## Slice 1: Core walking skeleton

### 4. Core engine walking skeleton and benchmarks · needs a decision
Make the next milestone a bounded core engine walking skeleton that proves the complete path works end to end. Include minimum readiness and consistent source/version bindings. Defer advanced distributed publication and cleanup infrastructure.
**Done when:** the skeleton supports one development workspace using existing identity and access checks; real CSV and text PDF ingestion with source/version provenance; full structured dataset processing without row truncation; typed, restricted lookup/filter/aggregation execution; BM25 and baseline vector retrieval with embeddings built at ingestion; reranking, evidence-backed answers, citations, and insufficient-evidence responses; and reproducible benchmarks for correctness, evidence support, coverage, token usage, ingestion time, and query latency.
- [ ] Design it (spec): `/architect core engine walking skeleton and benchmarks`

## Slice 2: PageIndex integration

### 5. PageIndex integration · needs a decision
Plan PageIndex integration as a subsequent, independently verifiable slice after the baseline works.
**Done when:** PageIndex is integrated into the retrieval pipeline and independently verified.
- [ ] Design it (spec): `/architect PageIndex integration`

## Slice 3: OKF integration

### 6. OKF integration · needs a decision
Plan OKF integration as a subsequent, independently verifiable slice after the baseline works.
**Done when:** OKF is integrated into the enrichment pipeline and independently verified.
- [ ] Design it (spec): `/architect OKF integration`

## Slice 4: Knowledge graph enrichment

### 7. Knowledge graph enrichment · needs a decision
Plan knowledge graph enrichment as a subsequent, independently verifiable slice after the baseline works.
**Done when:** the knowledge graph is integrated and independently verified.
- [ ] Design it (spec): `/architect knowledge graph enrichment`

## Slice 5: LAYA bounded escalation

### 8. LAYA bounded escalation · needs a decision
Plan LAYA integration for bounded heuristic and query expansion escalation when discovery finds no support.
**Done when:** LAYA is integrated as a subsequent, independently verifiable slice.
- [ ] Design it (spec): `/architect LAYA bounded escalation`

## Slice 6: End to end interface integration

### 9. End to end interface integration
Connect the refactored backend execution pipeline to the React web application in test workspace mode, updating the query input, citation links, and document viewer highlights. (basis: Architecture Bible §71, §72; docs/MIGRATION_MAP.md Reuse First; frontend/AGENTS.md)
**Done when:** users in the web interface can view live ingestion status, ask multi part queries in test workspace mode, click citations to jump to exact document locations, and see clean error or refusal messages.
- [ ] Build it: `/develop end to end interface integration`

## Slice 7: Production authentication and multi user workspaces

### 10. Production authentication, workspace membership, and session persistence · needs a decision · GA
Implement real authentication provider integration, user account management, workspace ownership and membership access controls, and persistent chat sessions prior to multi user release. (basis: Architecture Bible §3, §16, §46; docs/reference/SECURITY.md; docs/reference/API.md)
**Done when:** users can authenticate through real identity providers, create and manage workspaces with explicit membership permissions, and resume persistent chat sessions with full authorization enforcement.
- [ ] Design it (spec): `/architect production authentication, workspace membership, and session persistence`

## References"""

content = sections_pattern.sub(new_sections, content)

with open('docs/scope/scope.md', 'w') as f:
    f.write(content)
