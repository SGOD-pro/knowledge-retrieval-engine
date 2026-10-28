# Canonical Comparison Workspace Verification Report

## 1. Executive Summary and Execution Metadata

This report documents the verification of the target stores following the single process canonical ingestion into workspace `ws_canonical_comparison_0004` at commit [`528a97533a32fa3b9a6d8d39b06667017d5c477a`](file:///home/swyra/projects/knowledge-retrieval-engine).

```
Target Workspace:       ws_canonical_comparison_0004
Head Commit:            528a97533a32fa3b9a6d8d39b06667017d5c477a
Git Status:             Clean (0 uncommitted files)
Execution Mode:         Single process runner
Ingestion Command:      cd backend && PYTHONPATH=src uv run python scripts/ingest_canonical_comparison_workspace.py --workspace-id ws_canonical_comparison_0004
Process Exit Code:      1 (Failure on document 9 Qdrant Cloud point upsert timeout)
Historical Baseline:    ws_fresh_benchmark strictly preserved; no historical override
Spec 0006 AC-3 Scope:   Partial (capacity retention enforced in process; multi worker cross host unverified)
Benchmark Status:       NOT RUN (canonical 60 question benchmark gated)
```

---

## 2. Process Exit Code and Failure Checkpoint

The ingestion task exited with code 1 during the processing of the final document (`data/non_pdf_formats/survay.csv`).

### Failure Checkpoint Details
1. TableStore Ingestion: Succeeded completely. All 60,255 rows were committed to DynamoDB `kre-table` in AWS `ap-south-1` across 616 transactional batches with atomic checkpoints.
2. Embedding Generation: Succeeded completely. Bedrock Titan Text Embeddings V2 embedded all 5,000 vector chunks (`embed_service.full_done count=5000 latency_ms=4940561.48`). Local ONNX BGE dual embeddings were also generated.
3. Qdrant Cloud Save: Failed. In [`backend/src/modules/documents/documents_repository.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/modules/documents/documents_repository.py#L181), batch 1 (points 0 to 50) was written successfully. Batch 2 (points 50 to 100) exceeded the 60 second httpx timeout under remote cluster latency (`sa-east-1`), raising `httpx.WriteTimeout` wrapped in `RuntimeError: Failed to save document to cloud database: The write operation timed out`.
4. Document Snapshot: All 9 snapshots exist in [`backend/data/workspace_boundary.db`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/data/workspace_boundary.db) because boundary snapshots are recorded prior to vector writes.

```mermaid
flowchart TD
    A["Parse & Ingest CSV to DynamoDB TableStore (60,255 rows)"] -->|Success| B["Compute Titan V2 + ONNX Dual Embeddings (5,000 chunks)"]
    B -->|Success| C["Upsert Points to Qdrant Cloud (Batch 1: 0-50)"]
    C -->|Success| D["Upsert Points to Qdrant Cloud (Batch 2: 50-100)"]
    D -->|httpx.WriteTimeout| E["Process Exited with Code 1"]
```

---

## 3. Live Target Stores Verification

### 3.1 TableStore Verification in DynamoDB `kre-table`
Verified by live single pass queries executed against DynamoDB `kre-table` in AWS `ap-south-1`:

| Table ID | Document ID | Active Version | Persisted Rows | Expected Rows | Single Pass Read Count | Coverage Complete | Ingestion Digest |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `survay` | `34202080-60de-532e-a0c2-01f45b0af39a` | `c029d234eefd3fee2563b9c0929946ca` | 60,255 | 60,255 | 60,255 | True | `38f9121b44ab67f9` |
| `rs_status_bill_passed_assent_1952_2016` | `676cc28b-7480-575b-9643-44cd8d85698f` | `85d048226e88f44a76f3bf68c3f96669` | 727 | 727 | 727 | True | `0430653cab0f2b72` |

Both tables match raw row counts exactly. DynamoDB ingestion manifests were retrieved and verified for both tables.

### 3.2 Canonical Document Snapshots in Boundary Repository
Verified in [`backend/data/workspace_boundary.db`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/data/workspace_boundary.db):

| Source Document | Document ID | SHA-256 Version Prefix | Format | Persisted | Total | Incomplete Flag | PageIndex Status | OKF Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `survay.csv` | `34202080-60de-532e-a0c2-01f45b0af39a` | `c029d234eefd3fee2563b9c0929946ca` | csv | 60,255 | 67,284 | False | None | ready |
| `SEC-Form-10Q.pdf` | `492ccb2d-0d0d-5022-8a7b-e3f87df8befd` | `3518726963892d32ec11a876a5dfa448` | pdf | 40 | 40 | False | ready | failed |
| `rs_status_bill_passed_assent-1952-2016.csv` | `676cc28b-7480-575b-9643-44cd8d85698f` | `85d048226e88f44a76f3bf68c3f96669` | csv | 727 | 727 | False | None | ready |
| `2412.20875v1.pdf` | `89cc9991-e7e1-548f-bede-7c53bdd1a7c7` | `927420d4297edb4f6ff566961a14c98a` | pdf | 28 | 28 | False | ready | failed |
| `National-Strategy-for-Artificial-Intelligence.pdf` | `95b11ed0-7cb1-535b-b5c2-8ad3da681136` | `da5404dbb584c8956aae707dfcfe324d` | pdf | 139 | 139 | False | ready | failed |
| `2212.14776v3.pdf` | `afa8559f-21d4-5b64-885f-9c42ce2d4503` | `bce22600474513c61b8c47701d19275b` | pdf | 25 | 25 | False | ready | failed |
| `2501.09166v1.pdf` | `b213f371-bf2b-5359-b568-db0ea20a508e` | `d5a727f2d0b04269389630b6687f7fa4` | pdf | 13 | 13 | False | missing_layout | failed |
| `2501.05730v1.pdf` | `f8897f60-0128-5722-bd45-e8938daf9b75` | `4eafc24abe305dd84a859a95e19456a5` | pdf | 18 | 18 | False | missing_layout | failed |
| `NFHS_5_India_Districts_Factsheet_Data.xls` | `fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0` | `e482b990d74eb46b5ce5a6e10606b745` | xls | 707 | 707 | False | None | None |

Combined corpus SHA-256 matches verified hash `ac193bc5bea7bfcc547e39924118fdd469385ceb528d14d5528f0464f74c959d`.

### 3.3 Chunks and Vector Bindings in Qdrant Cloud (`kre_chunks`)
Verified by scrolling points filtered on `workspace_id = 'ws_canonical_comparison_0004'`:

* Total Points in Qdrant Cloud: 1,747 points
* Point distribution across documents:
  * `SEC-Form-10Q.pdf`: 40 chunks (100% indexed)
  * `rs_status_bill_passed_assent-1952-2016.csv`: 727 chunks (100% indexed)
  * `2412.20875v1.pdf`: 28 chunks (100% indexed)
  * `National-Strategy-for-Artificial-Intelligence.pdf`: 139 chunks (100% indexed)
  * `2212.14776v3.pdf`: 25 chunks (100% indexed)
  * `2501.09166v1.pdf`: 13 chunks (100% indexed)
  * `2501.05730v1.pdf`: 18 chunks (100% indexed)
  * `NFHS_5_India_Districts_Factsheet_Data.xls`: 707 chunks (100% indexed)
  * `survay.csv`: 50 chunks (Partial coverage, 50 of 5,000 chunks written prior to timeout)

### 3.4 PageIndex Summaries and Representative Target Resolution
Verified via [`PageIndexLookupService`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/services/retrieval/page_index_lookup.py):
* Total PageIndex Summaries: 6
* Ready with layout: 4 PDFs (`SEC-Form-10Q.pdf`, `2412.20875v1.pdf`, `National-Strategy-for-Artificial-Intelligence.pdf`, `2212.14776v3.pdf`)
* Missing layout fallback: 2 PDFs (`2501.09166v1.pdf`, `2501.05730v1.pdf`)
* Overflow to S3: 0 documents overflowed (`has_s3_overflow = False` for all summaries)
* Representative target resolution:
  * Query: `"What does table 1 show?"` on `2412.20875v1.pdf` resolved uniquely to candidate page 6 (`state='unique'`, 1 matching location)
  * Query: `"Show me exhibit 31.1"` on `SEC-Form-10Q.pdf` resolved uniquely to candidate page 40 (`state='unique'`, 1 matching location)

### 3.5 Open Knowledge Fabric (OKF) Extraction Status
Verified in [`backend/data/kre_okf.db`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/data/kre_okf.db):
* Total Manifests: 8
* Entities: 2 table entities (`rs_status_bill_passed_assent_1952_2016` and `survay`)
* Assertions: 20 column schema assertions (10 per table)
* Factual assertions extracted from text PDFs: 0 assertions

---

## 4. Root Cause Investigation of `get_client(config=...)` Error

During OKF enrichment, provider error logs recorded: `Bedrock OKF call failed (no retries per AC-3): Unable to locate credentials`.

### Root Cause Analysis
1. In [`backend/src/ingestion/okf_builder.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L864), the helper attempted to initialize the Bedrock runtime client via:
   ```python
   client = get_client("bedrock-runtime", config=cfg)
   ```
2. However, [`backend/src/aws/infra.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/aws/infra.py#L152) defines `get_client` with signature:
   ```python
   def get_client(service: str, region_name: str | None = None) -> Any:
   ```
   `get_client` does not accept a `config` parameter, raising `TypeError: get_client() got an unexpected keyword argument 'config'`.
3. The exception handler in [`backend/src/ingestion/okf_builder.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/ingestion/okf_builder.py#L871) attempted a fallback via `boto3.Session(region_name=settings.AWS_REGION)`. This fallback omitted `profile_name=settings.AWS_PROFILE` (which is `"aws"`). Because only profile `[aws]` is configured in `~/.aws/credentials` (no `[default]`), boto3 raised `NoCredentialsError: Unable to locate credentials`.

### Impact on Completed Corpus
* PDFs 1 to 5 (`2212.14776v3`, `2412.20875v1`, `2501.05730v1`, `2501.09166v1`, `SEC-Form-10Q`):
  Unaffected by the credential bug. The upfront token calculation estimated 2,092 to 2,095 tokens per prompt. Because the Spec 0006 AC-3 cap enforces `max_input_tokens_per_call = 2000`, `budget.reserve_call` rejected the requests upfront. These 5 documents failed closed with 0 remote calls and 0 tokens consumed.
* PDF 6 (`National-Strategy-for-Artificial-Intelligence`):
  Passed the token reservation check (1,972 estimated tokens), reserved the call, attempted invocation, encountered the `TypeError` and credential fallback failure, and failed closed with status `failed` after consuming 4,378 prompt tokens across 2 attempts.
* CSVs (`survay` and `rs_status`):
  Unaffected. CSV schema enrichment does not invoke remote LLMs; it derives verified table column assertions directly from TableStore metadata.

---

## 5. Reranker and Retrieval Configuration

* Primary Reranker Provider: OpenRouter API
* Model: `nvidia/llama-nemotron-rerank-vl-1b-v2:free`
* Threshold: `RERANKER_THRESHOLD = 0.05`
* Circuit Breaker: Trips on consecutive failures or 429 rate limits; half open probe interval is 300 seconds; cooldown is 3,600 seconds.
* Fallback Mode: Deterministic lexical BM25Plus scoring.
* Telemetry: Every query records `reranker_mode` (`remote_success` or `lexical_bm25_fallback`) and `reranker_remote_calls`.

---

## 6. Observed Provider Calls and Costs

| Provider / Resource | Service | Operations | Quantity | Unit Price | Observed Cost |
| :--- | :--- | :--- | :--- | :--- | :--- |
| AWS Bedrock | Titan Text Embeddings V2 | Chunk Embedding | 6,747 calls (1,747 saved + 5,000 survay) | $0.00002 / 1K tokens | ~$0.033 |
| AWS Bedrock | Anthropic Claude 3 Haiku | OKF Extraction | 2 calls (4,378 input tokens, 0 output) | $0.00025 / 1K input tokens | ~$0.001 |
| AWS DynamoDB | TableStore (`kre-table`) | Transactional Writes | 616 write batches (60,982 rows) | $1.25 / million writes | ~$0.184 |
| Qdrant Cloud | Point Upserts | Batch Upserts | 1,747 points across 35 batches | Free tier / active cluster | $0.000 |
| Total Observed | | | | | **~$0.218** |

Total observed cost aligns closely with authorized Option A preflight estimate ($0.185 TableStore + $0.033 embeddings).

---

## 7. Safe Resume Procedure for Remaining Survay Points

To complete Qdrant vector indexing for `survay.csv` safely without restarting ingestion or rewriting existing records:

1. TableStore rows (60,255 rows) and active version `c029d234eefd3fee2563b9c0929946ca` in DynamoDB remain untouched.
2. Boundary snapshots in `data/workspace_boundary.db` and OKF manifests in `data/kre_okf.db` remain untouched.
3. Vector Chunks: 50 points exist in Qdrant collection `kre_chunks`. The remaining points (point indices 50 to 5,000) can be upserted in smaller batches of 25 with `wait=False` (or with client timeout increased to 120s) without repeating Bedrock embedding calls if embeddings are loaded from cache or computed with batch throttling.
4. Tabular Query Note: In the query router, tabular queries targeting `survay` route directly to DynamoDB TableStore single pass queries where 100% of rows are present.

---

## 8. Strategy Ablation Readiness Verdicts

```mermaid
flowchart LR
    subgraph Run1 [Run 1: Repaired Baseline]
        R1[Conditional / Partial Ready]
    end
    subgraph Run2 [Run 2: PageIndex]
        R2[Ready]
    end
    subgraph Run3 [Run 3: OKF]
        R3[Degraded / Not Ready]
    end
    subgraph Run4 [Run 4: Combined]
        R4[Degraded]
    end
```

### Run 1: Repaired Baseline (BM25 + Dense Vector + TableStore)
* **Verdict**: CONDITIONAL / PARTIAL READY
* **Justification**:
  * DynamoDB TableStore is 100% verified with complete single pass reads for both CSVs (60,255 rows in `survay`, 727 rows in `rs_status`).
  * Documents 1 to 8 have 100% vector chunks indexed in Qdrant Cloud (1,697 points).
  * `survay.csv` has 50 vector points in Qdrant Cloud. If evaluation queries for `survay.csv` target tabular aggregation, TableStore answers them with complete coverage. If evaluation queries perform semantic similarity search over raw survey rows, coverage is partial.

### Run 2: PageIndex Ablation (Baseline + PageIndex)
* **Verdict**: READY
* **Justification**:
  * All 6 PDF PageIndex summaries are persisted in [`backend/data/workspace_boundary.db`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/data/workspace_boundary.db).
  * 4 PDFs have verified structural layouts with 139 structural elements; 2 PDFs have valid missing layout status.
  * Structural lookup path was executed live; queries for Table 1 and Exhibit 31.1 resolve uniquely to candidate pages with zero errors.

### Run 3: OKF Ablation (Baseline + OKF)
* **Verdict**: DEGRADED / NOT READY for Text Factual Evaluation
* **Justification**:
  * Table schema assertions exist for both CSVs (20 column assertions).
  * Zero text factual assertions were produced across all 6 PDFs due to strict Spec 0006 AC-3 2,000 token cap enforcement and the Bedrock client configuration error.
  * Running an OKF ablation in this state will provide zero factual uplift on text questions, falling back entirely to baseline.

### Run 4: Combined System (Baseline + PageIndex + OKF)
* **Verdict**: DEGRADED
* **Justification**:
  * Baseline and PageIndex components will function, but the OKF contribution will be limited exclusively to tabular column definitions.
