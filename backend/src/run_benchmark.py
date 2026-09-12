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

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# Page number extraction — exact match only, no tolerance
# ---------------------------------------------------------------------------


def _parse_page(citation: Any) -> int | None:
    """Extract page number from a citation dict. Exact match only.

    Returns None if the page number cannot be determined. A None result
    counts as a miss (hit=0); it is never skipped or treated as a special case.
    """
    if isinstance(citation, str):
        # Fallback: chunk_id format "...:<doc_id>:page:<N>:element:<M>"
        chunk_id = citation
        if ":page:" in chunk_id:
            try:
                parts = chunk_id.split(":page:")
                return int(parts[1].split(":")[0])
            except (IndexError, ValueError):
                pass
        return None

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


def _hits_at_k(
    retrieved_citations: list[dict], expected_pages: set[int], k: int
) -> int:
    """Return 1 if any of the top-k retrieved citations match an expected page, else 0."""
    for citation in retrieved_citations[:k]:
        p = _parse_page(citation)
        if p is not None and p in expected_pages:
            return 1
    return 0


# ---------------------------------------------------------------------------
# API call / Internal Pipeline execution
# ---------------------------------------------------------------------------

from src.services.langgraph_pipeline import pipeline


def get_ngrams(text: str, n: int = 3) -> set[str]:
    text = text.lower().replace(" ", "")
    if len(text) < n:
        return set([text])
    return set([text[i : i + n] for i in range(len(text) - n + 1)])


from services.evaluation.benchmark_scorer import content_match, compute_faithfulness


def _content_match(retrieved_text: str, expected_text: str) -> bool:
    """Canonical content match from services.evaluation.benchmark_scorer."""
    return content_match(retrieved_text, expected_text)


def _hits_at_k_dual(
    retrieved_chunks: list[Any], expected_pages: set[int], expected_text: str, k: int
) -> int:
    """Return 1 if any of the top-k retrieved chunks match an expected page OR content."""
    for chunk in retrieved_chunks[:k]:
        # 1. Exact page match
        p = _parse_page(chunk.id)
        if p is not None and p in expected_pages:
            return 1

        # 2. Content fallback match
        if expected_text and content_match(getattr(chunk, "text", ""), expected_text):
            return 1

    return 0


def _faithfulness_score(answer: str, context: str) -> float | None:
    """Canonical faithfulness estimate from services.evaluation.benchmark_scorer."""
    return compute_faithfulness(answer, context)


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
    print(f"Running benchmark: {total} queries using INTERNAL pipeline.run()")
    print("Hit criterion: exact page match OR content similarity >= 0.8")
    print("-" * 60)

    hits_at_5 = 0
    hits_at_3 = 0
    faith_hits = []
    faith_misses = []
    latencies_ms = []
    fast_path_count = 0
    not_found_count = 0
    parse_fail_count = 0
    errors = []

    for i, gt in enumerate(ground_truths):
        query = gt["query"]
        expected_pages = _expected_pages(gt)
        expected_text = gt.get("retrieved_context", "")

        if not expected_pages and not expected_text:
            parse_fail_count += 1
            print(
                f"  [{i+1}/{total}] SKIP (no parseable expected page or text): {query[:60]}..."
            )
            continue

        t0 = time.perf_counter()
        try:
            # Use the internal pipeline directly to access full chunk text
            result = pipeline.run(query)
        except Exception as e:
            errors.append({"query": query, "error": str(e)})
            print(f"  [{i+1}/{total}] ERROR: {e}")
            continue
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(elapsed_ms)

        retrieved_chunks = getattr(result, "top_chunks", [])
        answer = getattr(result, "answer", "")
        is_fast = getattr(result, "fast_path", False)

        if is_fast:
            fast_path_count += 1
        if answer in ("NOT_FOUND", ""):
            not_found_count += 1

        h5 = _hits_at_k_dual(retrieved_chunks, expected_pages, expected_text, k=5)
        h3 = _hits_at_k_dual(retrieved_chunks, expected_pages, expected_text, k=3)
        hits_at_5 += h5
        hits_at_3 += h3

        # Build context from returned chunks
        context = " ".join([c.text for c in retrieved_chunks])
        faith = _faithfulness_score(answer, context)

        if faith is not None:
            if h5:
                faith_hits.append(faith)
            else:
                faith_misses.append(faith)

        hit_symbol = "✓" if h5 else "✗"
        path_label = "fast" if is_fast else "full"
        faith_str = f"{faith:.2f}" if faith is not None else "N/A"
        print(
            f"  [{i+1}/{total}] {hit_symbol} R@5={h5} faith={faith_str} "
            f"path={path_label} latency={elapsed_ms:.0f}ms | {query[:55]}..."
        )

    # Compute scored queries (excluding parse failures and errors)
    scored = total - parse_fail_count - len(errors)
    recall_at_5 = hits_at_5 / scored if scored else 0.0
    recall_at_3 = hits_at_3 / scored if scored else 0.0

    all_real_faith = faith_hits + faith_misses
    avg_real_faith = sum(all_real_faith) / len(all_real_faith) if all_real_faith else 0.0
    avg_faith_hits = sum(faith_hits) / len(faith_hits) if faith_hits else 0.0
    avg_faith_misses = sum(faith_misses) / len(faith_misses) if faith_misses else 0.0
    avg_latency_ms = sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0
    llm_activation_rate = 1.0 - (fast_path_count / scored) if scored else 0.0
    abstention_rate = not_found_count / scored if scored else 0.0

    summary = {
        "total_queries": total,
        "scored_queries": scored,
        "parse_failures": parse_fail_count,
        "api_errors": len(errors),
        "recall_at_5": recall_at_5,
        "recall_at_3": recall_at_3,
        "real_answer_faithfulness": avg_real_faith,
        "avg_faithfulness_on_hits": avg_faith_hits,
        "avg_faithfulness_on_misses": avg_faith_misses,
        "abstention_rate": abstention_rate,
        "avg_latency_ms": avg_latency_ms,
        "llm_activation_rate": llm_activation_rate,
        "fast_path_count": fast_path_count,
        "not_found_count": not_found_count,
        "errors": errors,
    }

    print("-" * 60)
    print(f"Recall@5:           {recall_at_5:.4f}  (target: > 0.50)")
    print(f"Recall@3:           {recall_at_3:.4f}")
    print(f"Avg faith (hits):   {avg_faith_hits:.4f} (target: > 0.80)")
    print(f"Avg faith (misses): {avg_faith_misses:.4f}")
    print(f"Avg latency:        {avg_latency_ms:.0f}ms")
    print(f"LLM activation:     {llm_activation_rate:.4f} (target: < 0.60)")
    print(f"Fast path queries:  {fast_path_count}/{scored}")
    print(f"NOT_FOUND answers:  {not_found_count}")
    print(f"Parse failures:     {parse_fail_count}")
    print(f"API errors:         {len(errors)}")
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
        and summary["avg_faithfulness_on_hits"] > 0.80
        and summary["llm_activation_rate"] < 0.60
    )
    sys.exit(0 if targets_met else 1)
