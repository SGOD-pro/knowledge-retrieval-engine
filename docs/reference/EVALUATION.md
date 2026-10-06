# KRE Evaluation Contract

## Status
Requirements for evaluation; no results are asserted here.

## Source Coverage
Evaluate dense reports, multi-column papers, table-heavy documents, slide decks, scanned PDFs, CSV/XLSX and mixed-source queries.

## Retrieval Metrics
Recall@K, MRR, nDCG, Precision@K, required-evidence recall.

## Answer Metrics
Answer support, request completeness, contradiction handling, human relevancy where applicable.

## Extraction Metrics
Parse success, schema validity, OCR/table accuracy, locator accuracy, coverage.

## System Metrics
Latency, cold/warm behavior, model calls/tokens/cost, branch timeouts, cache, resource use.

## Baselines
Compare vector-only, BM25-only and BM25+vector against KRE on the same source/question set.

## Ground Truth
Questions should be human-authored against real documents, with exact locators and requirement coverage. Architecture diagrams are not benchmark evidence.
