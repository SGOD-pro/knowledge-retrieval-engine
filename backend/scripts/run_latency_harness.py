#!/usr/bin/env python3
"""Dedicated Latency Benchmark Harness (200 Runs).

Measures p50, p90, p95, p99, min, max, mean, stddev over:
  - 200 fast-path warm runs (cache=False, benchmark_mode=True; Target: p95 < 400ms)
  - 200 full-path warm runs (cache=False, benchmark_mode=True; Target: p95 < 4000ms)
  - Separate exact_response_cache_latency (cache=True; explicitly NOT pipeline latency)
  - Local process cold cache latency (BM25 cache cleared)
  - Lambda cold-start workflow (reports UNMEASURED until multi-sample idle time workflow executes).
"""

import argparse
import json
import logging
import math
from pathlib import Path
import statistics
import sys
import time
from typing import Any

# Ensure backend/src is in sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
SRC_DIR = BACKEND_DIR / "src"

sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from modules.query.query_service import QueryService
from schemas.models import QueryRequest
from services.retrieval.bm25_retriever import invalidate_chunk_cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("latency_harness")


def compute_percentiles(latencies: list[float]) -> dict[str, float]:
    sorted_lats = sorted(latencies)
    n = len(sorted_lats)
    if n == 0:
        return {}

    def get_p(p: float) -> float:
        k = (n - 1) * (p / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return round(sorted_lats[int(k)], 2)
        d0 = sorted_lats[int(f)] * (c - k)
        d1 = sorted_lats[int(c)] * (k - f)
        return round(d0 + d1, 2)

    return {
        "count": n,
        "min": round(min(sorted_lats), 2),
        "p50": get_p(50),
        "p90": get_p(90),
        "p95": get_p(95),
        "p99": get_p(99),
        "max": round(max(sorted_lats), 2),
        "mean": round(statistics.mean(sorted_lats), 2),
        "stddev": round(statistics.stdev(sorted_lats), 2) if n > 1 else 0.0,
    }


def measure_lambda_cold_start() -> dict[str, Any]:
    """Workflow measuring first Lambda invocation latency after verified idle time.
    Until an external orchestrator runs multiple idle iterations, returns UNMEASURED.
    """
    return {
        "status": "UNMEASURED",
        "reason": "Requires externally executable workflow after real idle time (>15min) to collect >=30 cold samples for p95.",
        "samples_collected": 0,
        "target_met": "UNMEASURED",
    }


def run_latency_harness(
    workspace_id: str = "ws_fresh_benchmark",
    num_runs: int = 200,
    cache_runs: int = 50,
    fast_query: str = "What does SDC stand for in On the Interpretability of Attention Networks?",
    full_query: str = "In the uploaded SEC filing, how many common shares were outstanding as of July 17, 2026?",
) -> dict[str, Any]:
    service = QueryService()

    # 1. Local Process Cold Cache Latency
    logger.info("Measuring local_process_cold_cache_latency (cleared BM25 cache)...")
    invalidate_chunk_cache()

    t0 = time.perf_counter()
    req_fast_cold = QueryRequest(query=fast_query, workspace_id=workspace_id, cache=False, benchmark_mode=True)
    resp_fast_cold = service.execute_query(req_fast_cold)
    fast_cold_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    t0 = time.perf_counter()
    req_full_cold = QueryRequest(query=full_query, workspace_id=workspace_id, cache=False, benchmark_mode=True, force_full_path=True)
    resp_full_cold = service.execute_query(req_full_cold)
    full_cold_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    logger.info("Local Process Cold Cache: Fast Path = %.2fms | Full Path = %.2fms", fast_cold_ms, full_cold_ms)

    # 2. Warmup phase for infrastructure connections (Qdrant, Bedrock pool, BGE Lambda)
    logger.info("Warming infrastructure connection pools (3 iterations, pipeline active)...")
    for _ in range(3):
        service.execute_query(QueryRequest(query=fast_query, workspace_id=workspace_id, cache=False, benchmark_mode=True))
        service.execute_query(QueryRequest(query=full_query, workspace_id=workspace_id, cache=False, benchmark_mode=True, force_full_path=True))

    # 3. Fast Path Warm Pipeline Runs (cache=False, benchmark_mode=True)
    logger.info("Executing %d warm runs for FAST path (pipeline_warm_latency)...", num_runs)
    fast_latencies = []
    req_fast_warm = QueryRequest(query=fast_query, workspace_id=workspace_id, cache=False, benchmark_mode=True)

    fast_bedrock_embed_calls = 0
    fast_bge_lambda_calls = 0
    fast_gen_calls = 0

    for i in range(1, num_runs + 1):
        t_start = time.perf_counter()
        resp = service.execute_query(req_fast_warm)
        lat_ms = (time.perf_counter() - t_start) * 1000.0
        fast_latencies.append(lat_ms)

        # Assert no cache hit
        assert not resp.get("cached", False), f"Run {i}: Warm pipeline run returned cached=True"

        # Assert fast-path invariants: zero Bedrock embeddings, zero generation calls
        b_embed = resp.get("bedrock_embedding_calls", 0)
        bge_lam = resp.get("bge_lambda_calls", 0)
        gen = resp.get("generation_calls", 0)

        assert b_embed == 0, f"Run {i}: Fast path made {b_embed} Bedrock embedding calls (expected 0)"
        assert gen == 0, f"Run {i}: Fast path made {gen} generation calls (expected 0)"

        fast_bedrock_embed_calls += b_embed
        fast_bge_lambda_calls += bge_lam
        fast_gen_calls += gen

        if i % 50 == 0 or i == num_runs:
            logger.info("  Fast path progress: %d/%d (latest: %.1fms)", i, num_runs, lat_ms)

    fast_stats = compute_percentiles(fast_latencies)

    # 4. Full Path Warm Pipeline Runs (cache=False, benchmark_mode=True)
    logger.info("Executing %d warm runs for FULL path (pipeline_warm_latency)...", num_runs)
    full_latencies = []
    req_full_warm = QueryRequest(query=full_query, workspace_id=workspace_id, cache=False, benchmark_mode=True, force_full_path=True)

    full_bedrock_embed_calls = 0
    full_bge_lambda_calls = 0
    full_gen_calls = 0

    for i in range(1, num_runs + 1):
        t_start = time.perf_counter()
        resp = service.execute_query(req_full_warm)
        lat_ms = (time.perf_counter() - t_start) * 1000.0
        full_latencies.append(lat_ms)

        # Assert no cache hit
        assert not resp.get("cached", False), f"Run {i}: Warm pipeline run returned cached=True"

        # Assert full-path invariants: exactly one remote Titan embedding, at most one generation call
        b_embed = resp.get("bedrock_embedding_calls", 0)
        bge_lam = resp.get("bge_lambda_calls", 0)
        gen = resp.get("generation_calls", 0)

        assert b_embed == 1, f"Run {i}: Full path made {b_embed} Bedrock embedding calls (expected 1)"
        assert gen <= 1, f"Run {i}: Full path made {gen} generation calls (expected <= 1)"

        full_bedrock_embed_calls += b_embed
        full_bge_lambda_calls += bge_lam
        full_gen_calls += gen

        if i % 50 == 0 or i == num_runs:
            logger.info("  Full path progress: %d/%d (latest: %.1fms)", i, num_runs, lat_ms)

    full_stats = compute_percentiles(full_latencies)

    # 5. Exact Response-Cache Latency (Benchmarked separately with cache=True)
    logger.info("Benchmarking exact_response_cache_latency separately (%d runs, cache=True)...", cache_runs)
    # Prime the cache
    req_prime = QueryRequest(query=fast_query, workspace_id=workspace_id, cache=True)
    service.execute_query(req_prime)

    cache_latencies = []
    for i in range(1, cache_runs + 1):
        t_start = time.perf_counter()
        c_resp = service.execute_query(req_prime)
        c_lat_ms = (time.perf_counter() - t_start) * 1000.0
        cache_latencies.append(c_lat_ms)
        assert c_resp.get("cached", False), f"Cache run {i} failed to hit response cache"

    cache_stats = compute_percentiles(cache_latencies)

    fast_target_met = fast_stats["p95"] < 400.0
    full_target_met = full_stats["p95"] < 4000.0

    report = {
        "timestamp": time.strftime("%Y%m%d_%H%M%S"),
        "workspace_id": workspace_id,
        "num_runs": num_runs,
        "local_process_cold_cache_latency_ms": {
            "fast_path": fast_cold_ms,
            "full_path": full_cold_ms,
            "note": "Measured once after in-process cache invalidation; does not represent Lambda cold start.",
        },
        "lambda_cold_start": measure_lambda_cold_start(),
        "pipeline_warm_latency": {
            "fast_path": {
                "target_p95_ms": 400.0,
                "target_met": fast_target_met,
                "statistics": fast_stats,
                "telemetry_averages": {
                    "bedrock_embedding_calls": round(fast_bedrock_embed_calls / num_runs, 2),
                    "bge_lambda_calls": round(fast_bge_lambda_calls / num_runs, 2),
                    "generation_calls": round(fast_gen_calls / num_runs, 2),
                },
            },
            "full_path": {
                "target_p95_ms": 4000.0,
                "target_met": full_target_met,
                "statistics": full_stats,
                "telemetry_averages": {
                    "bedrock_embedding_calls": round(full_bedrock_embed_calls / num_runs, 2),
                    "bge_lambda_calls": round(full_bge_lambda_calls / num_runs, 2),
                    "generation_calls": round(full_gen_calls / num_runs, 2),
                },
            },
        },
        "exact_response_cache_latency — NOT retrieval pipeline latency": {
            "description": "Measures cached response lookups with cache=True. Must NEVER be compared to retrieval pipeline SLAs.",
            "statistics": cache_stats,
        },
    }

    # Write report
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    report_dir = BACKEND_DIR / "reports" / "latency" / timestamp
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / "latency_report.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    logger.info("Saved latency report to %s", report_file)

    print("\n==================================================================")
    print("                 LATENCY HARNESS RESULTS                          ")
    print("==================================================================")
    print(f"Local Process Cold Cache: Fast={fast_cold_ms:.1f}ms | Full={full_cold_ms:.1f}ms")
    print(f"Lambda Cold Start: UNMEASURED (Target Met: UNMEASURED)\n")
    print(f"Fast Path Warm Pipeline (Target: p95 < 400ms):")
    print(f"  p50={fast_stats['p50']}ms | p90={fast_stats['p90']}ms | p95={fast_stats['p95']}ms | p99={fast_stats['p99']}ms")
    print(f"  Min={fast_stats['min']}ms | Mean={fast_stats['mean']}ms | Max={fast_stats['max']}ms | StdDev={fast_stats['stddev']}ms")
    print(f"  Bedrock Embed Calls={round(fast_bedrock_embed_calls / num_runs, 2)} | Gen Calls={round(fast_gen_calls / num_runs, 2)}")
    print(f"  Target Met: {'PASS' if fast_target_met else 'FAIL'}\n")
    print(f"Full Path Warm Pipeline (Target: p95 < 4000ms):")
    print(f"  p50={full_stats['p50']}ms | p90={full_stats['p90']}ms | p95={full_stats['p95']}ms | p99={full_stats['p99']}ms")
    print(f"  Min={full_stats['min']}ms | Mean={full_stats['mean']}ms | Max={full_stats['max']}ms | StdDev={full_stats['stddev']}ms")
    print(f"  Bedrock Embed Calls={round(full_bedrock_embed_calls / num_runs, 2)} | Gen Calls={round(full_gen_calls / num_runs, 2)}")
    print(f"  Target Met: {'PASS' if full_target_met else 'FAIL'}\n")
    print("Exact Response-Cache (NOT retrieval pipeline latency):")
    print(f"  p50={cache_stats['p50']}ms | p90={cache_stats['p90']}ms | p95={cache_stats['p95']}ms | Mean={cache_stats['mean']}ms")
    print("==================================================================\n")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dedicated Latency Harness.")
    parser.add_argument("--workspace-id", default="ws_fresh_benchmark", help="Workspace ID")
    parser.add_argument("--runs", type=int, default=200, help="Number of warm pipeline runs")
    parser.add_argument("--cache-runs", type=int, default=50, help="Number of cache-only runs")
    args = parser.parse_args()

    run_latency_harness(workspace_id=args.workspace_id, num_runs=args.runs, cache_runs=args.cache_runs)
