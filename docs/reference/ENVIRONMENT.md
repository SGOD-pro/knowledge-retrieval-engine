# KRE Environment Contract

Environments:
`local`, `test`, `dev`, `staging`, `prod`

Config classes:
- auth/access
- storage
- model/provider (enforce strict dimension and embedding model compatibility; incompatible embeddings must not share collections)
- budgets
- allowed origins
- feature/enrichment flags

Never put secrets in source-controlled examples. Snapshot query semantics that depend on policy/config versions.

