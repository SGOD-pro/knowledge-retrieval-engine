import time
import os
import json
from src.services.langgraph_pipeline import pipeline

def run_query(query_text, query_name):
    print(f"\n--- Running {query_name} ---")
    start = time.time()
    result = pipeline.run(query_text)
    end = time.time()
    
    print(f"Total time for {query_name}: {end - start:.2f} seconds")
    # The pipeline state might have timing info if we instrumented it, but 
    # we can just time the whole thing to see if the second run is fast.

print("Warming up pipeline with Query 2...")
q2 = "What is the projected size and CAGR of the Indian healthcare sector by 2020 as stated in the report?"
run_query(q2, "Query 2")

print("\nRunning Query 1 (The slow one from U2)...")
q1 = "Who are the primary NITI Aayog authors credited with writing the National Strategy for Artificial Intelligence report?"
run_query(q1, "Query 1 (first time in this process)")

print("\nRunning Query 1 again...")
run_query(q1, "Query 1 (second time)")
