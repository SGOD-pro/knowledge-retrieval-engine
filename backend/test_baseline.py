import os
from dotenv import load_dotenv

# Ensure we load the .env from the backend directory regardless of cwd
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import json
from src.config import settings
from src.services.langgraph_pipeline import pipeline
from src.run_benchmark import _expected_pages, _hits_at_k

def run_baseline():
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
            response = pipeline.run(query, document_ids=None)
            retrieved_chunk_ids = [c.id for c in getattr(response, "top_chunks", [])]
            h5 = _hits_at_k(retrieved_chunk_ids, expected_pages, k=5)
            hits_at_5 += h5
            scored += 1
        except Exception as e:
            print(f"Error on {query}: {e}")
            
    recall = hits_at_5 / scored if scored else 0.0
    print(f"Baseline (all enabled): Recall@5 = {recall:.4f}")

if __name__ == "__main__":
    run_baseline()
