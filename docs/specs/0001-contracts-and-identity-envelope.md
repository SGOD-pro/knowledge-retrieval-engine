# 0001. Contracts and Identity Envelope

**Date**: 2026-10-08
**Status**: Accepted

## Summary

This specification establishes shared data contracts for the Knowledge Retrieval Engine using frozen Pydantic models. It defines identity envelopes, canonical evidence records, capability artifacts, query contracts, structured execution results, and independent verification records. Every model is deeply immutable and strictly typed, eliminating unvalidated payload dictionaries, arbitrary values, and ambient keys. This guarantees that ingestion, indexing, retrieval, and execution components communicate through verified contracts without schema drift, unvalidated state transitions, or cross workspace data leakage.

## Context

The earlier implementation suffered from schema drift, ambient dictionary payloads, and mixed identity conventions across modules. Some services passed raw dictionaries with varying key names (such as mixing document identifiers with source identifiers). Crucial metadata like workspace boundaries, source versions, content hashes, and extraction pipeline versions were frequently lost or made optional.

Standard Python objects and dictionaries permit in place mutation. When collections inside shared records can be modified after creation, audit logs become unreliable and cached results risk silent corruption. Standard copy operations in data validation libraries can also bypass validators, allowing invalid states to leak into supposedly frozen structures. Furthermore, arbitrary or untyped payload structures weaken type safety, while informal text descriptions for capability coverage prevent automated range reasoning.

Unbounded table payloads and unvalidated client execution parameters also presented severe reliability risks. Passing entire spreadsheets or massive database tables as inline arrays within distributed messages causes memory spikes and serialization bottlenecks. Similarly, conflating source dataset references with query execution results risks mixing immutable ingestion facts with ephemeral query selections. Allowing client callers to supply custom budget counters, raw code, or arbitrary formula text creates critical execution vulnerabilities. Evaluating structured tables without full dataset completeness guarantees risks delivering misleading sample calculations as factual totals. Establishing frozen, strictly bounded contracts with rebuild safe identities, machine readable coverage, server controlled execution policies, full dataset execution semantics, and snapshot pinned verification records resolves these problems and provides a reliable type foundation for all refactored slices.

## Requirements

**User stories**:
* As a backend developer, I want deeply immutable, strictly typed Pydantic contracts with validated state transitions so that pipeline components communicate without run time schema errors, untyped values, or accidental mutations.
* As a retrieval engineer, I want canonical evidence to carry rebuild safe identities, exact visual coordinates with top left origin and preserved floating point precision, typed qualifiers, and extraction quality metrics so that answers reference verified sources with full provenance.
* As an execution engineer, I want large tables represented by registered dataset references evaluated over complete datasets under server controlled execution policies, streaming batch sizes separated from scan ceilings, and raw source formulas kept distinct from executable abstract syntax trees so that calculations remain exact, bounded, and secure.
* As a verification engineer, I want query verification decisions pinned to snapshots and recorded in separate verification records carrying verifier type, version, and attempt numbers in their storage identity rather than mutating source evidence so that source extraction remains clean and auditable.
* As a security engineer, I want trusted authorization context injected strictly by server middleware and validated against every contract envelope so that cross workspace data leakage and client parameter tampering are prevented across all storage and execution layers.

**Acceptance criteria**:
* **AC-1 (Flat Identity Envelope and Server Only Authorization Context Injection)**: `IdentityEnvelope` serializes identity attributes flat at the top level in JSON without an unapproved nested envelope wrapper. Authorization context is governed by a server only `TrustedAuthContext` (containing `authorized_workspace_id`, `principal_id`, `principal_roles`, and `access_policy_version`), injected exclusively by server authentication middleware and strictly forbidden from client request payloads. The envelope validates `workspace_id` against `authorized_workspace_id`, rejecting empty strings, whitespace, and client attempts to forge or override authorization contexts. It enforces lifecycle requirements: ingestion evidence creation requires `source_id` and positive `source_version` while rejecting `query_id` and `requirement_id`; query contracts require `query_id` and `snapshot_id`; verification records require `query_id`, `snapshot_id`, `requirement_id`, and `evidence_id`. It rejects ambient keys and conflicting identifiers.
* **AC-2 (Deep Immutability, Elimination of Untyped Payloads, and Typed Qualifiers)**: Every contract model enforces `model_config = ConfigDict(frozen=True, extra="forbid")`. All collection fields use immutable `tuple` types instead of mutable lists or dictionaries. Untyped values are strictly eliminated: `TablePayload.rows` is typed as `tuple[tuple[ScalarValue, ...], ...]` where `ScalarValue` is `str | int | float | bool | None`. Qualifiers are typed as `tuple[QualifierItem, ...]` where `QualifierItem` holds a non empty string key and a scalar value (`str | int | float | bool | None`). Any attempt to modify an attribute or mutate a collection raises a run time error.
* **AC-3 (Validated State Transitions and Revalidation Enforcement)**: State transitions never mutate model instances in place and never use unvalidated shallow copying. Every lifecycle transition must execute complete validation through explicit transition constructors or factory methods that revalidate all model invariants, rejecting contradictory states (such as marking an artifact complete with partial coverage or retaining a failure state while setting readiness to true).
* **AC-4 (Rebuild Safe Deterministic and Immutable Build Identifiers)**: Identifiers are deterministic, collision resistant, and safe against pipeline rebuilds and re indexing. Evidence identifiers bind source identity, source version, pipeline extraction version, content hash digest, and chunk index: `f"ev_{source_id}_v{source_version}_{pipeline_version}_{content_digest}_{chunk_index}"`. Capability artifacts bind capability type, source identifier, source version, artifact build version, and immutable build run hash: `f"cap_{capability}_{source_id}_v{source_version}_{artifact_version}_{build_id_prefix}"`. Snapshot identifiers bind workspace and manifest generation: `f"snap_{workspace_id}_gen{manifest_generation}"`. Verification storage identifiers bind query, snapshot, requirement, evidence, verifier type, verifier version, and attempt number: `f"vr_{query_id}_{snapshot_id}_{requirement_id}_{evidence_id}_{verifier_type}_{verifier_version}_att{attempt_number}"`. Dataset references bind source, version, table, and schema digest: `f"ds_{source_id}_v{source_version}_{table_id}_{schema_digest}"`. Execution results bind query, requirement, and execution attempt: `f"exec_{query_id}_{requirement_id}_att{attempt_number}"`.
* **AC-5 (Complete Multi Format Locator Union, Top Left Origin, and Coordinate Precision Preservation)**: `Location` is defined as a discriminated union on `type` (`document`, `table`, `slide`, `section`). Table locators support dual visual coordinates (`page` as positive integer and normalized `bbox` as a 4 element tuple of floats `(x0, y0, x1, y1)`) and structural coordinates (`table_id`, `row_id`, `column_id`, `sheet_name`). All bounding box coordinates use a standard top left origin where `(0.0, 0.0)` is the top left corner, `x` increases rightward, and `y` increases downward, satisfying `0.0 <= x0 < x1 <= 1.0` and `0.0 <= y0 < y1 <= 1.0`. All coordinates preserve full 64 bit floating point precision without arbitrary rounding or lossy truncation. Document, slide, and section locators provide stable coordinates across PDF, DOCX, PPTX, CSV, and XLSX formats.
* **AC-6 (Bounded Payloads, Byte Caps, Dataset References, and Separate Execution Results)**: `ContentPayload` is defined as a discriminated union on `kind` (`text`, `cell`, `table`, `dataset_ref`, `asset`, `formula`). Every payload enforces strict byte and character caps: `TextPayload` limits text to 32,768 characters (approximately 64 KB UTF-8); `CellPayload` limits string values to 4,096 characters; `TablePayload` enforces a maximum of 50 inline rows, 50 columns, and 262,144 bytes (256 KB) total serialized payload size. Tables exceeding these limits must use `DatasetRefPayload` carrying a pure source reference `DatasetReference` (without query execution status). Total serialized payload size on any single `CanonicalEvidence` instance must not exceed 262,144 bytes (256 KB). Query execution completion status (`COMPLETE_FOR_SELECTION`, `EMPTY_SELECTION`, `INCOMPLETE_EXECUTION`) is captured strictly in a separate query plane `StructuredExecutionResult` model, enforcing 100 percent evaluation over matching rows and prohibiting silent partial sampling.
* **AC-7 (Asset Provenance and Separation of Raw Formulas from Executable ASTs)**: `AssetPayload` stores stable storage URIs (maximum 1,024 characters) without inline binary data, and explicitly distinguishes raw extracted source captions from AI generated descriptions using boolean flag `is_ai_generated_description`. `FormulaPayload` represents raw extracted spreadsheet formulas (`raw_expression`, `cached_value`, `eval_status`) and is permitted for table cell and structured record evidence types. Executable formula execution at query time uses a separate, server controlled `ExecutableFormulaAST` with allowlisted operations, strictly prohibiting raw formula string evaluation, Python `eval()`, or raw SQL.
* **AC-8 (Extraction Quality Independence)**: `CanonicalEvidence` represents extracted source evidence with extraction quality (`ExtractionQuality` containing `status` from `VALID`, `DEGRADED`, `REJECTED` and `confidence` float between 0.0 and 1.0) and extraction provenance (`EvidenceProvenance` containing `content_hash`, `extraction_pipeline_version`, and optional `parser_element_id`). It never records query semantic verification.
* **AC-9 (Snapshot Pinned Verification Records with Storage Identity Distinguishing Verifier Type, Version, and Attempt)**: Query semantic verification produces a distinct, immutable `VerificationRecord` strictly pinned to `snapshot_id`, linking `query_id`, `snapshot_id`, `requirement_id`, and `evidence_id`. The record captures `verifier_version`, `verifier_type` (`mechanical`, `encoder`, `generative`), `attempt_number` (bounded by `max_verifier_attempts = 3`), `execution_time_ms`, `support_status` (`ASSERTED`, `HYPOTHESIZED`, `CONTRADICTED`, `UNVERIFIED`), numeric confidence score (0.0 to 1.0), textual verification rationale, and UTC timestamp. The storage identity `verification_id` explicitly embeds verifier type, verifier version, and attempt number to prevent collision across retries and verifier pipelines. Source evidence instances remain unmodified.
* **AC-10 (Comprehensive Locator and Payload Compatibility Validation)**: A model validator on `CanonicalEvidence` validates compatibility across `EvidenceType`, `Location`, and `ContentPayload`: `text_chunk` requires `TextPayload` with `DocumentLocation`, `SectionLocation`, or `SlideLocation`; `table_cell` requires `CellPayload` or `FormulaPayload` with `TableLocation`; `table_slice` requires `TablePayload` or `DatasetRefPayload` with `TableLocation`; `figure_asset` requires `AssetPayload` with `DocumentLocation` or `SlideLocation`; `structured_record` requires `TextPayload`, `CellPayload`, or `FormulaPayload` with `SectionLocation` or `TableLocation`. Incompatible pairings are rejected with validation errors.
* **AC-11 (Operator Specific Predicates, Conjunction Semantics, Requirement Uniqueness, Mixed Query Hybrid Resolution, and Server Execution Policy with Streaming Batches)**: `SelectionPredicate` enforces operator specific value shapes: `eq` and `neq` require a single scalar primitive; `gt`, `gte`, `lt`, and `lte` require an ordered scalar; `in` and `not_in` require a non empty tuple of scalars; `between` requires a 2 element tuple where lower bound is less than or equal to upper bound; `contains` requires a non empty string. Multiple predicates within a selection combine strictly via logical AND (conjunction). `RequirementItem` validates that `operation` is valid for the declared `intent`. `QueryContract` enforces requirement uniqueness (`requirement_id` must be unique across the requirements tuple), resolves `resolved_answer_mode` through server planner policy (enforcing `deterministic` for pure lookups and calculations, `narrative` for pure summarization, and `hybrid` whenever requirements contain a mix of exact/computational and narrative intents) regardless of client preferences, and references a server controlled `ServerExecutionPolicy` (via `budget_policy_id`). `ServerExecutionPolicy` separates streaming `execution_batch_size` (such as 5,000 rows) from total scan ceiling (`max_execution_scan_rows`), memory limit (`max_execution_memory_mb`), and timeout limit (`execution_timeout_ms`), preventing client callers from injecting arbitrary resource limits, raw SQL, or unrestricted code execution.
* **AC-12 (Partition Aware Coverage Ranges, Unknown Total Readiness Rules, and Scoped Readiness)**: `CapabilityArtifact` defines typed `ArtifactCoverage` using machine readable structured partition ranges (`CoverageRange` containing `unit_type` from `page`, `chunk`, `sheet`, `row`, `slide`, `section`, `start_index`, `end_index`, and optional `partition_id`). Ranges enforce closed inclusive bounds `[start_index, end_index]` where `start_index <= end_index`, with 1 indexed numbering for document pages, slides, and sheets, and 0 indexed numbering for chunks and rows. `ArtifactCoverage` supports unknown total units during active streaming (`total_units: int | None = None`); when `total_units` is unknown, `is_complete` cannot be True. `ArtifactCoverage` validates that `covered_ranges` are sorted and non overlapping per partition, `exclusion_ranges` are disjoint from `covered_ranges`, and range unit counts strictly equal `processed_count` and `total_units`. Scoped readiness records specific completed partitions (`ready_partitions: tuple[str, ...]`), enabling query planners to dispatch to verified partitions while unready partitions trigger baseline fallbacks. `ArtifactFailureState` records typed failure details (`error_code`, `message`, `timestamp`, `retryable`, `details`).
* **AC-13 (Explicit Serialization and Deserialization Coercion Rules)**: All contract models support deterministic JSON serialization via `.model_dump(mode="json")` and deserialization via `.model_validate_json(...)`. Pre validators automatically coerce JSON arrays into immutable Python tuples for all collection attributes. Identity envelope fields serialize flat at the top level without an intermediate container key. Floating point coordinates preserve full precision without rounding, and timestamps serialize as ISO 8601 UTC strings.
* **AC-14 (Query Plane Structured Execution Result Contract and Verification Invariants)**: Tabular execution against registered datasets emits a frozen `StructuredExecutionResult` carrying `execution_id`, `evidence_id`, `query_id`, `snapshot_id`, `requirement_id`, `attempt_number` (1 indexed integer >= 1), `source_id`, `source_version`, `table_id`, `operation`, `metric`, `selection`, `records_examined`, `records_matched`, `selection_digest`, `lineage_ref`, `result_value`, `result_unit`, `coverage_status`, and `executed_at`. Invariants require `0 <= records_matched <= records_examined`, `attempt_number >= 1`, and strict `coverage_status` semantics (`COMPLETE_FOR_SELECTION` requires complete unbudgeted row evaluation, `EMPTY_SELECTION` requires zero matches in full scan, `INCOMPLETE_EXECUTION` signals resource or scan cutoff). It captures full dataset execution proof outside of reciprocal rank fusion, keeping execution outcomes distinct from ingestion dataset references.

