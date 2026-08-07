"""
Manual eval runner — Step 2 & 3 of the Debug and Improve Loop.

Runs the 10 manual queries from manual_eval_set.json against the live pipeline,
printing per-query traces: planner decision, BM25 top-5, Vector top-5, final answer.

Usage:
    ENVIRONMENT=dev AWS_PROFILE=aws python src/kre/run_manual_eval.py
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# ── Planner trace ────────────────────────────────────────────────────────────
from kre.retrieval.planner import planner, compute_complexity

# ── API endpoint ─────────────────────────────────────────────────────────────
from kre.api.main import query_endpoint, QueryRequest

# ── BM25 + repo to do a dry retrieval trace ──────────────────────────────────
from kre.retrieval.bm25_retriever import BM25Retriever
from kre.db.postgres import PostgresRepository

EVAL_PATH = Path(__file__).parent.parent.parent / "tests" / "data" / "manual_eval_set.json"


def run_eval():
    with open(EVAL_PATH) as f:
        queries = json.load(f)

    repo = PostgresRepository()

    print("Loading all chunks from DynamoDB (cached for this run)...")
    t0 = time.perf_counter()
    all_chunks = repo.get_all_chunks()
    print(f"  Loaded {len(all_chunks)} chunks in {(time.perf_counter()-t0)*1000:.0f} ms\n")

    bm25 = BM25Retriever()

    total = len(queries)
    fast_path_count = 0
    recall_hits = 0

    for q_obj in queries:
        qid = q_obj["id"]
        query = q_obj["query"]
        expected_kws = q_obj.get("expected_keywords", [])
        expected_doc = q_obj.get("expected_doc", "")

        print("=" * 70)
        print(f"[{qid}] {query}")
        print("-" * 70)

        # ── 1. Planner trace ─────────────────────────────────────────────────
        score, flags = compute_complexity(query)
        plan = planner.route(query)
        print(f"  Planner  : score={score:.3f}  fast_path={plan.fast_path}")
        print(f"  Flags    : {flags}")
        if plan.fast_path:
            fast_path_count += 1

        # ── 2. BM25 trace (dry run against all chunks) ───────────────────────
        bm25_results = bm25.search(query, all_chunks, top_k=5)
        print(f"  BM25 top-5 chunks:")
        for rank, (chunk, score_bm25) in enumerate(bm25_results, 1):
            preview = chunk.text[:100].replace("\n", " ")
            kw_hit = any(kw.lower() in chunk.text.lower() for kw in expected_kws)
            marker = "✓" if kw_hit else "✗"
            print(f"    {rank}. [{marker}] score={score_bm25:.3f} doc={chunk.document_id[:8]}.. p{chunk.page_number} | {preview}")

        # ── 3. Full pipeline call ─────────────────────────────────────────────
        req = QueryRequest(query=query)
        t_start = time.perf_counter()
        try:
            resp = query_endpoint(req)
        except Exception as e:
            print(f"  Pipeline ERROR: {e}")
            resp = {}
        latency_ms = (time.perf_counter() - t_start) * 1000

        answer = resp.get("answer", "")
        citations = resp.get("citations", [])
        print(f"\n  Pipeline : latency={latency_ms:.0f}ms  fast_path={resp.get('fast_path')}  citations={len(citations)}")
        print(f"  Answer   : {answer[:300]}")

        # ── 4. Keyword recall check ───────────────────────────────────────────
        answer_lower = answer.lower()
        all_docs_text = " ".join(
            c.get("text_snippet", "") if isinstance(c, dict) else str(c)
            for c in citations
        ).lower()
        combined = answer_lower + " " + all_docs_text

        kw_found = [kw for kw in expected_kws if kw.lower() in combined]
        kw_recall = len(kw_found) / len(expected_kws) if expected_kws else 1.0
        if kw_recall >= 0.5:
            recall_hits += 1
            status = "✓ PASS"
        else:
            status = "✗ FAIL"
        print(f"  KW Recall: {kw_recall:.0%}  found={kw_found}  → {status}")
        print()

        time.sleep(1.5)  # avoid Bedrock throttle

    print("=" * 70)
    print(f"SUMMARY: {recall_hits}/{total} queries passed keyword recall >= 50%")
    print(f"Fast-path rate: {fast_path_count}/{total} = {fast_path_count/total:.0%}")
    print("=" * 70)


if __name__ == "__main__":
    run_eval()
