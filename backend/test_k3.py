import os
import json
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from src.config import settings
from src.services.langgraph_pipeline import pipeline
from src.run_benchmark import _faithfulness_score

queries_to_test = {
    "hit_1": "What exact mathematical formula defines Scaled Dot-Product Attention?",
    "hit_2": "Why is the dot product scaled by 1/sqrt(d_k) in Scaled Dot-Product Attention?",
    "miss_1": "What is the relationship between AI intervention in agriculture and farm yields?",
    "miss_2": "How many parallel attention heads h are employed in the Transformer?"
}

def run_k3():
    # Make sure defaults are set (so it matches J2)
    settings.BM25_THRESHOLD = 0.1
    settings.PAGEINDEX_THRESHOLD = 0.1
    settings.VECTOR_THRESHOLD = 0.3
    settings.RERANKER_THRESHOLD = 0.5
    
    for label, query in queries_to_test.items():
        print(f"\n=== {label.upper()} ===")
        print(f"Query: {query}")
        
        try:
            response = pipeline.run(query, document_ids=None)
            ans = response.answer
            context = response.context_snippet
            
            score = _faithfulness_score(ans, context)
            
            print(f"Answer: {ans}")
            print(f"Context snippet provided to LLM: {context}")
            print(f"Faithfulness Score: {score}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    run_k3()