## Options considered

### Option 1: Pydantic V2 frozen models with flat identity serialization, discriminated unions, and deeply immutable tuples

Implement dedicated Pydantic V2 models configured with `frozen=True` and `extra="forbid"`. Use discriminated unions for polymorphic locators and content payloads. Use immutable tuples for all collection fields, replace untyped values with strict scalar unions, and type qualifiers as structured key value items. Serialize identity fields flat to match technical specification schemas while keeping internal validation modular. Separate raw extracted spreadsheet formulas from executable abstract syntax trees. Separate source dataset references from query specific structured execution results. Separate source extraction evidence from snapshot pinned query verification records with verifier specific storage keys. Enforce validated state transitions through factory methods. Inject authorization context strictly on the server.

**Pros**:
* Direct compatibility with FastAPI, automatic type validation, and high performance compiled validation in Python 3.12.
* Eliminates ambient dictionary keys and accidental run time mutation across pipeline boundaries.
* Guarantees deep immutability through tuple collections, scalar unions, and frozen child models.
* Rebuild safe identifiers prevent state collisions across ingestion runs and pipeline upgrades.
* Machine readable coverage ranges enable programmatic gap detection without text parsing, handling partitions and unknown streaming totals.
* Server controlled execution policies separate streaming batch sizes from total resource ceilings, preventing client tampering and unbounded queries.
* Preserves raw formula extraction provenance while securing execution through restricted ASTs.
* Separating `StructuredExecutionResult` from `DatasetRefPayload` cleanly isolates query execution outcomes from static source references.
* Embedding verifier type, version, and attempt number in verification storage identities prevents retry collisions and verifier race conditions.
* Matches the exact JSON structure specified in the architecture documentation.

**Cons**:
* Requires converting legacy dictionary data into strict models before processing.
* Strict byte and row bounding requires parsers to emit dataset references for large files.

### Option 2: Python standard dataclasses with dictionary payloads and separate validator functions

Retain standard Python frozen dataclasses and allow payload fields to carry free form dictionaries, relying on external functions for validation.

**Pros**:
* Minimal initial boilerplate and fast prototyping.

**Cons**:
* No run time type enforcement or automatic JSON parsing validation.
* Reintroduces ambient keys and untracked dictionary drift between services.
* Does not recursively freeze nested dictionaries or lists, leaving mutability loopholes.
* Permits unvalidated shallow copying and untyped values.

### Option 3: Protocol Buffers with compiled Python classes

Define contracts in Protocol Buffers and generate Python classes using the protocol buffer compiler.

**Pros**:
* Language agnostic schema definition.

**Cons**:
* Adds external compilation tooling and unnecessary complexity for a pure Python backend.
* Awkward mapping to native Python tuple structures, discriminated unions, and FastAPI request validation.

## Decision

**Chosen option**: Option 1: Pydantic V2 frozen models with flat identity serialization, discriminated unions, and deeply immutable tuples.

All cross module contracts will be implemented as frozen, strictly validated Pydantic V2 models inside a dedicated `backend/src/schemas/contracts/` package.

## Rationale

Pydantic V2 provides compiled schema validation that integrates directly with FastAPI and application settings. By setting `frozen=True` and `extra="forbid"`, the engine eliminates accidental in memory mutations and rejects unexpected dictionary keys that caused bugs in earlier implementations. Using immutable `tuple` types rather than mutable `list` or `dict` structures closes the deep immutability loophole, guaranteeing that nested collections cannot be modified in place. Eliminating untyped values with explicit scalar unions guarantees strict static and run time type guarantees.

Discriminated unions allow precise locator and payload representations across PDF, DOCX, PPTX, CSV, and XLSX without degrading to untyped dictionaries. Distinguishing raw extracted spreadsheet formulas (`FormulaPayload`) from server controlled executable expressions (`ExecutableFormulaAST`) allows formula extraction provenance to be preserved without exposing execution engines to arbitrary code injection. Moving query execution status out of `DatasetRefPayload` into `StructuredExecutionResult` keeps source extraction representations pure, preventing query specific selection outcomes from polluting baseline evidence.

Separating `CanonicalEvidence` (which captures immutable extraction fidelity) from `VerificationRecord` (which captures query semantic evaluation pinned to a specific snapshot, verifier type, version, and attempt number) preserves evidence purity. Source evidence is never mutated to record query decisions, keeping provenance tamper proof. Embedding verifier type, version, and attempt in verification storage identities enables comprehensive retry tracking without key collisions. Machine readable coverage ranges allow query planners to verify capability completeness with simple range arithmetic across partitions and handle unknown streaming totals. Server injected authorization contexts protect tenant boundaries against client spoofing.

