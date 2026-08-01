# BOUNDARIES.md (rev 5)

## Hard Boundaries

- Answers from provided documents only. Never from LLM training data.
  Enforced by system prompt. Tested by test_r15_*.

- Supported formats v1: PDF, DOCX, XLSX, PPTX.
  HTML, XML, CSV: v2.

- No OCR in default mode. opendataloader-pdf --hybrid is opt-in.
  Non-PDF formats have no OCR layer — text extraction only.

- Temperature = 0 on all LLM calls (query-time and ingestion-time).

- No streaming output. Answers returned as complete structured JSON.

- No multi-turn conversation state. Each query is independent. (v2)

- Local model inference is ONLY permitted for the Query Lambda fast path (`BGE-small-en-v1.5` ONNX). All other embedding, reranking, and LLM calls must be API calls through providers/*.py.

- No GPU-dependent frameworks (torch, transformers) in the Lambda deployment package. `onnxruntime` is the explicit and sole exception for BGE-small.

- Deployment package limits:
  - Query Lambda: Zip deployment <250MB unzipped, or Container image 10GB limit.
  - Ingestion Lambda: Zip deployment <250MB unzipped (pending dependency size check).
  - PDF Extraction Lambda: Container image 10GB limit.
  (50MB is only the direct upload limit, not the functional ceiling).

- Document text sent to API providers: still only compressed context to the LLM provider. Raw chunk text IS sent to the embedding and reranker providers during retrieval for the full path.

- OpenRouter free-tier models: DEV AND STAGING ONLY. Never the sole path in a production deployment. CI blocks deploy to prod stage if MODEL_PROVIDER=dev is set in prod environment config.

- No user query or answer stored to persistent log unless admin enables via explicit config flag. Off by default.

- Nova Micro (Bedrock) is ingestion-only. Zero query-time calls.

## Soft Boundaries

- Max corpus: pgvector/RDS PostgreSQL doesn't have the 10k-page FAISS-recall-degradation ceiling from rev 1-3. Benchmark at 50k pages before setting a new number.

- Lambda timeout: 15 minutes max. Full pipeline query must complete well under this (target 4s p95). Ingestion Lambda timeout must account for `odl-parser-lambda` JVM parse time + Ingestion Lambda's remaining work.

- Max graph: 5,000 nodes, 50,000 edges.
- Max file size: 500 pages per PDF, 200 slides per PPTX, 10,000 rows per XLSX sheet, 500 pages per DOCX.
- Language: Nemotron-1B supports 34 languages natively. v1 tested on English only.
- Ingestion is batch/async. Not synchronous per-upload.

## Out of Scope v1
- Image/chart description (Extraction logic exists in odl-parser-lambda but remains inert and out of scope for Phase 4 UI/client responses).
- Formula rendering.
- User authentication / multi-tenancy.
- Feedback-driven live reranking.
- SQL / structured query path.
- Fine-tuning on user data.
- Real-time document sync.
- CSV/HTML ingestion.
- Fully air-gapped / zero-external-API deployment.
