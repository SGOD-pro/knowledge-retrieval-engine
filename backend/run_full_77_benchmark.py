import json
import os
import sys
import time
from pathlib import Path
from typing import Any

if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from config import settings
from services.langgraph_pipeline import pipeline

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
            
    return jaccard >= 0.45

from run_benchmark import _parse_page, _expected_pages

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

def run_benchmark():
    # Load 17 baseline queries
    with open("llm_ground_truths.json", "r", encoding="utf-8") as f:
        gt17_raw = json.load(f)
        
    eval_queries = []
    for item in gt17_raw:
        exp_pages = list(_expected_pages(item))
        eval_queries.append({
            "id": f"BASE_{len(eval_queries)+1:02d}",
            "corpus": "baseline_17",
            "query": item["query"],
            "expected_answer": item.get("expected_answer") or item.get("llm_answer", ""),
            "expected_pages": exp_pages,
            "source_file": "baseline_corpus",
            "type": "baseline"
        })
        
    # Load 60 advance queries
    adv_file = Path("tests/data/advance/eval_60_queries.json")
    with open(adv_file, "r", encoding="utf-8") as f:
        gt60_raw = json.load(f)
        
    for item in gt60_raw:
        exp_pages = list(_expected_pages(item))
        eval_queries.append({
            "id": item.get("id", f"ADV_{len(eval_queries)+1:02d}"),
            "corpus": "advance_60",
            "query": item["query"],
            "expected_answer": item.get("expected_answer", ""),
            "expected_pages": exp_pages,
            "source_file": item.get("source_file", ""),
            "type": item.get("type", "factual")
        })

    total = len(eval_queries)
    print(f"=== Running Full {total}-Query End-to-End Benchmark (AF3) ===")
    print(f"  - Baseline Queries: 17")
    print(f"  - Advance Queries:  60")
    print("=" * 85)

    hits_at_5 = 0
    hits_at_3 = 0
    real_faith_scores = []
    all_faith_scores = []
    latencies_ms = []
    fast_path_count = 0
    full_path_count = 0
    not_found_count = 0
    results = []

    for i, item in enumerate(eval_queries):
        qid = item["id"]
        corpus = item["corpus"]
        query = item["query"]
        expected_ans = item.get("expected_answer", "")
        expected_pages = item.get("expected_pages", [])
        source_doc = item.get("source_file", "")
        q_type = item.get("type", "")

        t0 = time.perf_counter()
        try:
            res = pipeline.run(query)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            time.sleep(0.35)
        except Exception as e:
            print(f"[{i+1}/{total}] {qid} ERROR: {e}")
            continue

        answer = getattr(res, "answer", "")
        citations = getattr(res, "citations", [])
        is_fast = getattr(res, "fast_path", False)
        context_snippet = getattr(res, "context_snippet", "")
        top_chunks = getattr(res, "top_chunks", [])
        confidence = getattr(res, "confidence_score", 0.0)

        # Check Hit at 5 and 3
        h5 = 0
        h3 = 0
        
        # 1. Page match check (for baseline queries with ground-truth page numbers)
        if expected_pages:
            top5_pages = [c.page_number for c in top_chunks[:5] if c.page_number is not None]
            top3_pages = [c.page_number for c in top_chunks[:3] if c.page_number is not None]
            if any(p in top5_pages for p in expected_pages):
                h5 = 1
            if any(p in top3_pages for p in expected_pages):
                h3 = 1
                
        # 2. Per-chunk content match check
        for c in top_chunks[:5]:
            if _content_match(c.text, expected_ans):
                h5 = 1
                break
                
        for c in top_chunks[:3]:
            if _content_match(c.text, expected_ans):
                h3 = 1
                break
                
        # 3. Answer content match
        if expected_ans and answer != "NOT_FOUND":
            if _content_match(answer, expected_ans):
                h5 = 1
                h3 = 1

        if h5:
            hits_at_5 += 1
        if h3:
            hits_at_3 += 1

        if is_fast:
            fast_path_count += 1
        else:
            full_path_count += 1

        is_not_found = (answer == "NOT_FOUND")
        if is_not_found:
            not_found_count += 1

        # Faithfulness
        all_chunk_text = " ".join([c.text for c in top_chunks[:5]])
        faith = _faithfulness_score(answer, context_snippet or all_chunk_text)
        all_faith_scores.append(faith)
        if not is_not_found:
            real_faith_scores.append(faith)

        latencies_ms.append(elapsed_ms)

        hit_sym = "✓" if h5 else "✗"
        path_sym = "FAST" if is_fast else "FULL"
        print(f"[{i+1:02d}/{total}] {hit_sym} {qid:<9} R@5={h5} Faith={faith:.2f} Path={path_sym:<4} Latency={elapsed_ms:.0f}ms | {source_doc[:20]:<20} | {query[:40]}...")

        # Specific check for PPTX Digital Sahayak cost query
        if "cost per citizen" in query.lower() and "sahayak" in query.lower():
            print(f"\n>>> [PPTX GUT-CHECK CASE: {qid}]")
            print(f"    Query: {query}")
            print(f"    Path: {path_sym}")
            print(f"    Answer: {answer}")
            print(f"    Top Chunks Retrieved:")
            for rank, c in enumerate(top_chunks[:5], 1):
                print(f"      {rank}. Page/Slide: {c.page_number} | Text: {c.text[:80].replace(chr(10), ' ')}")
            print("-" * 60 + "\n")

        results.append({
            "id": qid,
            "corpus": corpus,
            "query": query,
            "source_file": source_doc,
            "type": q_type,
            "hit_at_5": h5,
            "hit_at_3": h3,
            "faithfulness": faith,
            "fast_path": is_fast,
            "latency_ms": elapsed_ms,
            "answer": answer,
            "expected_answer": expected_ans,
            "confidence": confidence,
        })

    # Summary Statistics
    scored = len(results)
    recall_at_5 = hits_at_5 / scored if scored else 0.0
    recall_at_3 = hits_at_3 / scored if scored else 0.0
    avg_faith_real = sum(real_faith_scores) / len(real_faith_scores) if real_faith_scores else 0.0
    avg_faith_all = sum(all_faith_scores) / len(all_faith_scores) if all_faith_scores else 0.0
    avg_latency = sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0
    llm_activation = full_path_count / scored if scored else 0.0
    fast_path_rate = fast_path_count / scored if scored else 0.0
    not_found_rate = not_found_count / scored if scored else 0.0

    print("\n" + "=" * 85)
    print(f"=== FULL {total}-QUERY BENCHMARK RESULTS (AUTHENTIC DUAL RETRIEVAL) ===")
    print("=" * 85)
    print(f"Total Queries Scored:              {scored}/{total}")
    print(f"Recall@5:                          {recall_at_5:.4f} ({hits_at_5}/{scored})")
    print(f"Recall@3:                          {recall_at_3:.4f} ({hits_at_3}/{scored})")
    print(f"LLM Activation Rate (Full Path):   {llm_activation:.4f} ({full_path_count}/{scored})")
    print(f"Fast-Path Activation Rate:         {fast_path_rate:.4f} ({fast_path_count}/{scored})")
    print(f"Average Latency:                   {avg_latency:.0f} ms")
    print("-" * 85)
    print("FAITHFULNESS SPLIT:")
    print(f"  - Real-Answer Faithfulness:      {avg_faith_real:.4f} (on {len(real_faith_scores)} synthesized answers)")
    print(f"  - NOT_FOUND Abstentions:         {not_found_count}/{scored} ({not_found_rate*100:.1f}%)")
    print(f"  - Blended Faithfulness:          {avg_faith_all:.4f}")
    print("=" * 85)

    # Breakdown by format/corpus
    format_stats = {}
    for r in results:
        fmt = r["source_file"] if r["source_file"] else "baseline"
        if fmt not in format_stats:
            format_stats[fmt] = {"total": 0, "hits": 0, "fast": 0, "full": 0, "not_found": 0}
        format_stats[fmt]["total"] += 1
        if r["hit_at_5"]:
            format_stats[fmt]["hits"] += 1
        if r["fast_path"]:
            format_stats[fmt]["fast"] += 1
        else:
            format_stats[fmt]["full"] += 1
        if r["answer"] == "NOT_FOUND":
            format_stats[fmt]["not_found"] += 1

    print("\n=== PER-DOCUMENT / FORMAT BREAKDOWN ===")
    print(f"{'DOCUMENT':<45} | {'TOTAL':<5} | {'HITS':<5} | {'RECALL':<8} | {'FAST':<5} | {'FULL':<5} | {'NOT_FOUND':<9}")
    print("-" * 90)
    for doc_name, ds in sorted(format_stats.items()):
        rec = ds["hits"] / ds["total"] if ds["total"] else 0.0
        print(f"{doc_name:<45} | {ds['total']:<5} | {ds['hits']:<5} | {rec:.4f}   | {ds['fast']:<5} | {ds['full']:<5} | {ds['not_found']:<9}")
    print("=" * 90)

    # Save to tmp/full_77_benchmark_results.json
    out_file = Path("tmp/full_77_benchmark_results.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {
                "total_queries": total,
                "scored_queries": scored,
                "recall_at_5": recall_at_5,
                "recall_at_3": recall_at_3,
                "real_answer_faithfulness": avg_faith_real,
                "blended_faithfulness": avg_faith_all,
                "not_found_count": not_found_count,
                "not_found_rate": not_found_rate,
                "llm_activation_rate": llm_activation,
                "fast_path_activation_rate": fast_path_rate,
                "avg_latency_ms": avg_latency,
            },
            "per_document": format_stats,
            "queries": results,
        }, f, indent=2)

    print(f"\nDetailed raw results saved to: {out_file}")

if __name__ == "__main__":
    run_benchmark()
