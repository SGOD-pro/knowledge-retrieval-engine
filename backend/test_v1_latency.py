import time
from src.services.langgraph_pipeline import pipeline

def run_query_with_timing(query_text, query_name):
    print(f"\n=== Timing {query_name} ===")
    start_total = time.time()
    
    result = pipeline.run(query_text)
    
    total_time = time.time() - start_total
    print(f"Total end-to-end time: {total_time:.4f} seconds")
    
    print("Stage timings (from PipelineState):")
    for stage, duration_ms in result.stage_timings.items():
        print(f"  - {stage}: {duration_ms} ms")

print("Warming up with Q2 (to absorb one-time costs)...")
q2 = "What is the projected size and CAGR of the Indian healthcare sector by 2020 as stated in the report?"
run_query_with_timing(q2, "Q2 (Warmup)")

print("\nRunning Q1 (The previously slow one)...")
q1 = "Who are the primary NITI Aayog authors credited with writing the National Strategy for Artificial Intelligence report?"
run_query_with_timing(q1, "Q1 (Fast Path)")

print("\nRunning Q1 again...")
run_query_with_timing(q1, "Q1 (Second Run)")
