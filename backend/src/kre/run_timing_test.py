"""
5-query per-stage timing diagnostic.

Runs exactly N queries (default 5) with per-stage timing logged explicitly:
  embed_ms, bm25_ms, page_index_ms, vector_ms, rerank_ms, llm_ms, total_ms

No artificial sleep. No full benchmark metrics. Purpose: confirm
per-stage latency is healthy (200-800 ms) after billing fix, before
running the full 120-query benchmark.

Usage:
    cd backend
    uv run python src/kre/run_timing_test.py
    uv run python src/kre/run_timing_test.py --n 5
"""

import json
import time
import logging
import argparse
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

import kre.providers.embedding_provider
import kre.providers.llm_provider
import kre.providers.reranker_provider
import kre.retrieval.bm25_retriever as _bm25_mod
import kre.retrieval.page_index_retriever as _pi_mod
import kre.retrieval.vector_retriever as _vec_mod

_stage_times: dict = {}

_orig_embed = kre.providers.embedding_provider.embed_text
def _timed_embed(text, **kwargs):
    t0 = time.perf_counter()
    result = _orig_embed(text, **kwargs)
    _stage_times["embed_ms"] = (time.perf_counter() - t0) * 1000
    return result
kre.providers.embedding_provider.embed_text = _timed_embed

_orig_rerank = kre.providers.reranker_provider.rerank_documents
def _timed_rerank(query, documents, **kwargs):
    t0 = time.perf_counter()
    result = _orig_rerank(query, documents, **kwargs)
    _stage_times["rerank_ms"] = (time.perf_counter() - t0) * 1000
    _stage_times["rerank_docs"] = len(documents)
    return result
kre.providers.reranker_provider.rerank_documents = _timed_rerank

_orig_llm = kre.providers.llm_provider.generate_completion
def _timed_llm(system_prompt, user_prompt, **kwargs):
    t0 = time.perf_counter()
    result = _orig_llm(system_prompt, user_prompt, **kwargs)
    _stage_times["llm_ms"] = (time.perf_counter() - t0) * 1000
    return result
kre.providers.llm_provider.generate_completion = _timed_llm

_orig_bm25_search = _bm25_mod.BM25Retriever.search
def _timed_bm25(self, query, chunks, top_k=10):
    t0 = time.perf_counter()
    result = _orig_bm25_search(self, query, chunks, top_k=top_k)
    _stage_times["bm25_ms"] = (time.perf_counter() - t0) * 1000
    _stage_times["bm25_candidates"] = len(result)
    return result
_bm25_mod.BM25Retriever.search = _timed_bm25

_orig_pi = _pi_mod.PageIndexRetriever.filter_and_rank
def _timed_pi(self, query, candidates, top_k=10):
    t0 = time.perf_counter()
    result = _orig_pi(self, query, candidates, top_k=top_k)
    _stage_times["page_index_ms"] = (time.perf_counter() - t0) * 1000
    _stage_times["page_index_candidates"] = len(result[0]) if result else 0
    return result
_pi_mod.PageIndexRetriever.filter_and_rank = _timed_pi

_orig_vec = _vec_mod.VectorRetriever.search
def _timed_vec(self, query, fast_path=False, **kwargs):
    t0 = time.perf_counter()
    result = _orig_vec(self, query, fast_path=fast_path, **kwargs)
    _stage_times["vector_ms"] = (time.perf_counter() - t0) * 1000
    _stage_times["vector_results"] = len(result)
    return result
_vec_mod.VectorRetriever.search = _timed_vec

from kre.shared.db.postgres import PostgresRepository
_orig_get_all = PostgresRepository.get_all_chunks
_chunk_cache = None
def _cached_get_all(self, document_ids=None):
    global _chunk_cache
    if _chunk_cache is None:
        _chunk_cache = _orig_get_all(self, document_ids)
    if document_ids:
        doc_set = set(str(d) for d in document_ids)
        return [c for c in _chunk_cache if str(c.document_id) in doc_set]
    return _chunk_cache
PostgresRepository.get_all_chunks = _cached_get_all


