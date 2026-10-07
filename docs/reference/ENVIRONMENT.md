# KRE Environment Contract

Environments:
`local`, `test`, `dev`, `staging`, `prod`

Config classes:
- auth/access
- storage
- model/provider (enforce embedding model compatibility: different embedding models may use separate collections OR separate named-vector configurations within one collection. Compatibility encompasses model identity/version, dimensions, normalization, and distance metric—not dimension alone. Never compare a query vector against an incompatible model's vectors. Compatible ranked candidate lists may be fused according to the RRF contract; raw incompatible vectors or similarity values are not mixed.)
- budgets
- allowed origins
- feature/enrichment flags

Never put secrets in source-controlled examples. Snapshot query semantics that depend on policy/config versions.
