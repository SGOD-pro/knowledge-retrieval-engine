import json
import sys
import time
from pathlib import Path

if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from src.services.langgraph_pipeline import pipeline
from run_advance_60_benchmark import _content_match, _faithfulness_score

def run():
    print("=== Phase Y Investigation ===")
    
    # 1. Load benchmark results
    results_path = Path("d:/WORK/knowledge-retrieval-engine/backend/tests/data/advance/benchmark_results_60.json")
    with open(results_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    queries = data["queries"]
    total_queries = len(queries)
    
    # --- Y1: Ground Truth Quality Check ---
    print("\n--- Y1: Ground Truth Quality Check ---")
    suspect_count = 0
    for q in queries:
        expected = q.get("expected_answer", "").strip()
        if not expected or expected.lower() == "not_found" or len(expected) < 2:
            print(f"Suspect entry: {q['id']} - Expected Answer is empty or invalid: '{expected}'")
            suspect_count += 1
            
    print(f"Total suspect entries: {suspect_count} / {total_queries}")
    if suspect_count == 0:
        print("Ground truth appears complete and verifiable.")
        
    # --- Y3: Breakdown by document format ---
    print("\n--- Y3: Recall@5 Breakdown by Document Format ---")
    format_stats = {}
    for q in queries:
        src = q["source_file"]
        h5 = q["hit_at_5"]
        if src not in format_stats:
            format_stats[src] = {"total": 0, "hits": 0, "fast_path": 0}
        format_stats[src]["total"] += 1
        format_stats[src]["hits"] += h5
        format_stats[src]["fast_path"] += 1 if q.get("fast_path") else 0
        
    for src, stats in format_stats.items():
        recall = stats["hits"] / stats["total"] if stats["total"] else 0
        fast_rate = stats["fast_path"] / stats["total"] if stats["total"] else 0
        print(f"{src:45s} | Recall@5: {recall:.2f} ({stats['hits']}/{stats['total']}) | FastPath: {fast_rate:.2f}")

    # --- Y2: Isolate routing failure from retrieval failure ---
    print("\n--- Y2: Isolate Routing vs Retrieval Failure ---")
    misses = [q for q in queries if q["hit_at_5"] == 0]
    print(f"Total misses to investigate: {len(misses)}")
    
    recovered_count = 0
    still_miss_count = 0
    
    for i, q in enumerate(misses):
        qid = q["id"]
        query_text = q["query"]
        expected_ans = q["expected_answer"]
        src = q["source_file"]
        
        # Only re-run fast-path misses, as full-path misses are already confirmed retrieval failures
        was_fast = q.get("fast_path", False)
        if not was_fast:
            print(f"[{i+1}/{len(misses)}] {qid} was already FULL path and failed -> Retrieval failure.")
            still_miss_count += 1
            continue
            
        print(f"[{i+1}/{len(misses)}] Re-running {qid} on FULL PATH... ({src})")
        t0 = time.perf_counter()
        
        try:
            res = pipeline.run(query_text, force_full_path=True)
        except Exception as e:
            print(f"  Error: {e}")
            still_miss_count += 1
            continue
            
        answer = getattr(res, "answer", "")
        top_chunks = getattr(res, "top_chunks", [])
        
        all_chunk_text = " ".join([c.text for c in top_chunks[:5]])
        
        h5 = 0
        if _content_match(all_chunk_text, expected_ans) or (expected_ans.lower() in answer.lower() and answer != "NOT_FOUND"):
            h5 = 1
            
        if h5:
            recovered_count += 1
            print(f"  -> RECOVERED! The LLM successfully answered the question using deep retrieval chunks.")
        else:
            still_miss_count += 1
            print(f"  -> STILL MISSED! Full path + LLM could not answer. (Retrieval failure)")
            
    print(f"\nY2 Summary: Out of {len(misses)} original misses, forcing full-path RECOVERED {recovered_count}. {still_miss_count} remained misses.")
    
    if recovered_count > still_miss_count:
        print("Conclusion: The majority of misses were due to ROUTING failures (Fast-Path short-circuiting valid chunks).")
    else:
        print("Conclusion: The majority of misses are RETRIEVAL failures (Chunks missing or LLM unable to synthesize).")

if __name__ == "__main__":
    run()
