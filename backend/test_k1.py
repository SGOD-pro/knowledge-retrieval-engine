import os
import json
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from src.config import settings
# Disable thresholds to get full candidate pools
settings.BM25_THRESHOLD = -1.0
settings.PAGEINDEX_THRESHOLD = -1.0
settings.VECTOR_THRESHOLD = -1.0
settings.RERANKER_THRESHOLD = -1.0

from src.services.retrieval.bm25_retriever import BM25Retriever
from src.services.retrieval.vector_retriever import VectorRetriever
from src.services.langgraph_pipeline import _rrf_merge
from src.services.retrieval.reranker import rerank
from src.run_benchmark import _expected_pages
from src.db.database import CloudRepository

def run_k1():
    with open('d:/WORK/knowledge-retrieval-engine/backend/llm_ground_truths.json', 'r', encoding='utf-8') as f:
        ground_truths = json.load(f)
        
    bm25_retriever = BM25Retriever()
    vector_retriever = VectorRetriever()
    repo = CloudRepository()
    all_chunks = repo.get_all_chunks(None)
    
    gt_scores = []
    incorrect_scores = []
    
    for idx, gt in enumerate(ground_truths):
        query = gt["query"]
        expected = _expected_pages(gt)
        if not expected: continue
        
        bm25_res = bm25_retriever.search(query, all_chunks, top_k=20)
        bm25_cands = [c for c, _ in bm25_res]
        
        vec_res = vector_retriever.search(query, top_k=10, fast_path=False)
        vec_cands = [c for c, _ in vec_res]
        
        merged = _rrf_merge(bm25_cands, vec_cands, k=60)
        
        # Now rerank to get scores attached
        _ = rerank(query, merged, top_k=50) # top_k=50 so we keep all of them to inspect scores
        
        for chunk in merged:
            score = getattr(chunk, "reranker_score", 0.0)
            if chunk.page_number in expected:
                gt_scores.append(score)
            else:
                incorrect_scores.append(score)
                
    print("\n=== K1 Reranker Score Distributions ===")
    
    def print_stats(name, arr):
        if not arr:
            print(f"{name}: NO DATA")
            return
        arr_sorted = sorted(arr)
        min_v = arr_sorted[0]
        max_v = arr_sorted[-1]
        mid = len(arr_sorted) // 2
        median_v = arr_sorted[mid] if len(arr_sorted) % 2 != 0 else (arr_sorted[mid-1] + arr_sorted[mid]) / 2.0
        
        print(f"{name} ({len(arr)} chunks):")
        print(f"  Min:    {min_v:.6f}")
        print(f"  Max:    {max_v:.6f}")
        print(f"  Median: {median_v:.6f}")
        print(f"  All:    {[float(f'{x:.4f}') for x in arr_sorted]}")

    print_stats("Ground Truth Chunks", gt_scores)
    print_stats("Incorrect Chunks", incorrect_scores)

if __name__ == "__main__":
    run_k1()
