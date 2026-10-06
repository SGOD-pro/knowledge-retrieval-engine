# KRE CI/CD Contract

```text
format/lint
→ type checks
→ unit/contract tests
→ integration/security
→ packaging/deployment checks
→ evaluation/benchmark gates
→ deploy
→ smoke test
→ promote or rollback
```

Block promotion on contract/security/package/smoke failures and approved benchmark regression thresholds.

Application rollback and capability snapshot publication are separate concerns; rollback must not create incompatible data references.
