# KRE Cost Contract

\[
C_q=C_{planning}+C_{retrieval}+C_{rerank}+C_{verification}+C_{generation}+C_{storage/read}
\]

Track separately:
- ingestion compute
- embeddings/indexing
- optional enrichment
- query model/tool work
- storage/cache
- queue/runtime

Every model/tool invocation records operation, provider, tokens/usage where available, retries, latency and cost estimate/actual. Remote reranking/verification is not treated as free.
