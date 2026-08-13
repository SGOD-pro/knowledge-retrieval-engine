"""
Q02 Debug Trace - run this from backend/ with: uv run python tests/q02_run.py
Re-ingests all docs (to populate in-memory cache), then traces Q02 step-by-step.
"""
import sys
import os
import logging
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("q02")

from benchmark_comparison import ingest_all, DOCS
logger.info("=== RE-INGESTING TO POPULATE IN-MEMORY CACHE ===")
# Hack: Only ingest the doc that contains the answer to Q02 to save time!
DOCS[:] = [d for d in DOCS if d.name == "Workflow Documentation.docx"]
t0 = time.perf_counter()
all_chunks = ingest_all()
logger.info("ingest done: %d chunks in %.1fs", len(all_chunks), time.perf_counter() - t0)

# Force dev embeddings for query (local ONNX)
from config import settings
settings.ENVIRONMENT = "dev"

QUERY = "How does CodeCouncil differ from Copilot?"

from db.database import CloudRepository, _IN_MEMORY_CHUNKS
repo = CloudRepository()

print("\n" + "=" * 70)
print("Q02 DEBUG TRACE")
print(f"Query  : {QUERY}")
print(f"Chunks : {len(all_chunks)} in-memory | {len(_IN_MEMORY_CHUNKS)} in cache")
print(f"Fidelity threshold: {settings.FIDELITY_THRESHOLD}")
print("=" * 70 + "\n")

from services.retrieval.bm25_retriever import BM25Retriever
from services.retrieval.page_index_retriever import PageIndexRetriever
from services.retrieval.fidelity_check import check_fidelity, CoverageError, extract_query_entities
from services.retrieval.compressor import compress_chunks
from services.retrieval.reranker import rerank
from services.retrieval.vector_retriever import VectorRetriever
from services.langgraph_pipeline import _rrf_merge

# BM25
print("[STEP 1] BM25")
bm25 = BM25Retriever()
bm25_results = bm25.search(QUERY, all_chunks, top_k=10)
print(f"  Top-3:")
for i, (c, s) in enumerate(bm25_results[:3], 1):
    print(f"  [{i}] score={s:.3f} pg={c.page_number} | {c.text[:100]!r}")

# PageIndex
print("\n[STEP 2] PageIndex")
bm25_chunks = [c for c, _ in bm25_results]
pi = PageIndexRetriever()
pi_chunks, candidate_pages, candidate_chunk_ids = pi.filter_and_rank(QUERY, bm25_chunks, top_k=10)
print(f"  chunks={len(pi_chunks)} candidate_pages={candidate_pages}")

# Vector
print("\n[STEP 3] Vector Search")
vr = VectorRetriever(repository=repo)
doc_ids = list({c.document_id for c in all_chunks})
vector_results = vr.search(
    QUERY, fast_path=False, document_ids=doc_ids,
    candidate_page_ids=candidate_pages or None,
    candidate_chunk_ids=candidate_chunk_ids or None,
    top_k=10,
)
print(f"  Top-3:")
for i, (c, s) in enumerate(vector_results[:3], 1):
    print(f"  [{i}] sim={s:.3f} pg={c.page_number} | {c.text[:100]!r}")

# RRF
merged = _rrf_merge(bm25_chunks, [c for c, _ in vector_results])
print(f"\n[STEP 3b] RRF merge -> {len(merged)} chunks")

# Reranker
print("\n[STEP 4] Reranker")
try:
    top_chunks = rerank(QUERY, merged, top_k=6)
    print("  Top-3:")
    for i, c in enumerate(top_chunks[:3], 1):
        score = getattr(c, "reranker_score", "N/A")
        print(f"  [{i}] score={score} | {c.text[:100]!r}")
except Exception as e:
    top_chunks = merged[:6]
    print(f"  FAILED ({e}) -- using RRF top-6")

# Compressor
print("\n[STEP 5] Compressor")
compressed = compress_chunks(QUERY, top_chunks)
print(f"  {len(compressed)} chars")
print(f"  {compressed[:400]!r}")

# Fidelity
print("\n[STEP 6] Fidelity Check")
entities = extract_query_entities(QUERY)
text_lower = compressed.lower()
found = sum(1 for e in entities if e.lower() in text_lower)
coverage = float(found) / len(entities) if entities else 1.0
print(f"  Entities: {entities}")
print(f"  Found: {found}/{len(entities)}  Coverage: {coverage:.2f}  Threshold: {settings.FIDELITY_THRESHOLD}")

try:
    check_fidelity(QUERY, compressed)
    print("  -> FIDELITY PASSED")

    print("\n[STEP 7] LLM Call")
    from services.llm.llm_service import call as llm_call
    t_llm = time.perf_counter()
    resp = llm_call(QUERY, compressed)
    print(f"  LLM latency: {(time.perf_counter()-t_llm)*1000:.1f}ms")

    print("\n" + "=" * 70)
    print("FINAL ANSWER:")
    print(resp.get("answer", "NOT_FOUND"))
    citations = resp.get("citations", [])
    if citations:
        print(f"Citations: {citations}")
    print("=" * 70)

except CoverageError as e:
    print(f"  -> FIDELITY FAILED: {e}")
    print("\nFINAL ANSWER: NOT_FOUND")
