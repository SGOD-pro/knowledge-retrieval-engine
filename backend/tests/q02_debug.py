"""
Q02 Debug Trace — run a single query through KRE with detailed pipeline logging.
Prints: BM25 top-3, Vector top-3, Reranker top-3, Fidelity coverage, final answer.

Run from backend/: python tests/q02_debug.py
"""
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("q02_debug")

# Force dev embeddings (local ONNX) to avoid BGE Lambda
from config import settings
settings.ENVIRONMENT = "dev"

QUERY = "What are the steps in the onboarding workflow?"


def main():
    from db.database import CloudRepository
    from services.retrieval.bm25_retriever import BM25Retriever
    from services.retrieval.page_index_retriever import PageIndexRetriever
    from services.retrieval.vector_retriever import VectorRetriever
    from services.retrieval.reranker import rerank
    from services.retrieval.compressor import compress_chunks
    from services.retrieval.fidelity_check import check_fidelity, CoverageError, extract_query_entities

    print(f"\n{'='*70}")
    print(f"Q02 DEBUG TRACE")
    print(f"Query: {QUERY}")
    print(f"FIDELITY_THRESHOLD: {settings.FIDELITY_THRESHOLD}")
    print(f"{'='*70}\n")

    repo = CloudRepository()

    # Load all chunks from DB
    print("[ STEP 0 ] Loading all chunks from DynamoDB/Qdrant...")
    t0 = time.perf_counter()
    all_chunks = repo.get_all_chunks()
    print(f"  Total chunks in store: {len(all_chunks)}  ({(time.perf_counter()-t0)*1000:.1f}ms)\n")

    if not all_chunks:
        print("ERROR: No chunks in store. Run benchmark ingestion first.")
        sys.exit(1)

    # BM25
    print("[ STEP 1 ] BM25 Retrieval...")
    t1 = time.perf_counter()
    bm25 = BM25Retriever()
    bm25_results = bm25.search(QUERY, all_chunks, top_k=10)
    print(f"  BM25 top-3 ({(time.perf_counter()-t1)*1000:.1f}ms):")
    for i, (chunk, score) in enumerate(bm25_results[:3], 1):
        print(f"  [{i}] score={score:.3f} pg={chunk.page_number} text={repr(chunk.text[:90])}")
    print()

    # PageIndex
    print("[ STEP 2 ] PageIndex Filter + Rank...")
    bm25_chunks = [c for c, _ in bm25_results]
    t2 = time.perf_counter()
    pi = PageIndexRetriever()
    pi_chunks, candidate_pages, candidate_chunk_ids = pi.filter_and_rank(QUERY, bm25_chunks, top_k=10)
    print(f"  PageIndex: {len(pi_chunks)} chunks | pages={candidate_pages} ({(time.perf_counter()-t2)*1000:.1f}ms)\n")

    # Vector
    print("[ STEP 3 ] Vector Search...")
    t3 = time.perf_counter()
    vr = VectorRetriever(repository=repo)
    doc_ids = list({c.document_id for c in all_chunks})
    vector_results = vr.search(
        QUERY,
        fast_path=False,
        document_ids=doc_ids,
        candidate_page_ids=candidate_pages or None,
        candidate_chunk_ids=candidate_chunk_ids or None,
        top_k=10,
    )
    print(f"  Vector top-3 ({(time.perf_counter()-t3)*1000:.1f}ms):")
    for i, (chunk, score) in enumerate(vector_results[:3], 1):
        print(f"  [{i}] sim={score:.3f} pg={chunk.page_number} text={repr(chunk.text[:90])}")
    print()

    # RRF Merge
    from services.langgraph_pipeline import _rrf_merge
    merged = _rrf_merge(bm25_chunks, [c for c, _ in vector_results], k=60)
    print(f"[ STEP 3b] RRF Merge -> {len(merged)} unique chunks\n")

    # Reranker
    print("[ STEP 4 ] Reranker...")
    t4 = time.perf_counter()
    try:
        top_chunks = rerank(QUERY, merged, top_k=6)
        print(f"  Reranker top-3 ({(time.perf_counter()-t4)*1000:.1f}ms):")
        for i, chunk in enumerate(top_chunks[:3], 1):
            score = getattr(chunk, "reranker_score", "N/A")
            print(f"  [{i}] reranker_score={score} text={repr(chunk.text[:90])}")
    except Exception as e:
        top_chunks = merged[:6]
        print(f"  Reranker FAILED ({e}), using RRF top-6 instead")
    print()

    # Compressor
    print("[ STEP 5 ] Compressor...")
    compressed = compress_chunks(top_chunks)
    print(f"  Compressed ({len(compressed)} chars): {repr(compressed[:400])}\n")

    # Fidelity
    print("[ STEP 6 ] Fidelity Check...")
    entities = extract_query_entities(QUERY)
    text_lower = compressed.lower()
    found = sum(1 for e in entities if e.lower() in text_lower)
    coverage = float(found) / len(entities) if entities else 1.0
    print(f"  Entities extracted: {entities}")
    print(f"  Found: {found}/{len(entities)}  Coverage: {coverage:.2f}  Threshold: {settings.FIDELITY_THRESHOLD}")
    try:
        check_fidelity(QUERY, compressed)
        print("  FIDELITY PASSED\n")
    except CoverageError as e:
        print(f"  FIDELITY FAILED: {e}")
        print("\nFINAL ANSWER: NOT_FOUND (fidelity gate rejected)\n")
        return

    # LLM
    print("[ STEP 7 ] LLM Call...")
    t7 = time.perf_counter()
    from services.llm.llm_service import call as llm_call
    resp = llm_call(QUERY, compressed)
    print(f"  LLM latency: {(time.perf_counter()-t7)*1000:.1f}ms")
    print(f"\n{'='*70}")
    print("FINAL ANSWER:")
    print(resp.get("answer", "NOT_FOUND"))
    citations = resp.get("citations", [])
    if citations:
        print(f"Citations: {citations}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
