# BOUNDARIES.md — System Boundaries and Scope

## Hard Boundaries (enforced in code or system prompt)

- **Answers from provided documents only.** Never from LLM training data. Enforced by system prompt. Tested by `test_r15_*`.
- **PDF only in v1.** No Word/Excel/PPT/HTML ingestion.
- **No OCR in default mode.** Hybrid mode (`opendataloader-pdf --hybrid`) is a configuration option, not a default. Scanned PDFs: document this limitation clearly in the UI.
- **Temperature = 0 on all LLM calls.** Reproducibility is non-negotiable.
- **No streaming output.** Answers returned as complete structured JSON.
- **No multi-turn conversation state in v1.** Each query is independent.  
  *Reason:* Multi-turn requires session memory, which requires a privacy decision. Defer to v2.
- **No data leaves local deployment.** All models (`BGE-small`, `bge-reranker-base`, `spaCy`) run on-device. LLM call is the only external API call, and it receives only compressed context, never raw document text.
- **No user query or answer stored to persistent log** unless admin enables it via explicit config flag. Off by default.

---

## Soft Boundaries (design choices, revisit post-v1)

- **Max corpus:** 10,000 pages per instance.  
  *Above this:* FAISS recall degrades, partition corpus.
- **Max graph:** 5,000 nodes, 50,000 edges.  
  *Above this:* Log warning, partition by document cluster.
- **Max PDF size:** 500 pages per document.  
  *Larger files:* Split at ingestion time, preserve cross-chunk references.
- **English language only (v1).**  
  Architecture supports multilingual — swap `BGE-small` for `multilingual-e5-small` and rebuild index. Not a redesign.
- **Ingestion is batch/async**, not synchronous per-upload.  
  Re-ingestion of changed document replaces all its chunks and invalidates all related cache keys.

---

## Out of Scope for v1 (do not build, do not plan)

- **Image/chart description** — `opendataloader-pdf` hybrid handles this. Do not rebuild it.
- **Formula rendering** — same, handled at parser.
- **User authentication / multi-tenancy.**
- **Feedback-driven live reranking weight updates.**
- **SQL / structured database query path.**
- **Fine-tuning on user data.**
- **Real-time document sync / watch-folder ingestion.**

