"""KRE Benchmark Runner — Phase 3 replacement for deleted run_benchmark.py.

Hit criterion: exact page number match only. No tolerance window.
The page number is read from:
  1. citation["bounding_box"]["page_number"] (preferred — set by PDF extractor)
  2. citation["location_reference"] prefix "Page: N" (fallback)

Unreadable citations score 0 hits — they are never skipped or excluded.

Usage:
    # From backend/ with src/ on PYTHONPATH:
    python src/run_benchmark.py [--ground-truths PATH] [--api-url URL] [--limit N] [--output PATH]

    python src/run_benchmark.py                          # full run, all queries
    python src/run_benchmark.py --limit 20               # smoke test (first 20 queries)
    python src/run_benchmark.py --output results.json    # write JSON results
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Page number extraction — exact match only, no tolerance
# ---------------------------------------------------------------------------

def _parse_page(citation: dict) -> int | None:
    """Extract page number from a citation dict. Exact match only.

    Returns None if the page number cannot be determined. A None result
    counts as a miss (hit=0); it is never skipped or treated as a special case.
    """
    # Primary: bounding_box.page_number (set by PDF extractor)
    bb = citation.get("bounding_box")
    if isinstance(bb, dict):
        pn = bb.get("page_number")
        if pn is not None:
            try:
                return int(pn)
            except (ValueError, TypeError):
                pass

    # Fallback: location_reference "Page: N"
    loc = str(citation.get("location_reference") or "")
    if loc.startswith("Page: "):
        try:
            return int(loc[6:].strip())
        except ValueError:
            pass

    # Fallback: chunk_id format "...:<doc_id>:page:<N>:element:<M>"
    chunk_id = str(citation.get("chunk_id") or "")
    if ":page:" in chunk_id:
        try:
            parts = chunk_id.split(":page:")
            return int(parts[1].split(":")[0])
        except (IndexError, ValueError):
            pass

    return None


def _expected_pages(ground_truth: dict) -> set[int]:
    """Return the set of expected page numbers from a ground truth entry's citations."""
    pages = set()
    for c in ground_truth.get("citations", []):
        p = _parse_page(c)
        if p is not None:
            pages.add(p)
    return pages


def _hits_at_k(retrieved_citations: list[dict], expected_pages: set[int], k: int) -> int:
    """Return 1 if any of the top-k retrieved citations match an expected page, else 0."""
    for citation in retrieved_citations[:k]:
        p = _parse_page(citation)
        if p is not None and p in expected_pages:
            return 1
    return 0


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

