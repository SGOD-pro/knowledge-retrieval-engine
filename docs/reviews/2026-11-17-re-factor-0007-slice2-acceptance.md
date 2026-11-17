# Review, re-factor, 2026-11-17

**Reviewed by**: gemini-2.5-pro (author on claude-3-5-sonnet)
**Scope**: 8 files, branch vs main (re-factor vs af02997)
**Verdict**: Changes requested

## Summary
This change implements Spec 0007 Slice 2: Grounded Narrative PDF Extraction and Durable Bedrock Budgeting, alongside subsequent correctness repairs covering chunk provenance validation, grounding context preservation, actor-predicate syntactic binding, and token limit enforcement. The distributed budget ledger architecture, atomic DynamoDB transactions, monotonic lease fencing, and token accounting are implemented with high technical rigor. However, several heuristic text grounding regexes and rigid word allowlists in `graph_builder.py` suffer from severe brittleness: naive period splitting rejects standard corporate abbreviations (`Inc.`, `Corp.`) and decimal percentages, case-insensitive modality matching rejects factual relationships occurring in the month of May, and narrow intervening word allowlists reject standard business phrasing like "competitor", "supplier", and "startup".

## Major
### 🟠 Naive period splitting breaks grounding on standard corporate abbreviations and decimal numbers, `backend/src/ingestion/graph_builder.py:310`
**Problem**: In `validate_relationship_grounding:310`, sentences are split using `sentences = re.split(r"[.!?\n]+", text_lower)`, and `_find_enclosing_sentence:155` terminates at any `r"[.!?\n]"`. In standard business text, entities and financial metrics routinely contain periods: corporate suffixes (`Inc.`, `Corp.`, `Ltd.`, `Co.`), honorifics (`Dr.`), and decimal metrics (`51.5%`, `$3.5M`). When an entity or metric contains a dot, `re.split` splits the sentence in two. For example, `"Acme Inc. acquired Beta Corp."` is split into `['acme inc', ' acquired beta corp', '']`, leaving `Acme Inc` and `Beta Corp` in separate sentences; `matched_sentences` is empty and the extractor rejects the relationship with `co_occurrence_across_disjoint_sentences`. Similarly, `"Acme acquired 51.5% of Beta"` is split at the decimal point and rejected. In `extract_pdf_relationships`, this causes zero entities and zero relationships to be extracted from valid corporate narrative text.
**Why it matters**: Corporate press releases, 10-K filings, and contracts almost always use company suffixes (`Inc.`, `Corp.`) and decimal numbers. Discarding valid relationships on standard company names in production causes massive false-negative extraction dropouts.
**Suggested fix**: Use a boundary-aware sentence splitter (such as splitting on periods followed by whitespace and an uppercase letter, `r"(?<=[.!?])\s+(?=[A-Z])"`, or masking known abbreviations like `inc.`, `corp.`, `ltd.`, `dr.` and numeric decimals `\d+\.\d+` prior to splitting).

### 🟠 Case-insensitive modality regex rejects factual claims occurring in the month of May, `backend/src/ingestion/graph_builder.py:98`
**Problem**: `_UNSUPPORTED_MODALITY_REGEX` matches `\b(?:...|may|...)\b` with `re.IGNORECASE`. In narrative documents, `"May"` is one of the twelve calendar months. A factual sentence such as `"In May 2023, Acme acquired Beta."` matches the token `"may"` and is immediately rejected with `hypothetical_or_unsupported_claim`.
**Why it matters**: Any merger, acquisition, partnership, or executive appointment that took place in the month of May will be systematically rejected as an ungrounded or hypothetical claim.
**Suggested fix**: Restrict modal verb `"may"` to case-sensitive lowercase `\bmay\b` (or require auxiliary context such as `\b(?:may\s+(?:have|be|also)?\s*\w+)`), ensuring capitalized calendar references like `"In May 2023"` or `"May 14"` are not treated as epistemic modality.

