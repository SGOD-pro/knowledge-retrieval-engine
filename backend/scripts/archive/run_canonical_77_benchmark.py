raise RuntimeError("Obsolete runner. Use backend/scripts/run_canonical_60_benchmark.py")
import os
import sys
import json
import time
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
    print("=== CANONICAL 77-QUERY BENCHMARK (LIVE DUAL EMBEDDINGS + OKF GRAPH) ===")
    print("=" * 85)

    base_results_path = (
        Path(__file__).resolve().parent.parent
        / "tmp"
        / "full_77_benchmark_results.json"
    )
    with open(base_results_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    benchmark_queries = data.get("queries", [])
    print(
        f"Loaded {len(benchmark_queries)} canonical benchmark queries from {base_results_path.name}\n"
    )

    results = []
    latencies = []
    hits_at_5 = 0
    hits_at_3 = 0
    fast_path_count = 0
    full_path_count = 0
    faith_scores = []
    real_faith_scores = []
    not_found_count = 0

    per_doc_stats = {}

    for i, item in enumerate(benchmark_queries, 1):
        qid = item.get("id", f"Q{i:03d}")
        query = item.get("query", "")
        source_file = item.get("source_file", "unknown")
        expected_ans = item.get("expected_answer", "")

        if source_file not in per_doc_stats:
            per_doc_stats[source_file] = {
                "total": 0,
                "hits_5": 0,
                "hits_3": 0,
                "fast": 0,
                "full": 0,
                "not_found": 0,
            }
        per_doc_stats[source_file]["total"] += 1

        t0 = time.perf_counter()
        try:
            res = pipeline.run(query=query, workspace_id="ws_001")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed_ms)

            fast_path = getattr(res, "fast_path", False)
            if fast_path:
                fast_path_count += 1
                per_doc_stats[source_file]["fast"] += 1
            else:
                full_path_count += 1
                per_doc_stats[source_file]["full"] += 1

            ans = getattr(res, "answer", "")
            citations = getattr(res, "citations", [])
            top_chunks = getattr(res, "top_chunks", [])

            # Combine retrieved text
            context_text = " ".join([c.text for c in top_chunks[:5]])

            # Check hits
            h5 = 0
            h3 = 0

            # Content match or citation page match
            top5_text = " ".join([c.text for c in top_chunks[:5]])
            if _content_match(top5_text, expected_ans) or (
                expected_ans
                and expected_ans.lower() in ans.lower()
                and ans not in ("NOT_FOUND", "")
            ):
                h5 = 1

            top3_text = " ".join([c.text for c in top_chunks[:3]])
            if _content_match(top3_text, expected_ans) or (
                expected_ans
                and expected_ans.lower() in ans.lower()
                and ans not in ("NOT_FOUND", "")
            ):
                h3 = 1

            hits_at_5 += h5
            hits_at_3 += h3
            per_doc_stats[source_file]["hits_5"] += h5
            per_doc_stats[source_file]["hits_3"] += h3

            # Faithfulness
            faith = _faithfulness_score(ans, context_text)
            if ans in ("NOT_FOUND", "") or "context does not provide" in ans.lower():
                not_found_count += 1
                per_doc_stats[source_file]["not_found"] += 1
            elif faith is not None:
                real_faith_scores.append(faith)

            symbol = "✓" if h5 == 1 else "✗"
            path_str = "FAST" if fast_path else "FULL"
            faith_str = f"{faith:.2f}" if faith is not None else "N/A"
            print(
                f"[{i:02d}/{len(benchmark_queries)}] {symbol} {qid:<7} R@5={h5} Faith={faith_str} "
                f"Path={path_str:<4} Latency={elapsed_ms:5.0f}ms | {source_file:<45} | {query[:45]}..."
            )

            results.append(
                {
                    "id": qid,
                    "query": query,
                    "source_file": source_file,
                    "expected_answer": expected_ans,
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
            print(f"[{i:02d}/{len(benchmark_queries)}] ERROR on {qid}: {e}")
            results.append(
                {
                    "id": qid,
                    "query": query,
                    "source_file": source_file,
                    "error": str(e),
                    "hit_at_5": 0,
                    "hit_at_3": 0,
                    "latency_ms": elapsed_ms,
                }
            )

    total_queries = len(benchmark_queries)
    recall_at_5 = hits_at_5 / total_queries
    recall_at_3 = hits_at_3 / total_queries
    llm_activation_rate = full_path_count / total_queries
    fast_path_rate = fast_path_count / total_queries
    avg_latency = np.mean(latencies) if latencies else 0.0
    p95_latency = np.percentile(latencies, 95) if latencies else 0.0
    median_latency = np.median(latencies) if latencies else 0.0
    avg_real_faith = np.mean(real_faith_scores) if real_faith_scores else 0.0
    abstention_rate = not_found_count / total_queries

    print("\n" + "=" * 85)
    print("=== FINAL CANONICAL 77-QUERY BENCHMARK SUMMARY (OKF LIVE) ===")
    print("=" * 85)
    print(
        f"Total Queries Scored:              {total_queries} / {total_queries} (100.0%)"
    )
    print(
        f"Recall@5:                          {recall_at_5:.4f} ({hits_at_5}/{total_queries} = {recall_at_5*100:.2f}%)"
    )
    print(
        f"Recall@3:                          {recall_at_3:.4f} ({hits_at_3}/{total_queries} = {recall_at_3*100:.2f}%)"
    )
    print(
        f"LLM Activation Rate (Full Path):   {llm_activation_rate:.4f} ({full_path_count}/{total_queries} = {llm_activation_rate*100:.2f}%)"
    )
    print(
        f"Fast-Path Rate:                    {fast_path_rate:.4f} ({fast_path_count}/{total_queries} = {fast_path_rate*100:.2f}%)"
    )
    print(f"Mean Latency:                      {avg_latency:.0f} ms")
    print(f"Median Latency:                    {median_latency:.0f} ms")
    print(f"p95 Latency:                       {p95_latency:.0f} ms")
    print(
        f"Real-Answer Faithfulness:          {avg_real_faith:.4f} (on {len(real_faith_scores)} answers)"
    )
    print(
        f"NOT_FOUND Abstentions:             {not_found_count}/{total_queries} ({abstention_rate*100:.1f}%)"
    )
    print("=" * 85)

    print("\n" + "-" * 85)
    print(
        f"{'DOCUMENT / CORPUS':<52} | {'TOTAL':<5} | {'HITS@5':<6} | {'RECALL@5':<8} | {'FAST':<4} | {'FULL':<4}"
    )
    print("-" * 85)
    for doc_name, s in sorted(per_doc_stats.items()):
        rec = s["hits_5"] / s["total"] if s["total"] else 0.0
        print(
            f"{doc_name:<52} | {s['total']:<5} | {s['hits_5']:<6} | {rec:<8.4f} | {s['fast']:<4} | {s['full']:<4}"
        )
    print("=" * 85)

    # Save output JSON
    tmp_dir = Path(__file__).resolve().parent.parent / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    out_file = tmp_dir / "canonical_77_benchmark_live_okf.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "summary": {
                    "total_queries": total_queries,
                    "recall_at_5": recall_at_5,
                    "recall_at_3": recall_at_3,
                    "llm_activation_rate": llm_activation_rate,
                    "fast_path_rate": fast_path_rate,
                    "mean_latency_ms": avg_latency,
                    "median_latency_ms": median_latency,
                    "p95_latency_ms": p95_latency,
                    "real_answer_faithfulness": avg_real_faith,
                    "not_found_count": not_found_count,
                    "abstention_rate": abstention_rate,
                },
                "per_document": per_doc_stats,
                "queries": results,
            },
            f,
            indent=2,
        )
    print(f"\nDetailed raw results saved to: {out_file}\n")


if __name__ == "__main__":
    main()
