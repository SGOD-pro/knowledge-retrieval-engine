# KRE Performance Contract

## Latency
Sequential:
\[
T_{seq}=\sum_i T_i
\]
Independent discovery:
\[
T_{parallel}pprox\max(T_{branch})
\]

## Bounds
Every query has overall/branch deadlines, candidate limits, data-scan limits, graph depth/fanout, model limits and expansion limits.

## Large Corpus
Use bounded batches, queues/backpressure, pagination, checkpoints and cancellation.

## Reranking
Initial narrative retrieval uses a fixed bounded policy. Adaptive skipping requires measured preservation of required-evidence recall.

## Cache
Version/authorization/session-compatible only.
