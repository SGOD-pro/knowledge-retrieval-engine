import os
from dotenv import load_dotenv

# Ensure we load the .env from the backend directory regardless of cwd
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import json
from config import settings
from services.langgraph_pipeline import pipeline
from run_benchmark import _expected_pages, _hits_at_k

def run_ablation(name: str):
    with open('d:/WORK/knowledge-retrieval-engine/backend/llm_ground_truths.json', 'r', encoding='utf-8') as f:
        ground_truths = json.load(f)
        
    hits_at_5 = 0
    scored = 0
    for gt in ground_truths:
        query = gt["query"]
        expected_pages = _expected_pages(gt)
        if not expected_pages:
            continue
            
        try:
            # Bypass API and hit the pipeline directly
            response = pipeline.run(query, document_ids=None)
            
            # response.citations is a list of chunk IDs returned by LLM, but wait!
            # If we want pure retrieval recall, we should look at response.top_chunks!
            # Actually, _hits_at_k parses the citations list. Let's use top_chunks directly.
            retrieved_chunk_ids = [c.id for c in getattr(response, "top_chunks", [])]
            
            h5 = _hits_at_k(retrieved_chunk_ids, expected_pages, k=5)
            hits_at_5 += h5
            scored += 1
        except Exception as e:
            print(f"Error on {query}: {e}")
            
    recall = hits_at_5 / scored if scored else 0.0
    print(f"Ablation [{name} disabled]: Recall@5 = {recall:.4f}")

def main():
    # Baseline defaults
    defaults = {
        "BM25_THRESHOLD": 0.1,
        "PAGEINDEX_THRESHOLD": 0.1,
        "VECTOR_THRESHOLD": 0.3,
        "RERANKER_THRESHOLD": 0.5
    }
    
    print("Starting Ablation tests (direct pipeline invocation)...")
    print(f"Qdrant URL is set to: {settings.QDRANT_URL}")
    
    for target in defaults.keys():
        # Reset all to defaults
        for k, v in defaults.items():
            setattr(settings, k, v)
        
        # Disable the target
        setattr(settings, target, -1.0)
        
        run_ablation(target)

if __name__ == "__main__":
    main()