def run_timing_test(n: int = 5):
    from kre.api.main import query_endpoint, QueryRequest

    benchmark_json = Path("tests/data/benchmark_queries.json")
    with open(benchmark_json, "r", encoding="utf-8") as f:
        all_queries = json.load(f)

    queries = all_queries[:n]
    print(f"\n{'='*64}")
    print(f"PER-STAGE TIMING TEST -- {n} queries, no sleep")
    print(f"{'='*64}\n")

    rows = []

    for i, q in enumerate(queries):
        _stage_times.clear()

        req = QueryRequest(query=q["query"])
        t0 = time.perf_counter()
        try:
            response = query_endpoint(req)
            ok = True
        except Exception as e:
            print(f"  Query {i} EXCEPTION: {e}")
            ok = False
            response = {}
        total_ms = (time.perf_counter() - t0) * 1000

        row = {
            "q": i,
            "id": q.get("id", "?"),
            "fast_path": response.get("fast_path", False) if ok else None,
            "bm25_ms": _stage_times.get("bm25_ms", 0),
            "bm25_n": _stage_times.get("bm25_candidates", 0),
            "pi_ms": _stage_times.get("page_index_ms", 0),
            "pi_n": _stage_times.get("page_index_candidates", 0),
            "embed_ms": _stage_times.get("embed_ms", 0),
            "vector_ms": _stage_times.get("vector_ms", 0),
            "vector_n": _stage_times.get("vector_results", 0),
            "rerank_ms": _stage_times.get("rerank_ms", 0),
            "rerank_docs": _stage_times.get("rerank_docs", 0),
            "llm_ms": _stage_times.get("llm_ms", 0),
            "total_ms": total_ms,
        }
        rows.append(row)

        fp_tag = "[FAST]" if row["fast_path"] else "[FULL]"
        print(f"Query {i} {fp_tag} -- {q['query'][:70]}")
        print(f"  bm25:       {row['bm25_ms']:8.1f} ms  ({row['bm25_n']} candidates)")
        print(f"  page_index: {row['pi_ms']:8.1f} ms  ({row['pi_n']} candidates)")
        print(f"  embed:      {row['embed_ms']:8.1f} ms")
        print(f"  vector:     {row['vector_ms']:8.1f} ms  ({row['vector_n']} results)")
        print(f"  rerank:     {row['rerank_ms']:8.1f} ms  ({row['rerank_docs']} docs)")
        print(f"  llm:        {row['llm_ms']:8.1f} ms")
        print(f"  TOTAL:      {row['total_ms']:8.1f} ms")
        print()

    print(f"{'='*64}")
    print("SUMMARY TABLE")
    print(f"{'='*64}")
    hdr = f"{'Q':>2}  {'FP':>5}  {'bm25':>8}  {'pi':>8}  {'embed':>8}  {'vector':>8}  {'rerank':>8}  {'llm':>8}  {'total':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        fp = "Y" if r["fast_path"] else "N"
        print(
            f"{r['q']:>2}  {fp:>5}  "
            f"{r['bm25_ms']:>8.1f}  {r['pi_ms']:>8.1f}  "
            f"{r['embed_ms']:>8.1f}  {r['vector_ms']:>8.1f}  "
            f"{r['rerank_ms']:>8.1f}  {r['llm_ms']:>8.1f}  "
            f"{r['total_ms']:>9.1f}"
        )

    def avg(key):
        vals = [r[key] for r in rows if r[key] > 0]
        return sum(vals) / len(vals) if vals else 0.0

    print("-" * len(hdr))
    print(
        f"{'avg':>2}  {'':>5}  "
        f"{avg('bm25_ms'):>8.1f}  {avg('pi_ms'):>8.1f}  "
        f"{avg('embed_ms'):>8.1f}  {avg('vector_ms'):>8.1f}  "
        f"{avg('rerank_ms'):>8.1f}  {avg('llm_ms'):>8.1f}  "
        f"{avg('total_ms'):>9.1f}"
    )
    print()
    print("HEALTH CHECK:")
    print(f"  embed avg {avg('embed_ms'):.0f} ms  -- target: 200-800 ms  {'OK' if avg('embed_ms') < 2000 else 'SLOW -- billing/retry issue?'}")
    print(f"  rerank avg {avg('rerank_ms'):.0f} ms -- target: 200-800 ms  {'OK' if avg('rerank_ms') < 2000 else 'SLOW -- billing/retry issue?'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Per-stage timing test.")
    parser.add_argument("--n", type=int, default=5, help="Number of queries (default: 5).")
    args = parser.parse_args()
    run_timing_test(n=args.n)