## Feature design

**Data model sketch**:

```text
ScalarPrimitive = str | int | float | bool
OrderedScalar = int | float | str
ScalarValue = ScalarPrimitive | None
CoverageUnit = "page" | "chunk" | "sheet" | "row" | "slide" | "section"
ExecutionCoverageStatus = "COMPLETE_FOR_SELECTION" | "EMPTY_SELECTION" | "INCOMPLETE_EXECUTION"
FormulaEvalStatus = "CACHED_ONLY" | "AST_PARSED" | "EVAL_FAILED" | "UNSUPPORTED_SYNTAX"
VerifierType = "mechanical" | "encoder" | "generative"

QualifierItem
├── key: str (non empty)
└── value: ScalarValue

TrustedAuthContext (Server only, injected by middleware, forbidden in client input)
├── authorized_workspace_id: str (required, non empty)
├── principal_id: str (required, non empty)
├── principal_roles: tuple[str, ...] (default ("viewer",))
└── access_policy_version: str (required, non empty)

ServerExecutionPolicy
├── policy_id: str (such as "query-default-v3")
├── max_query_llm_calls: int (positive, default 2)
├── max_query_rerank_calls: int (positive, default 2)
├── max_verifier_attempts: int (positive, default 3)
├── execution_batch_size: int (positive, default 5000 rows per streaming batch)
├── max_execution_scan_rows: int (positive, default 50000 rows total scan ceiling)
├── max_execution_memory_mb: int (positive, default 256)
├── execution_timeout_ms: int (positive, default 5000)
├── null_policy: str ("error", "zero", "null", default "error")
└── formula_evaluation_mode: str ("validated_cached_values", "ast_evaluator")

IdentityEnvelope (Mixin / Base)
├── workspace_id: str (required, non empty, non whitespace, validated against TrustedAuthContext)
├── query_id: str | None
├── source_id: str | None
├── source_version: int | None (positive integer when present)
├── snapshot_id: str | None
└── requirement_id: str | None

Location (Discriminated Union on "type")
├── DocumentLocation (type="document")
│   ├── page: int (positive, 1 indexed)
│   ├── bbox: tuple[float, float, float, float] | None (top left origin, full precision 64 bit floats, 0.0 to 1.0)
│   ├── element_id: str | None
│   └── section_path: tuple[str, ...]
├── TableLocation (type="table")
│   ├── page: int | None (positive, 1 indexed)
│   ├── bbox: tuple[float, float, float, float] | None (top left origin, full precision 64 bit floats, 0.0 to 1.0)
│   ├── table_id: str
│   ├── row_id: str | int | None
│   ├── column_id: str | None
│   └── sheet_name: str | None
├── SlideLocation (type="slide")
│   ├── slide_number: int (positive, 1 indexed)
│   ├── shape_id: str | None
│   └── bbox: tuple[float, float, float, float] | None (top left origin, full precision 64 bit floats, 0.0 to 1.0)
└── SectionLocation (type="section")
    ├── heading: str
    ├── paragraph_index: int (non negative, 0 indexed)
    └── section_path: tuple[str, ...]

DatasetReference
├── source_id: str
├── source_version: int (positive)
├── registered_file_id: str
├── table_id: str
├── total_rows: int (non negative)
├── total_columns: int (non negative)
└── schema_hash: str (sha256 digest)

ContentPayload (Discriminated Union on "kind")
├── TextPayload (kind="text")
│   ├── text: str (max 32768 chars)
│   └── token_count: int | None
├── CellPayload (kind="cell")
│   ├── raw_value: ScalarValue (max 4096 chars if string)
│   ├── normalized_number: float | None
│   └── formatted_string: str | None (max 4096 chars)
├── TablePayload (kind="table")
│   ├── table_id: str
│   ├── headers: tuple[str, ...] (max 50 cols)
│   ├── rows: tuple[tuple[ScalarValue, ...], ...] (max 50 rows, row len == header len)
│   ├── row_count: int
│   └── col_count: int
├── DatasetRefPayload (kind="dataset_ref", pure source reference without query execution status)
│   ├── dataset_ref: DatasetReference
│   ├── total_rows: int
│   ├── total_columns: int
│   └── schema_hash: str
├── AssetPayload (kind="asset")
│   ├── asset_id: str
│   ├── mime_type: str
│   ├── storage_ref: str (URI or storage path, max 1024 chars, no inline binary)
│   ├── ai_description: str | None (max 4096 chars)
│   └── is_ai_generated_description: bool (default False)
└── FormulaPayload (kind="formula")
    ├── raw_expression: str (raw extracted spreadsheet formula, max 2048 chars, no arbitrary code)
    ├── cached_value: ScalarValue
    └── eval_status: FormulaEvalStatus (default "CACHED_ONLY")

ExecutableFormulaAST (Query / Execution plane only, constructed by server execution engine)
├── operator: str ("sum", "avg", "min", "max", "count", "add", "subtract", "multiply", "divide", "ratio", "difference")
├── operands: tuple[str | int | float, ...] (column references or numeric constants)
└── null_policy: str ("error", "zero", "null")

CanonicalEvidence
├── workspace_id: str (flat, non empty, matches TrustedAuthContext)
├── source_id: str (flat, non empty)
├── source_version: int (flat, positive)
├── evidence_id: str (rebuild safe: f"ev_{source_id}_v{source_version}_{pipeline_version}_{content_digest}_{chunk_index}")
├── type: EvidenceType (text_chunk, table_cell, table_slice, figure_asset, structured_record)
├── location: Location (discriminated union)
├── content: ContentPayload (discriminated union, total serialized size <= 262144 bytes)
├── unit: str | None
├── qualifiers: tuple[QualifierItem, ...] (typed key value collection)
├── provenance: EvidenceProvenance
│   ├── content_hash: str (sha256:...)
│   ├── extraction_pipeline_version: str
│   └── parser_element_id: str | None
└── quality: ExtractionQuality
    ├── status: QualityStatus (VALID, DEGRADED, REJECTED)
    └── confidence: float (0.0 to 1.0)

StructuredExecutionResult (Query plane contract, execution outcome for tabular queries)
├── workspace_id: str (flat, non empty, matches TrustedAuthContext)
├── query_id: str (non empty)
├── snapshot_id: str (pinned snapshot generation)
├── requirement_id: str (non empty)
├── attempt_number: int (positive, 1 indexed, default 1)
├── execution_id: str (f"exec_{query_id}_{requirement_id}_att{attempt_number}")
├── evidence_id: str (f"execution:{workspace_id}:{execution_id}")
├── source_id: str (non empty)
├── source_version: int (positive)
├── table_id: str (non empty)
├── operation: str ("sum", "avg", "count", "min", "max", "exact_match", "ratio", "difference")
├── metric: str | None
├── selection: tuple[SelectionPredicate, ...] (conjunction via logical AND)
├── records_examined: int (non negative, records_examined >= records_matched)
├── records_matched: int (non negative)
├── selection_digest: str (sha256 digest of matching record IDs)
├── lineage_ref: str (durable storage reference to detailed execution trace)
├── result_value: ScalarValue
├── result_unit: str | None
├── coverage_status: ExecutionCoverageStatus ("COMPLETE_FOR_SELECTION", "EMPTY_SELECTION", "INCOMPLETE_EXECUTION")
└── executed_at: str (ISO 8601 UTC timestamp)

VerificationRecord
├── verification_id: str (rebuild safe: f"vr_{query_id}_{snapshot_id}_{requirement_id}_{evidence_id}_{verifier_type}_{verifier_version}_att{attempt_number}")
├── workspace_id: str (non empty, matches TrustedAuthContext)
├── query_id: str (non empty)
├── snapshot_id: str (required, non empty, pinned snapshot generation)
├── requirement_id: str (non empty)
├── evidence_id: str (non empty)
├── verifier_version: str (such as "semantic-verifier-v2" or "mechanical-verifier-v1")
├── verifier_type: VerifierType ("mechanical", "encoder", "generative")
├── attempt_number: int (positive, 1 indexed, <= max_verifier_attempts)
├── execution_time_ms: int (non negative)
├── support_status: SupportStatus (ASSERTED, HYPOTHESIZED, CONTRADICTED, UNVERIFIED)
├── confidence_score: float (0.0 to 1.0)
├── verification_rationale: str (max 4096 chars)
└── verified_at: str (ISO 8601 UTC timestamp)

CoverageRange
├── unit_type: CoverageUnit ("page", "chunk", "sheet", "row", "slide", "section")
├── start_index: int (non negative, start <= end, 1 indexed for page/slide/sheet, 0 indexed for chunk/row)
├── end_index: int (non negative, closed inclusive bound)
└── partition_id: str | None (optional sheet name, chapter, or date partition)

ArtifactCoverage
├── unit_type: CoverageUnit
├── covered_ranges: tuple[CoverageRange, ...] (sorted, non overlapping per partition)
├── exclusion_ranges: tuple[CoverageRange, ...] (sorted, non overlapping, disjoint from covered)
├── processed_count: int (non negative)
├── total_units: int | None (non negative when known; None during active unindexed streaming)
├── is_complete: bool (formal completeness proof: total_units is not None, processed_count == total_units, empty exclusions, sorted non overlapping closed ranges covering all partition units with zero gaps)
└── ready_partitions: tuple[str, ...] (explicit partition scopes ready for routing)

ArtifactFailureState
├── error_code: str
├── message: str (max 2048 chars)
├── timestamp: str (ISO 8601 UTC timestamp)
├── retryable: bool
└── details: tuple[tuple[str, str], ...]

CapabilityArtifact
├── workspace_id: str (flat, non empty, matches TrustedAuthContext)
├── source_id: str (flat, non empty)
├── source_version: int (flat, positive)
├── artifact_id: str (rebuild safe: f"cap_{capability}_{source_id}_v{source_version}_{artifact_version}_{build_id_prefix}")
├── capability: CapabilityType (text_search, vector_search, table_execution, page_index, knowledge_graph)
├── ready: bool
├── coverage: ArtifactCoverage
├── artifact_version: str (such as "bm25-v7")
├── pipeline_version: str (such as "index-v3")
├── build_id: str (immutable build run identifier)
└── failure_state: ArtifactFailureState | None

SelectionPredicate (Operator specific typed value shapes)
├── field: str
├── operator: SelectionOperator (eq, neq, gt, gte, lt, lte, in, not_in, between, contains)
└── value: ScalarPrimitive | tuple[ScalarPrimitive, ...] | tuple[OrderedScalar, OrderedScalar]

RequirementTarget
├── dataset: str | None
└── metric: str | None

EvidenceRequirement
├── kind: str
├── locator: Location | None
└── selection: tuple[SelectionPredicate, ...]

RequirementItem
├── requirement_id: str (must be unique within a QueryContract)
├── intent: RequirementIntent (lookup, aggregate, compare, summarize, filter)
├── operation: OperationType | None (sum, avg, count, min, max, exact_match, ratio, difference)
├── target: RequirementTarget | None
├── selection: tuple[SelectionPredicate, ...] (predicates combine via logical AND)
└── required_evidence: tuple[EvidenceRequirement, ...]

QueryContract
├── query_id: str
├── workspace_id: str (matches TrustedAuthContext)
├── snapshot_id: str (pinned snapshot generation)
├── requirements: tuple[RequirementItem, ...] (at least 1 item, all requirement_ids unique)
├── requested_answer_mode: AnswerMode | None (optional client preference: deterministic, narrative, hybrid)
├── resolved_answer_mode: AnswerMode (mandatory server/planner resolved mode: deterministic for pure computation/lookup, narrative for pure summarize, hybrid for mixed requirements)
├── policy_resolution_rationale: str
└── budget_policy_id: str (references server controlled ServerExecutionPolicy, default "query-default-v3")
```

