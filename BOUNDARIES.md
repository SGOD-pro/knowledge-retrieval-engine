# BOUNDARIES.md (rev 4)

## Hard Boundaries

- Answers from provided documents only. Never from LLM training data.
  Enforced by system prompt. Tested by test_r15_*.

- Supported formats v1: PDF, DOCX, XLSX, PPTX.
  (Previously PDF-only. Updated.)
  HTML, XML, CSV: v2.

- No OCR in default mode. opendataloader-pdf --hybrid is opt-in.
  Non-PDF formats have no OCR layer — text extraction only.

- Temperature = 0 on all LLM calls (query-time and ingestion-time).

- No streaming output. Answers returned as complete structured JSON.

- No multi-turn conversation state. Each query is independent. (v2)

- No local model inference anywhere in production. All embedding,
  reranking, and LLM calls are API calls through providers/*.py.
  (Previously: "All models run on-device." This is now REVERSED.)

- No model weights or ML framework binaries (torch, onnxruntime,
  transformers with model files) in the Lambda deployment package.
  If a dependency pulls in torch as a transitive dependency, it
  must be excluded or the affected code path removed.

- Deployment package: <50MB zipped, <250MB unzipped. Enforced by
  CI check on every deploy. Build fails if exceeded.

- Document text sent to API providers: still only compressed
  context to the LLM provider (unchanged, Rule 3/4). BUT: raw
  chunk text IS now sent to the embedding and reranker providers
  during retrieval, since those are external API calls in this
  architecture. This is a genuine privacy posture change from
  rev 1-3's "fully local" claim — document this clearly for any
  regulated-industry deployment. If a customer requires zero
  document text leaving their environment, prod provider must be
  Bedrock inside their own VPC (Bedrock supports VPC endpoints),
  not OpenRouter under any circumstances.

- OpenRouter free-tier models: DEV AND STAGING ONLY. Never the sole
  path in a production deployment. CI blocks deploy to prod stage
  if MODEL_PROVIDER=dev is set in prod environment config.

- No user query or answer stored to persistent log unless admin
  enables via explicit config flag. Off by default.

- Nova Micro (Bedrock) is ingestion-only. Zero query-time calls.
  If Nova Micro is called during a query: CI test fails, PR blocked.

## Soft Boundaries

- Max corpus: revise upward. pgvector/Aurora Serverless doesn't
  have the 10k-page FAISS-recall-degradation ceiling from rev 1-3.
  New practical ceiling: driven by Aurora Serverless v2 cost/ACU
  scaling, not a hard retrieval-quality limit. Benchmark at 50k
  pages before setting a new number.

- Lambda timeout: 15 minutes max. Full pipeline query must complete
  well under this (target 4s p95) — timeout is not a practical
  constraint for query Lambda, but IS a constraint for any batch
  ingestion Lambda (large PDF batches may need Step Functions
  orchestration instead of a single Lambda invocation).

- Max graph: 5,000 nodes, 50,000 edges.
- Max file size: 500 pages per PDF, 200 slides per PPTX,
  10,000 rows per XLSX sheet, 500 pages per DOCX.
- Language: Nemotron-1B supports 34 languages natively.
  v1 tested on English only. Multilingual: v2 with benchmark.
- Ingestion is batch/async. Not synchronous per-upload.

## Out of Scope v1 (unchanged)
- Image/chart description.
- Formula rendering.
- User authentication / multi-tenancy.
- Feedback-driven live reranking.
- SQL / structured query path.
- Fine-tuning on user data.
- Real-time document sync.
- CSV/HTML ingestion.
- Fully air-gapped / zero-external-API deployment (would require
  reverting to rev 3's local-model architecture on ECS/Fargate,
  not Lambda — document as an alternate deployment profile, not v1).



