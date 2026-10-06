# KRE Storage/Data Contract

## Roles
- Object storage: immutable raw sources and large artifacts.
- DynamoDB: workspace/source/version state, canonical metadata, capabilities/snapshots, structured lineage, OKF/relationship data where used.
- QdrantDB: versioned dense retrieval artifacts.
- Redis/ElastiCache: authorization/session/version-aware cache.

## Logical Entities
`Workspace`, `Source`, `SourceVersion`, `CanonicalEvidence`, `CapabilityArtifact`, `CapabilityManifest`, `KnowledgeMapEntry`, `Snapshot`, `Requirement`, `Query`, `DiscoveryCandidate`, `ExecutionRecord`, `Claim`, `Job`, `Checkpoint`, `CacheEntry`.

## Identity
Source-derived entities carry workspace/source/version. Query-derived records add query/snapshot/requirement identity.

## Deletion
Immediate tombstone/access barrier; physical cleanup may be asynchronous.