**Validated state transitions**:

Because models are deeply immutable, state transitions produce new immutable instances. In Pydantic V2, standard `.model_copy(update={...})` does not run validators by default and can bypass integrity checks. Therefore, contract models prohibit bare unvalidated shallow copy operations. All transitions execute complete validation through explicit transition factory methods:

```text
1. Ingestion Extraction Transition:
   Raw document input -> Parser extraction -> Compute content digest
   CanonicalEvidence.create_from_extraction(
       auth_context=server_injected_auth_context,
       source_id=source_id,
       source_version=source_version,
       pipeline_version="parse-v5",
       chunk_index=chunk_index,
       location=location,
       content=content,
       qualifiers=qualifiers,
       provenance=provenance,
       quality=quality
   )
   (Validates locator and payload compatibility, bounds payload bytes, verifies rebuild safe evidence ID)

2. Capability Indexing Lifecycle Transitions:
   Initial state:
   artifact = CapabilityArtifact.create_initial(
       auth_context=server_injected_auth_context,
       source_id=source_id,
       source_version=source_version,
       capability="text_search",
       artifact_version="bm25-v7",
       pipeline_version="index-v3",
       build_id=build_id,
       initial_coverage=initial_coverage
   )  # ready=False, failure_state=None

   Progress update:
   artifact = artifact.transition_coverage(updated_coverage)
   (Revalidates range arithmetic: sum of covered ranges must match processed count; unknown total blocks is_complete)

   Scoped readiness update:
   artifact = artifact.transition_scoped_readiness(ready_partitions=("Sheet:Sales",))
   (Enables planner to route to verified partitions before entire multi partition source finishes)

   Success completion:
   ready_artifact = artifact.transition_to_ready(complete_coverage)
   (Validates that ready=True, is_complete=True, failure_state is None, total_units is not None, processed_count == total_units)

   Failure recording:
   failed_artifact = artifact.transition_to_failed(failure_state)
   (Validates that ready=False, failure_state is not None, error details are preserved)

3. Query Intake and Planning Transition:
   Client query request -> Server authentication middleware injects TrustedAuthContext
   Query Planner analyzes intents and requirements -> Enforces requirement uniqueness and conjunction semantics
   Query Planner resolves answer mode based on policy -> Produces QueryContract
   query_contract = QueryContract.create_planned(
       auth_context=server_injected_auth_context,
       query_id=query_id,
       snapshot_id=snapshot_id,
       requirements=requirements,
       requested_answer_mode=client_request.answer_mode,
       server_policy=server_execution_policy
   )
   (Validates requirements uniqueness, enforces resolved_answer_mode="deterministic" for exact aggregations, "narrative" for pure summarization, and "hybrid" for mixed queries)

4. Structured Table Execution Transition (Slice 3 interface):
   Execution tool receives registered DatasetReference and SelectionPredicates
   Evaluates over 100 percent of matching rows in streaming batches bounded by execution_batch_size
   execution_result = StructuredExecutionResult.create_result(
       auth_context=server_injected_auth_context,
       query_id=query.query_id,
       snapshot_id=query.snapshot_id,
       requirement_id=req.requirement_id,
       execution_id=execution_id,
       attempt_number=1,
       dataset_ref=dataset_ref,
       operation="sum",
       metric="revenue",
       selection=req.selection,
       records_examined=records_examined,
       records_matched=records_matched,
       selection_digest=selection_digest,
       lineage_ref=lineage_ref,
       result_value=184.2,
       result_unit="EUR_million",
       coverage_status=ExecutionCoverageStatus.COMPLETE_FOR_SELECTION,
       executed_at=current_utc_timestamp()
   )
   (Emits structured execution outcome outside of reciprocal rank fusion; dataset reference remains unchanged)

5. Query Verification Transition:
   Candidate evidence evaluated against RequirementItem
   verification_record = VerificationRecord.create_verified(
       auth_context=server_injected_auth_context,
       query_id=query.query_id,
       snapshot_id=query.snapshot_id,
       requirement_id=req.requirement_id,
       evidence_id=evidence.evidence_id,
       verifier_version="semantic-verifier-v2",
       verifier_type=VerifierType.GENERATIVE,
       attempt_number=1,
       execution_time_ms=142,
       support_status=SupportStatus.ASSERTED,
       confidence_score=0.99,
       rationale="Cell value Europe matches filter and year 2024 with reported revenue 184.2.",
       verified_at=current_utc_timestamp()
   )
   (Pinned to snapshot_id, embeds verifier type, version, and attempt in storage identity; source evidence unchanged)
```

**API surface**:

Package directory: `backend/src/schemas/contracts/`
* `envelope.py`: `IdentityEnvelope`, `TrustedAuthContext`, and authorization validation helpers.
* `location.py`: `Location` discriminated union (`DocumentLocation`, `TableLocation`, `SlideLocation`, `SectionLocation`).
* `payload.py`: `ContentPayload` discriminated union (`TextPayload`, `CellPayload`, `TablePayload`, `DatasetRefPayload`, `AssetPayload`, `FormulaPayload`), `DatasetReference`, `ScalarValue`, `QualifierItem`, `ExecutableFormulaAST`, and operator specific predicate types.
* `evidence.py`: `CanonicalEvidence`, `EvidenceType`, `EvidenceProvenance`, and `ExtractionQuality`.
* `execution.py`: `StructuredExecutionResult` and `ExecutionCoverageStatus`.
* `verification.py`: `VerificationRecord`, `SupportStatus`, and `VerifierType`.
* `capability.py`: `CapabilityArtifact`, `CapabilityType`, `CoverageRange`, `CoverageUnit`, `ArtifactCoverage`, and `ArtifactFailureState`.
* `query.py`: `QueryContract`, `RequirementItem`, `RequirementIntent`, `OperationType`, `SelectionPredicate`, `SelectionOperator`, `RequirementTarget`, `EvidenceRequirement`, and `ServerExecutionPolicy`.
* `__init__.py`: Package root re exporting all primary types.

| Class / Module | Purpose | Key inputs | Key outputs | Invariants |
|---|---|---|---|---|
| `IdentityEnvelope` | Base identity validation | `workspace_id`, optional IDs, `TrustedAuthContext` | Validated envelope | Non empty `workspace_id`, matches server auth context, flat JSON serialization |
| `Location` union | Precise document coordinates | Format coordinates | Validated location | Top left origin, normalized `bbox` 64 bit floats between 0.0 and 1.0, preserved precision |
| `ContentPayload` union | Bounded typed content | Extracted content | Validated payload | No untyped values, `TablePayload` bounded to 50 rows and 256 KB, raw formula distinct from AST |
| `CanonicalEvidence` | Immutable source evidence | Envelope, Location, Content, Quality, Qualifiers | Frozen evidence | Rebuild safe ID, type location payload compatibility, typed qualifiers, max 256 KB total |
| `StructuredExecutionResult` | Tabular query execution proof | Query ID, Snapshot ID, Requirement ID, Selection | Frozen execution record | 100 percent evaluation over matching rows, explicit coverage status, durable lineage |
| `VerificationRecord` | Semantic verification output | Query ID, Snapshot ID, Requirement ID, Evidence ID | Frozen verification record | Pinned to snapshot, storage key embeds verifier type, version, and attempt |
| `CapabilityArtifact` | Capability index readiness | Envelope, Coverage ranges, Failure state, Build ID | Frozen artifact | Rebuild safe build ID, closed ranges, unknown total rules, scoped readiness |
| `QueryContract` | Decomposed query plan | Query ID, Snapshot ID, Requirements, Policy ID | Frozen query contract | Pinned to snapshot, unique requirement IDs, conjunction predicates, server resolved answer mode |

**Complete locator and payload compatibility validation matrix**:

