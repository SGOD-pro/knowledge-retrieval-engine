import os
import time
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from src.services.langgraph_pipeline import run_llm, end_fast_path


def test_m1_exits():
    full_state = {
        "query": "What is the capital of France?",
        "compressed_text": "", # Empty context
        "top_chunks": [],
        "stage_timings": {}
    }
    
    print("Testing Full Path early exit (run_llm) - 5 iterations:")
    for i in range(5):
        t0 = time.perf_counter()
        _ = run_llm(full_state)
        t1 = time.perf_counter()
        print(f"  Iter {i+1}: {(t1 - t0)*1000:.4f} ms")

    fast_state = {
        "query": "What is the capital of France?",
        "top_chunks": [], # Empty candidate chunks
        "candidate_chunks": [],
        "stage_timings": {}
    }
    
    print("\nTesting Fast Path early exit (end_fast_path) - 5 iterations:")
    for i in range(5):
        t0 = time.perf_counter()
        _ = end_fast_path(fast_state)
        t1 = time.perf_counter()
        print(f"  Iter {i+1}: {(t1 - t0)*1000:.4f} ms")

if __name__ == "__main__":
    test_m1_exits()