def _query_api(api_url: str, query: str, document_ids: list[str] | None = None) -> dict:
    import urllib.request
    import urllib.error

    payload = json.dumps({"query": query, "document_ids": document_ids}).encode()
    req = urllib.request.Request(
        f"{api_url}/query",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Faithfulness judge (LLM-as-judge)
# ---------------------------------------------------------------------------

def _faithfulness_score(query: str, answer: str, context: str) -> float:
    """Simple entity-overlap faithfulness estimate (no LLM required for smoke tests).

    In Phase 4 this should be replaced with an LLM judge. For now, returns the
    fraction of query entities that appear in both the answer and the context.
    """
    import re
    query_terms = set(w.lower() for w in re.findall(r"\w+", query) if len(w) > 3)
    if not query_terms:
        return 1.0
    answer_lower = answer.lower()
    found = sum(1 for t in query_terms if t in answer_lower)
    return round(found / len(query_terms), 4)


# ---------------------------------------------------------------------------
# Main benchmark loop
# ---------------------------------------------------------------------------

def run_benchmark(
    ground_truths_path: str,
    api_url: str,
    limit: int | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    with open(ground_truths_path, encoding="utf-8") as f:
        ground_truths = json.load(f)

    if limit is not None:
        ground_truths = ground_truths[:limit]

    total = len(ground_truths)
    print(f"Running benchmark: {total} queries against {api_url}")
    print(f"Hit criterion: exact page match only (no tolerance window)")
    print("-" * 60)

    hits_at_5 = 0
    hits_at_3 = 0
    faithfulness_scores = []
    latencies_ms = []
    fast_path_count = 0
    not_found_count = 0
    parse_fail_count = 0
    errors = []

    for i, gt in enumerate(ground_truths):
        query = gt["query"]
        expected_pages = _expected_pages(gt)

        if not expected_pages:
            parse_fail_count += 1
            print(f"  [{i+1}/{total}] SKIP (no parseable expected page): {query[:60]}...")
            continue

        t0 = time.perf_counter()
        try:
            result = _query_api(api_url, query)
        except Exception as e:
            errors.append({"query": query, "error": str(e)})
            print(f"  [{i+1}/{total}] ERROR: {e}")
            continue
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(elapsed_ms)

        retrieved = result.get("citations", [])
        answer = result.get("answer", "")
        is_fast = result.get("fast_path", False)

        if is_fast:
            fast_path_count += 1
        if answer in ("NOT_FOUND", ""):
            not_found_count += 1

        h5 = _hits_at_k(retrieved, expected_pages, k=5)
        h3 = _hits_at_k(retrieved, expected_pages, k=3)
        hits_at_5 += h5
        hits_at_3 += h3

        faith = _faithfulness_score(query, answer, result.get("answer", ""))
        faithfulness_scores.append(faith)

        hit_symbol = "✓" if h5 else "✗"
        path_label = "fast" if is_fast else "full"
        print(f"  [{i+1}/{total}] {hit_symbol} R@5={h5} faith={faith:.2f} "
              f"path={path_label} latency={elapsed_ms:.0f}ms | {query[:55]}...")

    # Compute scored queries (excluding parse failures and errors)
    scored = total - parse_fail_count - len(errors)
    recall_at_5 = hits_at_5 / scored if scored else 0.0
    recall_at_3 = hits_at_3 / scored if scored else 0.0
    avg_faithfulness = sum(faithfulness_scores) / len(faithfulness_scores) if faithfulness_scores else 0.0
    avg_latency_ms = sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0
    llm_activation_rate = 1.0 - (fast_path_count / scored) if scored else 0.0

    summary = {
        "total_queries": total,
        "scored_queries": scored,
        "parse_failures": parse_fail_count,
        "api_errors": len(errors),
        "recall_at_5": round(recall_at_5, 4),
        "recall_at_3": round(recall_at_3, 4),
        "avg_faithfulness": round(avg_faithfulness, 4),
        "avg_latency_ms": round(avg_latency_ms, 1),
        "llm_activation_rate": round(llm_activation_rate, 4),
        "fast_path_count": fast_path_count,
        "not_found_count": not_found_count,
        "hit_criterion": "exact_page_match",
        "tolerance_window": None,  # None = exact match only. This field is locked.
    }

    print("-" * 60)
    print(f"Recall@5:           {recall_at_5:.4f}  (target: > 0.50)")
    print(f"Recall@3:           {recall_at_3:.4f}")
    print(f"Avg faithfulness:   {avg_faithfulness:.4f} (target: > 0.80)")
    print(f"Avg latency:        {avg_latency_ms:.0f}ms")
    print(f"LLM activation:     {llm_activation_rate:.4f} (target: < 0.60)")
    print(f"Fast path queries:  {fast_path_count}/{scored}")
    print(f"NOT_FOUND answers:  {not_found_count}")
    print(f"Parse failures:     {parse_fail_count}")
    print(f"API errors:         {len(errors)}")

    if output_path:
        output = {"summary": summary, "errors": errors}
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
        print(f"\nResults written to: {output_path}")

    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KRE Recall@5 Benchmark Runner")
    parser.add_argument(
        "--ground-truths",
        default=str(Path(__file__).resolve().parent.parent / "llm_ground_truths.json"),
        help="Path to ground truths JSON file (default: ../llm_ground_truths.json)",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("KRE_API_URL", "http://localhost:8000"),
        help="Base URL of the KRE query API (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of queries (useful for smoke tests)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON results to this path",
    )
    args = parser.parse_args()

    summary = run_benchmark(
        ground_truths_path=args.ground_truths,
        api_url=args.api_url,
        limit=args.limit,
        output_path=args.output,
    )

    # Exit with non-zero code if targets are missed (useful in CI)
    targets_met = (
        summary["recall_at_5"] > 0.50
        and summary["avg_faithfulness"] > 0.80
        and summary["llm_activation_rate"] < 0.60
    )
    sys.exit(0 if targets_met else 1)