The table below defines allowed pairings enforced by the `CanonicalEvidence` root validator. Any pairing not marked allowed raises a `ValidationError`. `FormulaPayload` represents raw extracted spreadsheet formulas and is explicitly permitted for table cell and structured record evidence types.

| `EvidenceType` | Allowed `Location` types | Allowed `ContentPayload` kinds | Rejection rationale |
|---|---|---|---|
| `text_chunk` | `document`, `section`, `slide` | `text` | Text chunks require prose locations and text payloads; tables, formulas, and figures are disallowed |
| `table_cell` | `table` | `cell`, `formula` | Individual table cells require table coordinates; content may be a scalar cell or an extracted raw formula |
| `table_slice` | `table` | `table`, `dataset_ref` | Table slices require table coordinates; tables up to 50 rows use `table`, larger use `dataset_ref` |
| `figure_asset` | `document`, `slide` | `asset` | Visual figures and charts require page or slide bounding boxes and asset payloads |
| `structured_record` | `section`, `table` | `text`, `cell`, `formula` | Structured records require section or table coordinates and text, cell, or formula payloads |

**Operator specific predicate validation matrix**:

The table below defines the value shapes required for each `SelectionOperator` in `SelectionPredicate`. Supplying a value with an incompatible shape raises a `ValidationError`.

| Operator | Allowed value types | Validation constraints | Rejection example |
|---|---|---|---|
| `eq`, `neq` | `ScalarPrimitive` (`str`, `int`, `float`, `bool`) | Single scalar primitive value | `operator="eq", value=["Europe", "Asia"]` |
| `gt`, `gte`, `lt`, `lte` | `OrderedScalar` (`int`, `float`, `str`) | Single comparable scalar (numeric or ISO date string) | `operator="gt", value=True` |
| `in`, `not_in` | `tuple[ScalarPrimitive, ...]` | Tuple of scalar primitives containing at least 1 element | `operator="in", value=2024` (must be tuple) |
| `between` | `tuple[OrderedScalar, OrderedScalar]` | Exactly 2 elements where lower bound is less than or equal to upper bound | `operator="between", value=(2024, 2020)` (inverted bounds) |
| `contains` | `str` | Non empty string pattern | `operator="contains", value=123` |

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| Ingestion chunking | `evidence_id` | Deterministic template `f"ev_{source_id}_v{source_version}_{pipeline_version}_{content_digest}_{chunk_index}"` |
| Ingestion extraction | `type` (`EvidenceType`) | Parser adapter based on extracted element kind (`text_chunk`, `table_cell`, etc.) |
| Ingestion extraction | `workspace_id` | Server authenticated session context (`TrustedAuthContext.authorized_workspace_id`) |
| Ingestion extraction | `source_id` | Ingestion job parameters |
| Ingestion extraction | `source_version` | Ingestion job parameters (positive integer) |
| Ingestion extraction | `location` | Parser coordinate detector emitting specific `Location` subtype with top left origin and full float precision |
| Ingestion extraction | `content` | Parser content extractor emitting specific `ContentPayload` subtype |
| Ingestion extraction | `unit` | Extracted from table column headers or numeric metadata, default `None` |
| Ingestion extraction | `qualifiers` | Extracted from row and column headers as typed `tuple[QualifierItem, ...]`, default empty tuple `()` |
| Ingestion extraction | `provenance.content_hash` | SHA-256 hash computed over raw source chunk bytes |
| Ingestion extraction | `provenance.extraction_pipeline_version` | Active parser version constant (such as `"parse-v5"`) |
| Ingestion extraction | `provenance.parser_element_id` | Original parser element identifier (such as OpenDataLoader element ID), default `None` |
| Ingestion extraction | `quality.status` | Parser QA validation metrics (`VALID`, `DEGRADED`, `REJECTED`) |
| Ingestion extraction | `quality.confidence` | Parser QA confidence score (float 0.0 to 1.0) |
| Ingestion extraction | `asset.is_ai_generated_description` | Set to `False` for raw extracted captions, `True` for vision model descriptions |
| Ingestion extraction | `formula.raw_expression` | Raw cell formula string extracted from spreadsheet parser (such as `"=SUM(C12:C18)-C19"`) |
| Ingestion large table | `dataset_ref.registered_file_id` | Registered file identifier in storage repository |
| Ingestion large table | `dataset_ref.schema_hash` | SHA-256 hash computed over table column schema |
| Indexing completion | `artifact_id` | Deterministic template `f"cap_{capability}_{source_id}_v{source_version}_{artifact_version}_{build_id_prefix}"` |
| Indexing completion | `artifact_version` | Active capability build version constant (such as `"bm25-v7"`) |
| Indexing completion | `pipeline_version` | Active indexing pipeline version constant (such as `"index-v3"`) |
| Indexing completion | `build_id` | Unique immutable build run identifier generated by build orchestrator |
| Indexing completion | `coverage.covered_ranges` | Programmatic range tracking from indexing batch job enforcing closed bounds |
| Indexing completion | `coverage.ready_partitions` | Partition readiness detector registering verified partition scopes |
| Indexing failure | `failure_state` | Indexer exception handler creating `ArtifactFailureState` |
| Query initialization | `workspace_id` | Server authenticated session context (`TrustedAuthContext.authorized_workspace_id`) |
| Query initialization | `query_id` | Request identifier generated by server API gateway |
| Query initialization | `snapshot_id` | Active pinned baseline snapshot manifest identifier from snapshot registry |
| Query planning | `requested_answer_mode` | Client API request parameter, defaulting to `None` when omitted |
| Query planning | `resolved_answer_mode` | Resolved by server planner: `"deterministic"` for exact lookups/aggregates, else policy default |
| Query planning | `policy_resolution_rationale` | Server planner explanatory note recording reason for mode selection |
| Query initialization | `budget_policy_id` | API request parameter or server default, referencing server controlled `ServerExecutionPolicy` |
| Requirement routing | `requirements[].requirement_id` | Query planner decomposition (`"r1"`, `"r2"`, etc., strictly unique) |
| Query execution | `execution_id` | Deterministic template `f"exec_{query_id}_{requirement_id}_att{attempt_number}"` |
| Query execution | `coverage_status` | Tabular executor evaluation (`COMPLETE_FOR_SELECTION`, `EMPTY_SELECTION`, `INCOMPLETE_EXECUTION`) |
| Query verification | `verification_id` | Deterministic template `f"vr_{query_id}_{snapshot_id}_{requirement_id}_{evidence_id}_{verifier_type}_{verifier_version}_att{attempt_number}"` |
| Query verification | `snapshot_id` | Pinned snapshot identifier from `QueryContract` |
| Query verification | `verifier_version` | Active verifier service version (such as `"semantic-verifier-v2"`) |
| Query verification | `verifier_type` | Component type (`VerifierType.GENERATIVE`, `ENCODER`, or `MECHANICAL`) |
| Query verification | `attempt_number` | Execution coordinator attempt counter (starts at 1, capped at 3) |
| Query verification | `execution_time_ms` | Elapsed milliseconds measured during verifier invocation |
| Query verification | `support_status` | Verification engine evaluation (`ASSERTED`, `HYPOTHESIZED`, `CONTRADICTED`, `UNVERIFIED`) |
| Query verification | `confidence_score` | Verification engine confidence score (float 0.0 to 1.0) |
| Query verification | `verification_rationale` | Verification engine explanatory rationale string |
| Query verification | `verified_at` | UTC timestamp recorded at verification completion |

**Key invariants**:
* Deep immutability: Every model sets `frozen=True` and `extra="forbid"`. All collections use `tuple`, never mutable `list` or `dict`.
* No untyped values: Table cells, predicate values, and qualifier values are strictly typed using scalar and union definitions. The type `Any` is prohibited.
* Flat identity serialization: Serialization emits top level identity keys without introducing an unapproved nested envelope wrapper in JSON.
* Rebuild safe identity: Evidence IDs bind source ID, source version, pipeline version, content digest, and chunk index. Artifact IDs bind capability, source ID, source version, artifact version, and build run ID. Rebuilding an index produces a new distinct build artifact without collision.
* Lifecycle field scoping: Ingestion evidence creation forbids `query_id` and `requirement_id`. Query contracts require `snapshot_id`. Verification records require `query_id`, `snapshot_id`, and `requirement_id`.
* Snapshot pinning: Verification records are strictly pinned to the query snapshot generation.
* Verifier identity uniqueness: Verification storage identity embeds verifier type, verifier version, and attempt number, preventing collision across retries and pipelines.
* Revalidation on transition: State transitions cannot use unvalidated shallow copying. All transitions revalidate invariants through transition factory methods.
* Machine readable closed coverage and partition completeness proof: Coverage is recorded as structured closed ranges `[start, end]` with unit types and indices, enforcing range arithmetic consistency, sorted order, and disjoint exclusions per partition. Formal completeness proof requires that `is_complete` can only be True when `total_units` is not None, `processed_count == total_units`, exclusions are accounted for, and for each partition, sorted non overlapping ranges cover the entire unit span without gaps.
* Unknown total units handling: If `total_units` is unknown (`None`), `is_complete` cannot be True, blocking false completeness claims during active streaming.
* Scoped readiness: Capabilities register ready partition scopes, allowing query planners to route to completed partitions before entire multi partition sources finish.
* Payload byte bounding: Hard caps of 32,768 chars on text, 4,096 chars on cell strings, 50 rows on inline tables, and 262,144 bytes (256 KB) on total evidence payload size. Larger tables require `DatasetRefPayload`.
* Separation of execution result from dataset reference: `DatasetRefPayload` carries pure source reference metadata. Query execution completeness and selection outcomes are recorded in `StructuredExecutionResult`.
* Structured execution result invariants: `StructuredExecutionResult` enforces `0 <= records_matched <= records_examined`, `attempt_number >= 1`, and strict `coverage_status` semantics (`COMPLETE_FOR_SELECTION` requires full evaluation of matching rows, `EMPTY_SELECTION` requires zero matches in full scan, `INCOMPLETE_EXECUTION` signals resource or scan cutoff).
* Full dataset bounded execution: Structured calculations must evaluate over 100 percent of matching rows. Bounded by server limits, returning `INCOMPLETE_EXECUTION` if limits are exceeded, strictly prohibiting silent partial sampling.
* Separation of raw formulas from executable ASTs: `FormulaPayload` holds raw source formula text and cached values. Calculations execute exclusively via server controlled `ExecutableFormulaAST` with allowlisted operators, forbidding raw string `eval()`.
* Coordinate origin and precision: Bounding box coordinates use a standard top left origin `(0.0, 0.0)` where `x` increases rightward and `y` increases downward, preserving full 64 bit IEEE 754 float precision without rounding.
* Requirement uniqueness: Each `requirement_id` within a `QueryContract` must be strictly unique.
* Predicate conjunction semantics: Multiple selection predicates in a selection tuple combine strictly via logical AND.
* Server only authorization context: `TrustedAuthContext` is constructed exclusively by server middleware and injected server side. Client payloads attempting to declare authorization context are rejected.
* Server resolved answer mode and execution policy: Clients cannot dictate arbitrary resource limits, raw SQL, or force narrative mode on exact calculations. Mode is resolved by planner policy: pure lookup or aggregate requirements enforce `deterministic`, pure summarization enforces `narrative`, and mixed requirements enforce `hybrid`. Budgets reference server definitions separating streaming batch sizes from total resource ceilings.