### 🟠 Rigid dictionary allowlists reject common English phrasing between entities and predicates, `backend/src/ingestion/graph_builder.py:388`
**Problem**: In `validate_relationship_grounding:388`, intervening tokens between predicate and target are strictly required to be members of `_ALLOWED_ACTIVE_INTERVENING_WORDS`, and tokens between source and predicate are restricted to `_ALLOWED_PRE_PREDICATE_WORDS`. Standard business nouns and adjectives such as `"competitor"`, `"supplier"`, `"vendor"`, `"provider"`, `"startup"`, `"developer"`, and `"rival"` are absent from `_ALLOWED_ACTIVE_INTERVENING_WORDS`, causing sentences like `"Acme acquired competitor Beta"`, `"Acme partnered with supplier Beta"`, and `"Acme invested in startup Beta"` to be rejected with `unsupported_syntactic_binding`. Furthermore, `_ALLOWED_PRE_PREDICATE_WORDS` omits common adverbs like `"today"` and prepositional phrases like `"in 2023"`, rejecting `"Acme today acquired Beta"` and `"Acme in 2023 acquired Beta"`. Failing the check also immediately short-circuits with `return False`, terminating search over other valid sentence clauses.
**Why it matters**: Natural language PDF extraction cannot rely on an arbitrary 30-word closed vocabulary. Valid factual relationships with common descriptive nouns or temporal adverbs will be erroneously dropped.
**Suggested fix**: Replace rigid closed-world token sets with structural syntax rules: allow noun phrases and adjectives before the target entity (or inspect POS tags / clause boundaries), permit temporal adverbs and prepositional spans before predicates, and `continue` searching alternative match positions rather than immediately returning `False`.

## Minor
### 🟡 Passive voice detection omits infinitive and modal auxiliary 'be', `backend/src/ingestion/graph_builder.py:346`
**Problem**: `is_passive = bool(re.search(r"\b(?:was|is|been|were|are)\b(?:\s+\w+){0,2}\s+" + re.escape(pred_stem) + r"\w*\s+by\b", clause))` omits `be`. Sentences using future or modal passive constructions like `"Beta will be acquired by Acme"` or `"Beta to be acquired by Acme"` fail passive detection (`is_passive = False`), fall into active voice evaluation, and are rejected as `reversed_relationship`.
**Why it matters**: Future or announced transaction announcements in news or SEC filings will be discarded.
**Suggested fix**: Add `be` to the passive auxiliary group: `r"\b(?:was|is|been|were|are|be)\b"`.

### 🟡 Chunk with page_number=0 causes unhandled ValidationError on DocumentLocation, `backend/src/ingestion/graph_builder.py:1489`
**Problem**: In `extract_pdf_relationships:1346`, `page = int(page_val)`. If an upstream parser produces `page_number=0` (0-indexed page numbers), `int(0)` produces `0`. At line 1489, `location = DocumentLocation(page=ch_page)` fails because `DocumentLocation.page` enforces `ge=1`, throwing a Pydantic `ValidationError` and aborting `extract_pdf_relationships`.
**Why it matters**: An edge-case page index from an upstream parser will crash the entire PDF extraction pass for that document.
**Suggested fix**: Clamp page numbers to a minimum of 1: `page = max(1, page)`.

### 🟡 `stage_relationships` call is unhandled by try/except in `build_and_persist_pdf_graph`, `backend/src/ingestion/graph_builder.py:1221`
**Problem**: While `commit_manifest` is wrapped in a `try/except` block (lines 1263-1265), the preceding call to `self.repository.stage_relationships(...)` (lines 1221-1228) is unprotected. If database communication fails during batch writing, the exception escapes uncaught, bypassing `fail_extraction_budget` and failing to return a fail-closed manifest.
**Why it matters**: Storage hiccups during staging leave the worker lease held and leak unhandled exceptions to callers instead of cleanly failing closed per ADR-001.
**Suggested fix**: Wrap `stage_relationships` in a `try/except` block that calls `self.repository.fail_extraction_budget` and returns `self._fail_closed_manifest`.

