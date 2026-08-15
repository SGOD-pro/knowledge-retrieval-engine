# BOUNDARIES.md (rev 6)

## Hard Boundaries

- Answers from provided documents only. Never from LLM training data.
  Enforced by system prompt. Tested by test_r15_*.

- Supported formats v1: PDF, DOCX, XLSX, PPTX, CSV.
  HTML, XML: v2.

- No OCR in default mode. opendataloader-pdf --hybrid is opt-in.
  Non-PDF formats have no OCR layer — text extraction only.

- Temperature = 0 on all LLM calls (query-time and ingestion-time).

- No streaming output. Answers returned as complete structured JSON.

- No multi-turn conversation state. Each query is independent. (v2)

- Local model inference runs in the BGE-small microservice (separate deployment). The Query Lambda does NOT bundle ONNX weights — it calls the microservice via HTTP. All other embedding, reranking, and LLM calls must be API calls through providers/*.py.

- No GPU-dependent frameworks (torch, transformers) in any Lambda deployment package. `onnxruntime` is allowed in the BGE-small microservice only.

- Deployment package limits:
  - Query Lambda: Zip deployment <250MB unzipped, or Container image 10GB limit.
  - Ingestion Lambda: Zip deployment <250MB unzipped (pending dependency size check).
  - PDF Extraction Lambda: Container image 10GB limit.
  (50MB is only the direct upload limit, not the functional ceiling).

- Document text sent to API providers: still only compressed context to the LLM provider. Raw chunk text IS sent to the embedding and reranker providers during retrieval for the full path.

- OpenRouter free-tier models: DEV AND STAGING ONLY. Never the sole path in a production deployment. (Note: NVIDIA NIM is a first-class production provider alongside Bedrock). CI blocks deploy to prod stage if MODEL_PROVIDER=dev is set in prod environment config.

- No user query or answer stored to persistent log unless admin enables via explicit config flag. Off by default.

- Nova Micro (Bedrock) is ingestion-only. Zero query-time calls.

## Soft Boundaries

- Max corpus: QdrantDB + DynamoDB don't have the 10k-page FAISS-recall-degradation ceiling from rev 1-3. Benchmark at 50k pages before setting a new number.

- Lambda timeout: 15 minutes max. Full pipeline query must complete well under this (target 4s p95). Ingestion Lambda timeout must account for `odl-parser-lambda` JVM parse time + Ingestion Lambda's remaining work.

- Max graph: 5,000 nodes, 50,000 edges.
- Max file size: 500 pages per PDF, 200 slides per PPTX, 10,000 rows per XLSX sheet, 500 pages per DOCX.
- Ingestion is executed via the canonical FastAPI `/ingest` API (synchronous HTTP multipart upload). The S3-event-driven async worker (`ingestion_lambda/main.py`) is intentionally deferred to v2 as a backlog scaling item for large bulk document drops.
- **Citation Semantics**: The API currently returns the *entire pool* of `top_chunks` that survived the Reranker as "citations" for a given response, provided the LLM's final answer passes the Fidelity Check (meaning the answer was successfully extracted from that context pool). It does *not* filter down to the actually-cited subset of chunks. This guarantees 100% grounded chunk IDs with correct locations (eliminating LLM hallucinated citations) but introduces a precision tradeoff where some returned citations may not have directly contributed to the generated answer.
- **Fast-Path Routing Gate**: The fast-path routing decision (which bypasses the LLM and Reranker for simple factual lookups) relies entirely on a keyword-based flag system (checking for synthesis, comparison, temporal, and relationship tokens). This has a known structural limitation: synthesis queries phrased with vocabulary outside the current flag families will be silently misrouted to the fast-path. This is currently mitigated by a broad `synthesis_flag`, but the problem is not fully solved until a semantic/embedding-based routing signal is implemented.
- **LLM Activation Rate Margin**: Our LLM activation metric is currently sitting at `0.5882` (10/17 queries), which mathematically satisfies the strict `< 0.60` target. However, this is a razor-thin margin achieved on a small benchmark sample (17 queries) that has already been extensively tuned against. Because this rate depends directly on the `synthesis_flag` mitigation (which has known structural keyword-evasion vulnerabilities), this target should not be considered robustly or permanently met until the semantic routing signal is implemented.
- **PDF Diagram & Multi-Column Layouts**: Diagram-heavy and multi-column PDFs processed via the `odl-parser-lambda`'s linear text extraction will currently produce degraded or garbage embeddings. They are not reliably searchable. This is a known, accepted limitation pending either a fallback parser or an upgrade to the extraction Lambda's spatial reconstruction logic.

## Out of Scope v1
- Image/chart description (Extraction logic exists in odl-parser-lambda but remains inert and out of scope for Phase 4 UI/client responses).
- Formula rendering.
- User authentication / multi-tenancy.
- Feedback-driven live reranking.
- SQL / structured query path.
- Fine-tuning on user data.
- Real-time document sync.
- Fully air-gapped / zero-external-API deployment.