**Security model**:
* `workspace_id` is mandatory across all contracts. Empty strings, whitespace, and wildcard characters are rejected during validation.
* Server only authorization enforcement: `TrustedAuthContext` is injected strictly by server authentication middleware. Model validators verify that client supplied workspace identifiers match the server verified `authorized_workspace_id`. Cross workspace parameter spoofing is blocked at schema entry.
* Execution boundary: Execution requests use pre registered dataset references and allowlisted AST operators. Arbitrary code, raw SQL, shell execution, and Python `eval()` are strictly prohibited.
* Stable storage references: Stable storage URIs are stored in place of expiring signed URLs, ensuring authorized URLs are minted only on demand with appropriate permission checks.

**Configuration required**:
None. Contracts are pure Pydantic schema models using existing Python 3.12 dependencies.

## Representative JSON examples

### 1. Identity envelopes: Ingestion identity versus Query identity with server injected auth context

Ingestion identity envelope (source scope, no query or requirement identifiers):
```json
{
  "workspace_id": "ws_enterprise_1",
  "source_id": "doc_annual_report_2024",
  "source_version": 2
}
```

Query identity envelope (query scope, pinned snapshot, no source version):
```json
{
  "workspace_id": "ws_enterprise_1",
  "query_id": "q_7891",
  "snapshot_id": "snap_ws_enterprise_1_gen4"
}
```

### 2. Canonical evidence: PDF text chunk with top left coordinates and rebuild safe ID

```json
{
  "workspace_id": "ws_enterprise_1",
  "source_id": "doc_annual_report_2024",
  "source_version": 2,
  "evidence_id": "ev_doc_annual_report_2024_v2_parse-v5_7f83b1657ff1fc53_14",
  "type": "text_chunk",
  "location": {
    "type": "document",
    "page": 18,
    "bbox": [0.0812456, 0.2234198, 0.9245112, 0.4518923],
    "element_id": "p_18_3",
    "section_path": ["Financial Performance", "Operating Highlights"]
  },
  "content": {
    "kind": "text",
    "text": "Total enterprise revenue for the fiscal year reached 412.5 million euros, representing an organic growth rate of 14 percent compared to the previous period.",
    "token_count": 28
  },
  "unit": "EUR_million",
  "qualifiers": [
    {"key": "period", "value": "FY2024"},
    {"key": "accounting_standard", "value": "IFRS"}
  ],
  "provenance": {
    "content_hash": "sha256:7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069",
    "extraction_pipeline_version": "parse-v5",
    "parser_element_id": "odl_elem_9821"
  },
  "quality": {
    "status": "VALID",
    "confidence": 0.99
  }
}
```

### 3. Canonical evidence: Table cell with dual coordinates, raw formula payload, and typed qualifiers for segment revenue

```json
{
  "workspace_id": "ws_enterprise_1",
  "source_id": "sheet_financial_model_2024",
  "source_version": 3,
  "evidence_id": "ev_sheet_financial_model_2024_v3_parse-v5_a1b2c3d4e5f6a7b8_42",
  "type": "table_cell",
  "location": {
    "type": "table",
    "page": 4,
    "bbox": [0.1245189, 0.4512893, 0.8819234, 0.7234198],
    "table_id": "table_segment_revenue",
    "row_id": "Europe",
    "column_id": "2024",
    "sheet_name": "Revenue"
  },
  "content": {
    "kind": "formula",
    "raw_expression": "=SUM(C12:C18)-C19",
    "cached_value": 184.2,
    "eval_status": "CACHED_ONLY"
  },
  "unit": "EUR_million",
  "qualifiers": [
    {"key": "sheet", "value": "Revenue"},
    {"key": "region", "value": "Europe"},
    {"key": "year", "value": 2024}
  ],
  "provenance": {
    "content_hash": "sha256:a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2",
    "extraction_pipeline_version": "parse-v5",
    "parser_element_id": "cell_C20"
  },
  "quality": {
    "status": "VALID",
    "confidence": 0.98
  }
}
```

### 4. Canonical evidence: Bounded inline table (up to 50 rows and 256 KB)

```json
{
  "workspace_id": "ws_enterprise_1",
  "source_id": "doc_annual_report_2024",
  "source_version": 2,
  "evidence_id": "ev_doc_annual_report_2024_v2_parse-v5_9c8b7a6f5e4d3c2b_60",
  "type": "table_slice",
  "location": {
    "type": "table",
    "page": 28,
    "bbox": [0.1012456, 0.1518923, 0.9023419, 0.5512893],
    "table_id": "table_regional_breakdown",
    "row_id": null,
    "column_id": null,
    "sheet_name": null
  },
  "content": {
    "kind": "table",
    "table_id": "table_regional_breakdown",
    "headers": ["Region", "2023", "2024", "Growth"],
    "rows": [
      ["Europe", 161.5, 184.2, 0.14],
      ["North America", 145.0, 168.1, 0.16],
      ["Asia Pacific", 52.3, 60.2, 0.15]
    ],
    "row_count": 3,
    "col_count": 4
  },
  "unit": "EUR_million",
  "qualifiers": [
    {"key": "reporting_basis", "value": "Consolidated"}
  ],
  "provenance": {
    "content_hash": "sha256:9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d9c8b",
    "extraction_pipeline_version": "parse-v5",
    "parser_element_id": "odl_tbl_44"
  },
  "quality": {
    "status": "VALID",
    "confidence": 0.97
  }
}
```

### 5. Canonical evidence: Large table dataset reference (pure source reference without query status)

```json
{
  "workspace_id": "ws_enterprise_1",
  "source_id": "sheet_global_transactions",
  "source_version": 1,
  "evidence_id": "ev_sheet_global_transactions_v1_tabular-v4_1a2b3c4d5e6f7a8b_0",
  "type": "table_slice",
  "location": {
    "type": "table",
    "page": null,
    "bbox": null,
    "table_id": "sheet_transactions_all",
    "row_id": null,
    "column_id": null,
    "sheet_name": "Transactions"
  },
  "content": {
    "kind": "dataset_ref",
    "dataset_ref": {
      "source_id": "sheet_global_transactions",
      "source_version": 1,
      "registered_file_id": "reg_file_parquet_99182",
      "table_id": "sheet_transactions_all",
      "total_rows": 125000,
      "total_columns": 18,
      "schema_hash": "sha256:4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d9c8b7a6f5e4d3c"
    },
    "total_rows": 125000,
    "total_columns": 18,
    "schema_hash": "sha256:4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d9c8b7a6f5e4d3c"
  },
  "unit": null,
  "qualifiers": [
    {"key": "storage_format", "value": "parquet"}
  ],
  "provenance": {
    "content_hash": "sha256:1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
    "extraction_pipeline_version": "tabular-v4",
    "parser_element_id": null
  },
  "quality": {
    "status": "VALID",
    "confidence": 1.0
  }
}
```

### 6. Structured execution result: Query plane execution outcome for tabular calculation

```json
{
  "workspace_id": "ws_enterprise_1",
  "query_id": "q_7891",
  "snapshot_id": "snap_ws_enterprise_1_gen4",
  "requirement_id": "r1",
  "attempt_number": 1,
  "execution_id": "exec_q_7891_r1_att1",
  "evidence_id": "execution:ws_enterprise_1:exec_q_7891_r1_att1",
  "source_id": "sheet_global_transactions",
  "source_version": 1,
  "table_id": "sheet_transactions_all",
  "operation": "sum",
  "metric": "revenue",
  "selection": [
    {
      "field": "region",
      "operator": "eq",
      "value": "Europe"
    },
    {
      "field": "year",
      "operator": "in",
      "value": ["2023", "2024"]
    }
  ],
  "records_examined": 125000,
  "records_matched": 4210,
  "selection_digest": "sha256:8f4c2b1a0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b",
  "lineage_ref": "s3://kre-workspace-lineage/ws_enterprise_1/q_7891/exec_r1.parquet",
  "result_value": 184.2,
  "result_unit": "EUR_million",
  "coverage_status": "COMPLETE_FOR_SELECTION",
  "executed_at": "2026-10-08T01:34:50Z"
}
```

### 7. Capability artifact with partition aware coverage ranges and immutable build ID

