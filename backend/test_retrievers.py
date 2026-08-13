import asyncio
from services.retrieval.bm25_retriever import BM25Retriever
from services.retrieval.page_index import PageIndexRetriever
from services.retrieval.vector_retriever import VectorRetriever
from db.database import CloudRepository
import json
import logging
logging.basicConfig(level=logging.WARNING)

async def main():
    repo = CloudRepository()
    bm25 = BM25Retriever(repository=repo)
    page_idx = PageIndexRetriever(repository=repo)
    vector = VectorRetriever(repository=repo)
    
    with open('d:/WORK/knowledge-retrieval-engine/backend/llm_ground_truths.json', 'r', encoding='utf-8') as f:
        ground_truths = json.load(f)
        
    for i, gt in enumerate(ground_truths):
        query = gt["query"]
        
        # Test BM25
        # The benchmark pipeline does bm25 -> page_index -> vector.
        bm25_res = bm25.search(query, top_k=20)
        bm25_min = min([s for _, s in bm25_res]) if bm25_res else 0.0
        
        vector_res = vector.search(query, fast_path=False, top_k=20)
        vec_min = min([s for _, s in vector_res]) if vector_res else 0.0
        
        print(f"Q{i+1}: BM25_min={bm25_min:.4f}, Vector_min={vec_min:.4f}, BM25_max={bm25_res[0][1] if bm25_res else 0:.4f}, Vec_max={vector_res[0][1] if vector_res else 0:.4f}")

if __name__ == "__main__":
    asyncio.run(main())
