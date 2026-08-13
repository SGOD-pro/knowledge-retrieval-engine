import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import json
from src.services.retrieval.bm25_retriever import BM25Retriever
from src.services.retrieval.vector_retriever import VectorRetriever
from src.run_benchmark import _expected_pages

def test_bm25_scores():
    with open('d:/WORK/knowledge-retrieval-engine/backend/llm_ground_truths.json', 'r', encoding='utf-8') as f:
        ground_truths = json.load(f)
        
    bm25 = BM25Retriever()
    vr = VectorRetriever()
    
    scores = []
    for gt in ground_truths:
        query = gt["query"]
        expected = _expected_pages(gt)
        if not expected: continue
        
        # Get chunks from DB via Vector search (bypassing threshold)
        from src.config import settings
        settings.VECTOR_THRESHOLD = -1.0
        vec_results = vr.search(query, top_k=20, fast_path=False)
        chunks = [res[0] for res in vec_results]
        
        if not chunks: continue
        
        bm25_results = bm25.search(query, chunks, top_k=20)
        
        for chunk, score in bm25_results:
            if chunk.page_number in expected:
                scores.append(score)
                break
                
    print(f"BM25 Scores for Ground Truth chunks: {sorted(scores)}")
    print(f"Min score: {min(scores) if scores else 'N/A'}")
    print(f"Max score: {max(scores) if scores else 'N/A'}")

if __name__ == "__main__":
    test_bm25_scores()