Successful capability artifact with machine readable coverage ranges and scoped readiness:
```json
{
  "workspace_id": "ws_enterprise_1",
  "source_id": "doc_annual_report_2024",
  "source_version": 2,
  "artifact_id": "cap_text_search_doc_annual_report_2024_v2_bm25-v7_bld88a1f",
  "capability": "text_search",
  "ready": true,
  "coverage": {
    "unit_type": "page",
    "covered_ranges": [
      {
        "unit_type": "page",
        "start_index": 1,
        "end_index": 84,
        "partition_id": "part_main"
      }
    ],
    "exclusion_ranges": [],
    "processed_count": 84,
    "total_units": 84,
    "is_complete": true,
    "ready_partitions": ["part_main"]
  },
  "artifact_version": "bm25-v7",
  "pipeline_version": "index-v3",
  "build_id": "bld88a1f99c24018",
  "failure_state": null
}
```

### 8. Query contract with unique requirement items, operator specific predicates, and resolved answer mode

```json
{
  "query_id": "q_7891",
  "workspace_id": "ws_enterprise_1",
  "snapshot_id": "snap_ws_enterprise_1_gen4",
  "requirements": [
    {
      "requirement_id": "r1",
      "intent": "aggregate",
      "operation": "sum",
      "target": {
        "dataset": "table_segment_revenue",
        "metric": "revenue"
      },
      "selection": [
        {
          "field": "region",
          "operator": "eq",
          "value": "Europe"
        },
        {
          "field": "year",
          "operator": "in",
          "value": ["2023", "2024"]
        }
      ],
      "required_evidence": [
        {
          "kind": "table_cell",
          "locator": null,
          "selection": [
            {
              "field": "region",
              "operator": "eq",
              "value": "Europe"
            }
          ]
        }
      ]
    }
  ],
  "requested_answer_mode": null,
  "resolved_answer_mode": "deterministic",
  "policy_resolution_rationale": "Exact metric calculation requires deterministic execution without narrative synthesis.",
  "budget_policy_id": "query-default-v3"
}
```

### 9. Verification record with storage identity embedding verifier type, version, and attempt

```json
{
  "verification_id": "vr_q_7891_snap_ws_enterprise_1_gen4_r1_ev_sheet_financial_model_2024_v3_parse-v5_a1b2c3d4e5f6a7b8_42_generative_semantic-verifier-v2_att1",
  "workspace_id": "ws_enterprise_1",
  "query_id": "q_7891",
  "snapshot_id": "snap_ws_enterprise_1_gen4",
  "requirement_id": "r1",
  "evidence_id": "ev_sheet_financial_model_2024_v3_parse-v5_a1b2c3d4e5f6a7b8_42",
  "verifier_version": "semantic-verifier-v2",
  "verifier_type": "generative",
  "attempt_number": 1,
  "execution_time_ms": 142,
  "support_status": "ASSERTED",
  "confidence_score": 0.99,
  "verification_rationale": "Extracted cell explicitly matches region Europe and year 2024 with reported revenue 184.2 million euros under pinned snapshot manifest generation 4.",
  "verified_at": "2026-10-08T01:35:12Z"
}
```

### 10. Server execution policy with streaming batch size and resource ceilings

```json
{
  "policy_id": "query-default-v3",
  "max_query_llm_calls": 2,
  "max_query_rerank_calls": 2,
  "max_verifier_attempts": 3,
  "execution_batch_size": 5000,
  "max_execution_scan_rows": 50000,
  "max_execution_memory_mb": 256,
  "execution_timeout_ms": 5000,
  "null_policy": "error",
  "formula_evaluation_mode": "ast_evaluator"
}
```

**Critical test scenarios**:

* Happy path: Ingestion constructs valid `CanonicalEvidence` with page and bbox locators and serializes cleanly to flat JSON, verifies **AC-1**, **AC-5**, **AC-8**, **AC-13**.
* Failure case: Attempting to mutate an attribute on any frozen contract model raises `ValidationError` with error code `model_is_frozen`, and attempting in place mutation on tuple collections raises `TypeError` or `AttributeError`, verifies **AC-2**.
* Failure case: Attempting to pass extra ambient keys to any contract model raises `ValidationError`, verifies **AC-2**.
* Failure case: Attempting to pass untyped `Any` dictionaries or non scalar objects into `TablePayload.rows` raises `ValidationError`, verifies **AC-2**.
* Typed qualifiers happy path: Constructing `CanonicalEvidence` with typed `QualifierItem` records preserving string, integer, or boolean values succeeds, verifies **AC-2**.
* State transition case: Validated updates using transition factory methods return new instances with updated fields while original instances remain unchanged, and revalidation blocks contradictory updates, verifies **AC-3**.
* Negative transition case: Attempting to transition `CapabilityArtifact` to ready while `is_complete=False` or while retaining a failure state raises `ValidationError`, verifies **AC-3**.
* Deterministic ID case: Helper generator functions produce rebuild safe identifiers binding source versions and content digests, verifies **AC-4**.
* Rebuild safety collision negative case: Rebuilding an index with an updated artifact version and build ID produces a distinct `artifact_id`, preventing collision with existing active indexes, verifies **AC-4**.
* Coordinate origin and precision case: Table cell with normalized bounding box values adheres to top left origin `(0.0, 0.0)` with `x0 < x1` and `y0 < y1` and preserves full 64 bit float precision without rounding, verifies **AC-5**.
* Boundary coordinate negative case: Table cell with normalized bounding box values outside 0.0 to 1.0 or where `x0 >= x1` or `y0 >= y1` raises `ValidationError`, verifies **AC-5**.
* Table row limit case: Constructing `TablePayload` with more than 50 inline rows raises `ValidationError`, directing callers to `DatasetRefPayload`, verifies **AC-6**.
* Payload byte cap negative case: Constructing `TextPayload` exceeding 32,768 characters or `TablePayload` exceeding 262,144 bytes raises `ValidationError`, verifies **AC-6**.
* Total evidence byte cap negative case: Constructing `CanonicalEvidence` with total serialized payload exceeding 262,144 bytes raises `ValidationError`, verifies **AC-6**.
* Dataset reference separation case: Constructing `DatasetRefPayload` carries pure source reference metadata without query execution status, verifies **AC-6**.
* Structured execution result contract case: Constructing `StructuredExecutionResult` captures query plane execution proof with explicit `attempt_number`, `records_examined`, `records_matched`, and `coverage_status` (`COMPLETE_FOR_SELECTION`), verifies **AC-6**, **AC-14**.
* Structured execution result invariants negative case: Constructing `StructuredExecutionResult` with `records_matched > records_examined`, `attempt_number < 1`, or negative record counts raises `ValidationError`, verifies **AC-14**.
* Full dataset bounded execution negative case: Structured execution exceeding scan row or time limits returns explicit `INCOMPLETE_EXECUTION` and blocks claiming complete calculation, verifies **AC-6**, **AC-14**.
* Asset provenance case: `AssetPayload` retains `is_ai_generated_description` boolean flag, verifying separation between source captions and generated descriptions, verifies **AC-7**.
* Raw formula happy path: Constructing `CanonicalEvidence` with `type="table_cell"` and `FormulaPayload` holding raw expression text and cached value succeeds, verifies **AC-7**, **AC-10**.
* Raw formula security negative case: Attempting to evaluate raw formula string directly without server validated `ExecutableFormulaAST` is blocked, verifies **AC-7**.
* Quality independence case: Constructing `CanonicalEvidence` enforces `ExtractionQuality` with status and confidence without requiring or accepting query verification fields, verifies **AC-8**.
* Snapshot pinned verification record case: Constructing `VerificationRecord` successfully links `query_id`, `snapshot_id`, `requirement_id`, and `evidence_id` with storage identity embedding verifier type, version, and attempt number, verifies **AC-9**.
* Missing snapshot negative case: Attempting to construct `VerificationRecord` without `snapshot_id` raises `ValidationError`, verifies **AC-9**.
* Verifier attempt limit negative case: Attempting to record verification with `attempt_number > 3` raises `ValidationError`, verifies **AC-9**.
* Semantic mismatch negative case: Evaluating a revenue requirement against evidence carrying an EBITDA metric or from an EBITDA reconciliation table produces `support_status=CONTRADICTED` or `UNVERIFIED` with an explicit semantic mismatch rationale, verifies **AC-9**.
* Incompatible evidence pairing negative case: Constructing `CanonicalEvidence` with mismatched combinations (such as `type="text_chunk"` with `TableLocation` or `type="table_cell"` with `TextPayload`) raises `ValidationError`, verifies **AC-10**.
* Operator specific predicate happy path: `SelectionPredicate` with `operator="between"` and 2 element ordered tuple `(2023, 2024)` succeeds, verifies **AC-11**.
* Operator specific predicate negative case: `SelectionPredicate` with `operator="between"` and inverted bounds `(2024, 2020)` or `operator="in"` with a single integer raises `ValidationError`, verifies **AC-11**.
* Predicate conjunction happy path: Multiple predicates in `selection` combine via logical AND, verifies **AC-11**.
* Duplicate requirement identifier negative case: Supplying duplicate `requirement_id` values within `QueryContract.requirements` raises `ValidationError`, verifies **AC-11**.
* Resolved answer mode enforcement case: Constructing `QueryContract` for aggregate intent enforces `resolved_answer_mode="deterministic"` regardless of `requested_answer_mode`, verifies **AC-11**.
* Mixed query hybrid answer mode case: Constructing `QueryContract` with a mixture of aggregate and summarize requirements resolves to `resolved_answer_mode="hybrid"`, verifies **AC-11**.
* Server execution policy batching case: `ServerExecutionPolicy` defines `execution_batch_size` separated from total scan ceiling `max_execution_scan_rows`, verifies **AC-11**.
* Machine readable closed range arithmetic case: `ArtifactCoverage` validates that `start_index <= end_index`, ranges are sorted per partition, and sum of covered ranges equals `processed_count`, verifies **AC-12**.
* Partition completeness proof gap detection case: Constructing `ArtifactCoverage` with `is_complete=True` when covered ranges have gaps or do not span the entire total unit range raises `ValidationError`, verifies **AC-12**.
* Unknown total units readiness negative case: Constructing `ArtifactCoverage` with `total_units=None` and `is_complete=True` raises `ValidationError`, verifies **AC-12**.
* Scoped readiness happy path: `ArtifactCoverage` registers ready partition scopes (`ready_partitions`), enabling planners to route to ready partitions before entire source finishes, verifies **AC-12**.
* Coverage accounting negative case: `ArtifactCoverage` with `processed_count > total_units` or `is_complete=True` when `processed_count < total_units` raises `ValidationError`, verifies **AC-12**.
* Typed failure state case: `CapabilityArtifact` with `ready=False` and typed `ArtifactFailureState` validates retryable flags and error details, verifies **AC-12**.
* Auth case: Constructing any envelope with an empty or whitespace `workspace_id` raises `ValidationError`, verifies **AC-1**.
* Client auth injection negative case: Attempting to supply client side `TrustedAuthContext` fields in an external request schema raises `ValidationError`, verifies **AC-1**.
* Trusted auth mismatch negative case: Supplying a client `workspace_id` that does not match `TrustedAuthContext.authorized_workspace_id` raises authorization validation error, verifies **AC-1**.
* Lifecycle scoping negative case: Constructing ingestion evidence with `query_id` or `requirement_id` raises `ValidationError`, verifies **AC-1**.
* Serialization coercion case: Passing Python lists into tuple fields during `model_validate(...)` cleanly coerces them into immutable tuples, verifies **AC-13**.

