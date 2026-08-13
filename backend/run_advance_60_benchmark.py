import json
import os
import sys
import time
from pathlib import Path
from typing import Any

if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from src.services.langgraph_pipeline import pipeline

def get_ngrams(text: str, n: int = 3) -> set[str]:
    text = text.lower().replace(" ", "")
    if len(text) < n:
        return set([text])
    return set([text[i:i+n] for i in range(len(text)-n+1)])

def _content_match(retrieved_text: str, expected_text: str) -> bool:
    if not retrieved_text or not expected_text:
        return False
    ret_grams = get_ngrams(retrieved_text)
    exp_grams = get_ngrams(expected_text)
    if not ret_grams or not exp_grams:
        return False
    
    intersection = len(ret_grams.intersection(exp_grams))
    union = len(ret_grams.union(exp_grams))
    jaccard = intersection / union
    
    if expected_text.lower() in retrieved_text.lower():
        return True
    if retrieved_text.lower() in expected_text.lower() and len(retrieved_text) > 50:
        return True
            
    return jaccard >= 0.5

def _faithfulness_score(answer: str, context: str) -> float:
    import re
    if answer in ("NOT_FOUND", ""):
        return 1.0
    
    answer_terms = set(w.lower() for w in re.findall(r"\w+", answer) if len(w) > 3)
    if not answer_terms:
        return 1.0
        
    context_lower = context.lower()
    found = sum(1 for t in answer_terms if t in context_lower)
    return round(found / len(answer_terms), 4)

def run():
    dataset_path = Path("d:/WORK/knowledge-retrieval-engine/backend/tests/data/advance/eval_60_queries.json")
    with open(dataset_path, "r", encoding="utf-8") as f:
        eval_queries = json.load(f)

    total = len(eval_queries)
    print(f"=== Running Held-Out Advance Benchmark (N={total} Non-Overlapping Queries) ===")
    print("=" * 70)

    hits_at_5 = 0
    hits_at_3 = 0
    faith_hits = []
    faith_misses = []
    latencies_ms = []
    fast_path_count = 0
    not_found_count = 0
    results = []

    for i, item in enumerate(eval_queries):
        qid = item.get("id", f"Q{i+1}")
        query = item["query"]
        expected_ans = item.get("expected_answer", "")
        source_doc = item.get("source_file", "")
        q_type = item.get("type", "factual")

        t0 = time.perf_counter()
        try:
            res = pipeline.run(query)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
        except Exception as e:
            print(f"[{i+1}/{total}] {qid} ERROR: {e}")
            continue

        answer = getattr(res, "answer", "")
        citations = getattr(res, "citations", [])
        is_fast = getattr(res, "fast_path", False)
        context_snippet = getattr(res, "context_snippet", "")
        top_chunks = getattr(res, "top_chunks", [])
        confidence = getattr(res, "confidence_score", 0.0)

        # Check Hit at 5
        # Hit if retrieved chunks contain expected answer or matching chunk
        h5 = 0
        h3 = 0
        all_chunk_text = " ".join([c.text for c in top_chunks[:5]])
        if _content_match(all_chunk_text, expected_ans) or (expected_ans.lower() in answer.lower() and answer != "NOT_FOUND"):
            h5 = 1
        
        top3_text = " ".join([c.text for c in top_chunks[:3]])
        if _content_match(top3_text, expected_ans) or (expected_ans.lower() in answer.lower() and answer != "NOT_FOUND"):
            h3 = 1

        if h5:
            hits_at_5 += 1
        if h3:
            hits_at_3 += 1

        if is_fast:
            fast_path_count += 1
        if answer == "NOT_FOUND":
            not_found_count += 1

        # Faithfulness
        faith = _faithfulness_score(answer, context_snippet or all_chunk_text)
        if h5:
            faith_hits.append(faith)
        else:
            faith_misses.append(faith)

        latencies_ms.append(elapsed_ms)

        hit_sym = "✓" if h5 else "✗"
        path_sym = "FAST" if is_fast else "FULL"
        print(f"[{i+1:02d}/{total}] {hit_sym} {qid} R@5={h5} Faith={faith:.2f} Path={path_sym} Latency={elapsed_ms:.0f}ms | {source_doc[:20]} | {query[:45]}...")

        results.append({
            "id": qid,
            "query": query,
            "source_file": source_doc,
            "type": q_type,
            "hit_at_5": h5,
            "faithfulness": faith,
            "fast_path": is_fast,
            "latency_ms": elapsed_ms,
            "answer": answer,
            "expected_answer": expected_ans,
            "confidence": confidence,
        })

    # Summary stats
    scored = len(results)
    recall_at_5 = hits_at_5 / scored if scored else 0.0
    recall_at_3 = hits_at_3 / scored if scored else 0.0
    avg_faith_hits = sum(faith_hits) / len(faith_hits) if faith_hits else 0.0
    avg_faith_misses = sum(faith_misses) / len(faith_misses) if faith_misses else 0.0
    avg_latency = sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0
    llm_activation = 1.0 - (fast_path_count / scored) if scored else 0.0
    fast_path_rate = fast_path_count / scored if scored else 0.0

    print("\n" + "=" * 70)
    print("=== HELD-OUT ADVANCE BENCHMARK RESULTS (N=60) ===")
    print("=" * 70)
    print(f"Recall@5:                   {recall_at_5:.4f}  (Baseline target: > 0.75)")
    print(f"Recall@3:                   {recall_at_3:.4f}")
    print(f"Avg Faithfulness (Hits):    {avg_faith_hits:.4f}  (Baseline target: > 0.85)")
    print(f"Avg Faithfulness (Misses):  {avg_faith_misses:.4f}")
    print(f"Fast-Path Activation Rate:  {fast_path_rate:.4f}  ({fast_path_count}/{scored})")
    print(f"LLM Activation Rate:        {llm_activation:.4f}  (Baseline target: < 0.60)")
    print(f"Average Latency:            {avg_latency:.0f} ms")
    print(f"NOT_FOUND Answers:          {not_found_count}/{scored}")
    print("=" * 70)

    out_file = Path("d:/WORK/knowledge-retrieval-engine/backend/tests/data/advance/benchmark_results_60.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {
                "total_queries": total,
                "scored_queries": scored,
                "recall_at_5": recall_at_5,
                "recall_at_3": recall_at_3,
                "avg_faithfulness_hits": avg_faith_hits,
                "avg_faithfulness_misses": avg_faith_misses,
                "fast_path_activation_rate": fast_path_rate,
                "llm_activation_rate": llm_activation,
                "avg_latency_ms": avg_latency,
                "fast_path_count": fast_path_count,
                "not_found_count": not_found_count,
            },
            "queries": results,
        }, f, indent=2)
    print(f"Detailed results saved to: {out_file}")

if __name__ == "__main__":
    run()