### 🟡 Sentence-wide negation check produces false positives on non-negating prepositional phrases, `backend/src/ingestion/graph_builder.py:321`
**Problem**: `tokens = _clean_text_tokens(sentence)` checks whether any word in `_NEGATION_WORDS` (including `"no"` and `"without"`) appears anywhere in the sentence. Factual statements containing phrases like `"Acme acquired Beta with no regulatory objections"` or `"Acme acquired Beta without debt"` are rejected as `negated_relationship`.
**Why it matters**: Valid relationships accompanied by qualifying prepositional phrases are falsely classified as negated.
**Suggested fix**: Check for negation tokens within the verbal predicate span or immediate pre-predicate auxiliary context rather than across the entire sentence.

## Nits
- ⚪ `backend/src/db/knowledge_graph_repo.py:2516`: In `reconcile_extraction_budget`, `DynamoDBKnowledgeGraphRepository` flags `fenced_out = True` if `lease_expires_at < time.time()`, whereas `InMemoryKnowledgeGraphRepository` and `SQLiteKnowledgeGraphRepository` only check `fence_token` and `lease_owner_id`. Align storage adapter behavior.
- ⚪ `backend/src/db/knowledge_graph_repo.py:68`: `disable_client_retries` unregisters retry handlers by modifying the boto3 client's internal event emitter in place. If callers pass a shared boto3 client singleton, this strips retries globally. Prefer configuring `Config(retries={'max_attempts': 0})` on client instantiation where feasible.
- ⚪ `backend/src/ingestion/graph_builder.py:183`: `validate_chunk_provenance` accesses attributes via `getattr(chunk, "id", None)`. If plain dictionary chunks are provided (as supported in `extract_tabular_relationships`), it fails with `missing_chunk_id`. Consider supporting `chunk.get("id")` for dictionary inputs.

## Strengths
- **Durable Budgeting and Distributed Fencing**: The implementation of `BUDGET#LEDGER` across InMemory, SQLite WAL, and DynamoDB is exemplary. The use of conditional updates, 15-second worker leases, monotonic fence tokens, and 2-item transactions (`transact_write_items`) in DynamoDB ensures strict distributed safety and crash recovery.
- **Strict Token Limits and Spend Retention**: Per-call bounds (inputTokens <= 2000, outputTokens <= 800) and aggregate document limits (4000 prompt, 1600 output, 5600 total) fail closed cleanly while durably retaining token expenditures in the ledger.
- **Concurrency Admission & Capacity Retention**: `KnowledgeGraphProviderConcurrencyController` enforces thread pool admission boundaries and retains worker capacity permits on caller timeouts, preventing orphaned background Bedrock calls from overloading the provider.
- **Conservative Entity Identity**: Document-scoped identity by default prevents cross-document homonym join hallucinations, and model-supplied disambiguation tokens are strictly verified against authoritative mappings before cross-document joins are permitted.
- **Thorough Test Suite**: 56 unit and regression tests in `test_knowledge_graph_slice2.py` exhaustively test concurrency, fencing, timeout recovery, and storage atomicity across SQLite and DynamoDB Local.

## Test coverage
The test suite in `backend/tests/test_knowledge_graph_slice2.py` has 56 passing tests providing excellent coverage for budget leasing, fence advancement, stale worker rejection, throttling retries, token ceiling overruns, and chunk provenance validation. However, test coverage for relationship grounding relies on synthetic sentences without company abbreviations (`Acme Inc` rather than `Acme Inc.`), without decimal percentages, and without natural descriptive nouns between predicates and entities. Adding regression tests for abbreviations (`Inc.`, `Corp.`), numeric percentages, calendar months (`May`), and descriptive noun phrases will ensure the grounding heuristics are robust before merge.