## Build plan

1. Create `backend/src/schemas/contracts/envelope.py` with `IdentityEnvelope`, server only `TrustedAuthContext`, flat serialization, non empty workspace validation, and lifecycle field constraints, satisfies **AC-1**.
2. Create `backend/src/schemas/contracts/location.py` with discriminated union `Location` covering `DocumentLocation`, `TableLocation` (with dual visual and structural coordinates and top left origin), `SlideLocation`, and `SectionLocation`, preserving full 64 bit float precision, satisfies **AC-5**.
3. Create `backend/src/schemas/contracts/payload.py` with discriminated union `ContentPayload` (`TextPayload`, `CellPayload`, bounded `TablePayload` <= 50 rows and <= 256 KB, pure source `DatasetRefPayload`, `AssetPayload` with AI generated flag, `FormulaPayload` holding raw expression text), `QualifierItem`, `DatasetReference`, `ScalarValue`, `ExecutableFormulaAST`, and operator specific predicate types, satisfies **AC-2**, **AC-6**, **AC-7**.
4. Create `backend/src/schemas/contracts/evidence.py` with `CanonicalEvidence`, `EvidenceType`, `EvidenceProvenance`, and `ExtractionQuality`, enforcing rebuild safe identifiers, payload byte caps, deep tuple immutability, typed qualifiers, and complete locator payload compatibility validation including `FormulaPayload`, satisfies **AC-2**, **AC-3**, **AC-4**, **AC-6**, **AC-7**, **AC-8**, **AC-10**, **AC-13**.
5. Create `backend/src/schemas/contracts/execution.py` with `StructuredExecutionResult` and `ExecutionCoverageStatus`, separating query execution results from source dataset references, satisfies **AC-6**, **AC-14**.
6. Create `backend/src/schemas/contracts/verification.py` with `VerificationRecord`, `SupportStatus`, and `VerifierType`, pinning verification records to snapshots, embedding verifier type, version, and attempt number in the storage identity, and tracking attempt counters while keeping them distinct from source evidence, satisfies **AC-4**, **AC-9**.
7. Create `backend/src/schemas/contracts/capability.py` with `CapabilityArtifact`, `CapabilityType`, `CoverageRange`, `CoverageUnit`, `ArtifactCoverage`, and `ArtifactFailureState`, enforcing rebuild safe build IDs, partition aware closed range arithmetic, unknown total readiness rules, scoped readiness, and validated transition factory methods, satisfies **AC-3**, **AC-4**, **AC-12**.
8. Create `backend/src/schemas/contracts/query.py` with `QueryContract`, `RequirementItem` (enforcing requirement uniqueness and predicate conjunction), `RequirementIntent`, `OperationType`, `SelectionPredicate` with operator specific validation, server resolved answer mode, and server controlled `ServerExecutionPolicy` (separating batch size from scan ceiling), satisfies **AC-11**.
9. Export all contract models in `backend/src/schemas/contracts/__init__.py` and implement explicit serialization and deserialization pre validators converting JSON arrays to immutable tuples, satisfies **AC-13**.
10. Implement comprehensive positive and negative unit test suites in `backend/tests/test_contracts_and_identity.py` covering all acceptance criteria, unvalidated transition rejections, untyped payload rejections, rebuild safety collision checks, byte cap violations, structured execution contracts, operator specific predicate violations, predicate conjunction, requirement uniqueness, verifier attempt limits, semantic mismatch detection, server only authorization injection, coordinate precision preservation, and boundary rejections, satisfies **AC-1**, **AC-2**, **AC-3**, **AC-4**, **AC-5**, **AC-6**, **AC-7**, **AC-8**, **AC-9**, **AC-10**, **AC-11**, **AC-12**, **AC-13**, **AC-14**.

## Consequences

**Positive**:
* Guarantees strict type safety across all future refactored slices with zero untyped values, ambient dictionaries, or operator value mismatches.
* Prevents unvalidated transition corruption by enforcing validated factory methods.
* Rebuild safe identifiers and immutable build IDs guarantee that re indexing and document re ingestion never collide with active snapshots.
* Machine readable closed coverage ranges enable deterministic query planning and gap discovery without string parsing, handling multi partition sources and unknown streaming totals.
* Pinned verification records ensure tamper proof auditability across snapshot generations, with storage keys distinguishing verifier type, version, and attempt to prevent retry collisions.
* Clean separation of `StructuredExecutionResult` from `DatasetRefPayload` prevents query execution selections from polluting static source evidence references.
* Full dataset execution guarantees prevent misleading partial sampling calculations from reaching users, while separate streaming batch sizes protect execution buffers.
* Preserving raw formula text separately from executable abstract syntax trees protects execution engines from code injection while retaining extraction provenance.
* High precision coordinates with standard top left origin preserve visual re highlighting fidelity on high resolution document viewers.
* Server only authorization context injection blocks client tenant parameter tampering at schema entry.
* Server resolved answer mode ensures exact calculations remain deterministic regardless of client preferences.
* Requirement uniqueness and predicate conjunction semantics ensure query decomposition plans are unambiguous and verifiable.
* Protects system memory and network bandwidth by capping payload bytes and inline table rows.

**Negative / tradeoffs**:
* Requires converting legacy dictionaries and dataclasses into strict Pydantic instances when integrating existing adapters.
* Tuple collections require explicit tuple conversion syntax during model instantiation.
* Strict byte and row bounding requires parsers to emit dataset references for large files.

**Neutral**:
* Existing file format adapters will require wrapper mappers in Slice 2 to emit these new structures.
* The actual tabular execution engine implementation remains deferred to Slice 3 (Deterministic structured execution), with only shared data contracts defined here.

## Follow-up

- [ ] Update document parsing adapters in Slice 2 (Feature 5) to emit `CanonicalEvidence` records with rebuild safe identifiers, top left coordinates, and typed qualifiers.
- [ ] Connect `CapabilityArtifact` instances to the snapshot registry in Foundation Feature 3.
- [ ] Implement tabular query execution engine in Slice 3 (Feature 6) to evaluate dataset references and emit `StructuredExecutionResult` records outside of reciprocal rank fusion.
- [ ] Implement query requirement routing in Slice 5 (Feature 8) to decompose incoming questions into `RequirementItem` instances governed by server execution policies and resolved answer modes.
- [ ] Implement evidence verification engine in Slice 6 (Feature 9) to evaluate evidence against requirements and produce snapshot pinned `VerificationRecord` records.

## References

**Project sources**:
* `KRE_ARCHITECTURE_BIBLE_REFACTOR_READY_2026-10-06.md`: §0 Canonical Architecture, §1 Identity, §4 Upload and Registration, §7 Canonical Evidence Model, §8 Evidence Status Model, §10 Capability Manifest vs Capability Snapshot, §12 Retrieval Capability Registry, §16 Query Intake, §17 Query Model and Resource Budgets, §18 Query Contract, §26 Structured Execution, §27 CSV/Excel Execution Tool and Safety Contract, §32 Claim and Evidence Validation, §33 Claim Support Matrix, §45 Versioning, §46 Version-Aware Cache, §47 Replacement, Deletion and Concurrent Publication, §48 Immutable Workspace Snapshot, §52 Data Interfaces (Retrieval and Execution to Validator), §65 Security / Authorization Boundary
* `docs/TECHNICAL_SPEC.md`: §1 Identity Envelope, §2 Canonical Evidence, §3 Capability Artifact, §4 Query Contract, §5 CSV/Excel Executor Contract
* `docs/MIGRATION_MAP.md`: Architecture Bible Traceability Matrix, Replace/Refactor First
* `docs/scope/scope.md`: Foundation Feature 1, Slice 1 Feature 4, Slice 2 Feature 5, Slice 3 Feature 6, Slice 4 Feature 7, Slice 5 Feature 8, Slice 6 Feature 9, Slice 7 Feature 10, Slice 8 Feature 11

**Practices & standards**:
* Frozen data models and immutable collections for pipeline safety
* Discriminated unions for multi format document locators
* Separation of extraction provenance from semantic verification
* Full dataset bounded execution without silent sampling
* Separation of source formula provenance from executable ASTs
* Bounded payload size and byte caps for distributed messages
* Server controlled execution policies for deterministic computation
