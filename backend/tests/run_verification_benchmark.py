import os
import sys
import json
import time
import random
import re
import numpy as np
from pathlib import Path
from dotenv import load_dotenv

# Ensure src is on python path and env is loaded
backend_dir = Path(__file__).resolve().parent.parent
load_dotenv(backend_dir / ".env")
sys.path.insert(0, str(backend_dir / "src"))

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from config import settings
from services.langgraph_pipeline import pipeline
from run_benchmark import _parse_page


from services.evaluation.benchmark_scorer import content_match as _content_match
from services.evaluation.benchmark_scorer import compute_faithfulness as _faithfulness_score



def main():
    print("=" * 85)
    print("=== CANONICAL 60-QUERY VERIFICATION BENCHMARK (random.seed=42) ===")
    print("=" * 85)

    data_path = Path(__file__).resolve().parent / "data" / "benchmark_queries.json"
    with open(data_path, "r", encoding="utf-8") as f:
        all_queries = json.load(f)

    print(f"Loaded {len(all_queries)} total candidate queries from {data_path.name}")

    # Deterministic reproducible sampling
    random.seed(42)
    sample_size = min(60, len(all_queries))
    sampled_queries = random.sample(all_queries, sample_size)
    print(f"Sampled {len(sampled_queries)} queries with random.seed(42)\n")

    results = []
    latencies = []
    hits_at_5 = 0
    hits_at_3 = 0
    fast_path_count = 0
    full_path_count = 0
    faith_scores = []
    real_faith_scores = []
    not_found_count = 0

    for i, item in enumerate(sampled_queries, 1):
        qid = item.get("id", f"Q{i:03d}")
        query = item.get("query", "")
        doc_filename = item.get("document_filename", "Unknown")
        expected_ans = item.get("expected_answer", "")
        expected_page = item.get("source_page") or item.get("page")

        t0 = time.perf_counter()
        try:
            res = pipeline.run(query=query, workspace_id="ws_001")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed_ms)

            fast_path = getattr(res, "fast_path", False)
            if fast_path:
                fast_path_count += 1
            else:
                full_path_count += 1

            ans = getattr(res, "answer", "")
            citations = getattr(res, "citations", [])
            top_chunks = getattr(res, "top_chunks", [])

            # Combine retrieved text
            context_text = " ".join([c.text for c in top_chunks[:5]])

            # Check hits
            h5 = 0
            h3 = 0

            # 1. Page match
            retrieved_pages_5 = set()
            for cit in citations[:5]:
                p = _parse_page(cit)
                if p is not None:
                    retrieved_pages_5.add(p)

            retrieved_pages_3 = set()
            for cit in citations[:3]:
                p = _parse_page(cit)
                if p is not None:
                    retrieved_pages_3.add(p)

            if expected_page is not None:
                if expected_page in retrieved_pages_5:
                    h5 = 1
                if expected_page in retrieved_pages_3:
                    h3 = 1

            # 2. Content match fallback
            if h5 == 0:
                top5_text = " ".join([c.text for c in top_chunks[:5]])
                if _content_match(top5_text, expected_ans) or (
                    expected_ans
                    and expected_ans.lower() in ans.lower()
                    and ans != "NOT_FOUND"
                ):
                    h5 = 1

            if h3 == 0:
                top3_text = " ".join([c.text for c in top_chunks[:3]])
                if _content_match(top3_text, expected_ans) or (
                    expected_ans
                    and expected_ans.lower() in ans.lower()
                    and ans != "NOT_FOUND"
                ):
                    h3 = 1

            hits_at_5 += h5
            hits_at_3 += h3

            # Faithfulness
            faith = _faithfulness_score(ans, context_text)
            if ans == "NOT_FOUND" or not ans:
                not_found_count += 1
            elif faith is not None:
                real_faith_scores.append(faith)

            symbol = "✓" if h5 == 1 else "✗"
            path_str = "FAST" if fast_path else "FULL"
            faith_str = f"{faith:.2f}" if faith is not None else "N/A"
            print(
                f"[{i:02d}/{sample_size}] {symbol} {qid:<6} R@5={h5} Faith={faith_str} Path={path_str:<4} Latency={elapsed_ms:5.0f}ms | {doc_filename:<35} | {query[:45]}..."
            )

            results.append(
                {
                    "id": qid,
                    "query": query,
                    "document_filename": doc_filename,
                    "expected_answer": expected_ans,
                    "expected_page": expected_page,
                    "actual_answer": ans,
                    "fast_path": fast_path,
                    "hit_at_5": h5,
                    "hit_at_3": h3,
                    "faithfulness": faith,
                    "latency_ms": elapsed_ms,
                    "citations": citations[:5],
                }
            )

        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            print(f"[{i:02d}/{sample_size}] ERROR on {qid}: {e}")
            results.append(
                {
                    "id": qid,
                    "query": query,
                    "document_filename": doc_filename,
                    "error": str(e),
                    "hit_at_5": 0,
                    "hit_at_3": 0,
                    "latency_ms": elapsed_ms,
                }
            )

    # Calculate aggregate metrics
    recall_at_5 = hits_at_5 / sample_size
    recall_at_3 = hits_at_3 / sample_size
    llm_activation_rate = full_path_count / sample_size
    avg_latency = np.mean(latencies) if latencies else 0.0
    p95_latency = np.percentile(latencies, 95) if latencies else 0.0
    median_latency = np.median(latencies) if latencies else 0.0
    avg_real_faith = np.mean(real_faith_scores) if real_faith_scores else 0.0
    abstention_rate = not_found_count / sample_size

    print("\n" + "=" * 85)
    print("=== CANONICAL 60-QUERY VERIFICATION BENCHMARK SUMMARY ===")
    print("=" * 85)
    print(f"Total Sampled Queries:             {sample_size}")
    print(
        f"Recall@5:                          {recall_at_5:.4f} ({hits_at_5}/{sample_size})"
    )
    print(
        f"Recall@3:                          {recall_at_3:.4f} ({hits_at_3}/{sample_size})"
    )
    print(
        f"LLM Activation Rate (Full Path):   {llm_activation_rate:.4f} ({full_path_count}/{sample_size})"
    )
    print(
        f"Fast-Path Rate:                    {fast_path_count / sample_size:.4f} ({fast_path_count}/{sample_size})"
    )
    print(f"Mean Latency:                      {avg_latency:.0f} ms")
    print(f"Median Latency:                    {median_latency:.0f} ms")
    print(f"p95 Latency:                       {p95_latency:.0f} ms")
    print(
        f"Real-Answer Faithfulness:          {avg_real_faith:.4f} (on {len(real_faith_scores)} answers)"
    )
    print(
        f"NOT_FOUND Abstentions:             {not_found_count}/{sample_size} ({abstention_rate*100:.1f}%)"
    )
    print("=" * 85)

    # Save output JSON
    tmp_dir = Path(__file__).resolve().parent.parent / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    out_file = tmp_dir / "verification_benchmark_60.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "metrics": {
                    "total_queries": sample_size,
                    "recall_at_5": recall_at_5,
                    "recall_at_3": recall_at_3,
                    "llm_activation_rate": llm_activation_rate,
                    "fast_path_rate": fast_path_count / sample_size,
                    "mean_latency_ms": avg_latency,
                    "median_latency_ms": median_latency,
                    "p95_latency_ms": p95_latency,
                    "real_answer_faithfulness": avg_real_faith,
                    "not_found_count": not_found_count,
                    "abstention_rate": abstention_rate,
                },
                "queries": results,
            },
            f,
            indent=2,
        )
    print(f"\nDetailed raw results saved to: {out_file}\n")


if __name__ == "__main__":
    main()
