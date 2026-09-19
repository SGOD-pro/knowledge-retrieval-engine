#!/usr/bin/env python3
"""Dedicated Latency Benchmark Harness (200 Runs).

Measures p50, p90, p95, p99, min, max, mean, stddev over:
  - 200 fast-path warm runs (Target: p95 < 400ms)
  - 200 full-path warm runs (Target: p95 < 4000ms)
  - Cold-start measurements with cleared caches.
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


def run_latency_harness(
    workspace_id: str = "ws_fresh_benchmark",
    num_runs: int = 200,
    fast_query: str = "According to the uploaded SEC filing, what is Apple's Commission File Number?",
    full_query: str = "In the uploaded SEC filing, how many common shares were outstanding as of July 17, 2026?",
) -> dict[str, Any]:
    service = QueryService()

    logger.info("Running cold start measurements...")
    invalidate_chunk_cache()

    # Fast cold run
    t0 = time.perf_counter()
    req_fast_cold = QueryRequest(query=fast_query, workspace_id=workspace_id, cache=False, benchmark_mode=True)
    resp_fast_cold = service.execute_query(req_fast_cold)
    fast_cold_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    # Full cold run
    t0 = time.perf_counter()
    req_full_cold = QueryRequest(query=full_query, workspace_id=workspace_id, cache=False, benchmark_mode=True, force_full_path=True)
    resp_full_cold = service.execute_query(req_full_cold)
    full_cold_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    logger.info("Cold Start: Fast Path = %.2fms | Full Path = %.2fms", fast_cold_ms, full_cold_ms)

    # Warmup phase (5 iterations)
    logger.info("Warming up connections (5 iterations)...")
    for _ in range(5):
        service.execute_query(QueryRequest(query=fast_query, workspace_id=workspace_id, cache=True))
        service.execute_query(QueryRequest(query=full_query, workspace_id=workspace_id, cache=True, force_full_path=True))

    # 1. Fast path warm runs
    logger.info("Executing %d warm runs for FAST path...", num_runs)
    fast_latencies = []
    req_fast_warm = QueryRequest(query=fast_query, workspace_id=workspace_id, cache=True)
    for i in range(1, num_runs + 1):
        t_start = time.perf_counter()
        resp = service.execute_query(req_fast_warm)
        lat_ms = (time.perf_counter() - t_start) * 1000.0
        fast_latencies.append(lat_ms)
        if i % 50 == 0 or i == num_runs:
            logger.info("  Fast path progress: %d/%d (latest: %.1fms)", i, num_runs, lat_ms)

    fast_stats = compute_percentiles(fast_latencies)

    # 2. Full path warm runs
    logger.info("Executing %d warm runs for FULL path...", num_runs)
    full_latencies = []
    req_full_warm = QueryRequest(query=full_query, workspace_id=workspace_id, cache=True, force_full_path=True)
    for i in range(1, num_runs + 1):
        t_start = time.perf_counter()
        resp = service.execute_query(req_full_warm)
        lat_ms = (time.perf_counter() - t_start) * 1000.0
        full_latencies.append(lat_ms)
        if i % 50 == 0 or i == num_runs:
            logger.info("  Full path progress: %d/%d (latest: %.1fms)", i, num_runs, lat_ms)

    full_stats = compute_percentiles(full_latencies)

    fast_target_met = fast_stats["p95"] < 400.0
    full_target_met = full_stats["p95"] < 4000.0

    report = {
        "timestamp": time.strftime("%Y%m%d_%H%M%S"),
        "workspace_id": workspace_id,
        "num_runs": num_runs,
        "cold_start_ms": {
            "fast_path": fast_cold_ms,
            "full_path": full_cold_ms,
        },
        "fast_path_warm": {
            "target_p95_ms": 400.0,
            "target_met": fast_target_met,
            "statistics": fast_stats,
        },
        "full_path_warm": {
            "target_p95_ms": 4000.0,
            "target_met": full_target_met,
            "statistics": full_stats,
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
    print("                 LATENCY HARNESS RESULTS (200 RUNS)               ")
    print("==================================================================")
    print(f"Cold Start: Fast={fast_cold_ms:.1f}ms | Full={full_cold_ms:.1f}ms\n")
    print(f"Fast Path Warm (Target: p95 < 400ms):")
    print(f"  p50={fast_stats['p50']}ms | p90={fast_stats['p90']}ms | p95={fast_stats['p95']}ms | p99={fast_stats['p99']}ms")
    print(f"  Min={fast_stats['min']}ms | Mean={fast_stats['mean']}ms | Max={fast_stats['max']}ms | StdDev={fast_stats['stddev']}ms")
    print(f"  Target Met: {'PASS' if fast_target_met else 'FAIL'}\n")
    print(f"Full Path Warm (Target: p95 < 4000ms):")
    print(f"  p50={full_stats['p50']}ms | p90={full_stats['p90']}ms | p95={full_stats['p95']}ms | p99={full_stats['p99']}ms")
    print(f"  Min={full_stats['min']}ms | Mean={full_stats['mean']}ms | Max={full_stats['max']}ms | StdDev={full_stats['stddev']}ms")
    print(f"  Target Met: {'PASS' if full_target_met else 'FAIL'}")
    print("==================================================================\n")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dedicated 200-Run Latency Harness.")
    parser.add_argument("--workspace-id", default="ws_fresh_benchmark", help="Workspace ID")
    parser.add_argument("--runs", type=int, default=200, help="Number of warm runs")
    args = parser.parse_args()

    run_latency_harness(workspace_id=args.workspace_id, num_runs=args.runs)
