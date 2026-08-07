import json
import time
import math
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
from kre.api.main import query_endpoint, QueryRequest
import kre.providers.embedding_provider
import kre.providers.llm_provider
import kre.providers.reranker_provider

from kre.shared.db.postgres import PostgresRepository
_original_get_all_chunks = PostgresRepository.get_all_chunks
_cached_chunks = None
def _mock_get_all_chunks(self, document_ids=None):
    global _cached_chunks
    if _cached_chunks is None:
        _cached_chunks = _original_get_all_chunks(self, document_ids)
    if document_ids:
        doc_set = set(str(d) for d in document_ids)
        return [c for c in _cached_chunks if str(c.document_id) in doc_set]
    return _cached_chunks
PostgresRepository.get_all_chunks = _mock_get_all_chunks

# Setup Stats Tracking
stats = {
    "llm": {"calls": 0, "time_ms": 0.0, "prompt_chars": 0, "response_chars": 0},
    "embed": {"calls": 0, "time_ms": 0.0, "chars": 0},
    "rerank": {"calls": 0, "time_ms": 0.0, "docs": 0}
}

_orig_generate = kre.providers.llm_provider.generate_completion
def _tracked_generate(system_prompt, user_prompt, **kwargs):
    t0 = time.perf_counter()
    res = _orig_generate(system_prompt, user_prompt, **kwargs)
    t1 = time.perf_counter()
    stats["llm"]["calls"] += 1
    stats["llm"]["time_ms"] += (t1 - t0) * 1000
    stats["llm"]["prompt_chars"] += len(system_prompt) + len(user_prompt)
    stats["llm"]["response_chars"] += len(res)
    return res
kre.providers.llm_provider.generate_completion = _tracked_generate

_orig_embed = kre.providers.embedding_provider.embed_text
def _tracked_embed(text, **kwargs):
    t0 = time.perf_counter()
    res = _orig_embed(text, **kwargs)
    t1 = time.perf_counter()
    stats["embed"]["calls"] += 1
    stats["embed"]["time_ms"] += (t1 - t0) * 1000
    stats["embed"]["chars"] += len(text)
    return res
kre.providers.embedding_provider.embed_text = _tracked_embed

_orig_rerank = kre.providers.reranker_provider.rerank_documents
def _tracked_rerank(query, documents, **kwargs):
    t0 = time.perf_counter()
    res = _orig_rerank(query, documents, **kwargs)
    t1 = time.perf_counter()
    stats["rerank"]["calls"] += 1
    stats["rerank"]["time_ms"] += (t1 - t0) * 1000
    stats["rerank"]["docs"] += len(documents)
    return res
kre.providers.reranker_provider.rerank_documents = _tracked_rerank


def run_benchmark(limit: int | None = None):
    global _cached_chunks
    benchmark_json = Path("tests/data/benchmark_queries.json")
    with open(benchmark_json, "r", encoding="utf-8") as f:
        all_queries = json.load(f)
        
    full_count = len(all_queries)
    queries = all_queries[:limit] if limit is not None else all_queries
    total_queries = len(queries)

    if total_queries < full_count:
        print(f"WARNING: Running benchmark on {total_queries} of {full_count} total queries (partial run — limit={limit}).")
    else:
        print(f"Running benchmark on {total_queries} of {full_count} total queries (full dataset).")
    
    # Metrics
    latencies = []
    mrr_5 = 0.0
    ndcg_5 = 0.0
    recall_3 = 0.0
    recall_5 = 0.0
    precision_3 = 0.0
    
    # LLM-as-a-judge Metrics
    faithfulness_score = 0.0
    context_precision_score = 0.0
    faithfulness_attempts = 0.0
    llm_calls_made = 0
    llm_ground_truths = []
    
    # System Metrics
    fast_path_count = 0
    
    # (count already printed above)
    
    for i, q in enumerate(queries):
        if i % 10 == 0:
            print(f"Processed {i}/{total_queries} queries...")
            
        req = QueryRequest(query=q["query"])
        
        start_time = time.perf_counter()
        try:
            response = query_endpoint(req)
        except Exception as e:
            print(f"Query failed: {e}")
            response = {}
            
        latency_ms = (time.perf_counter() - start_time) * 1000
        latencies.append(latency_ms)
        print(f"Query {i} latency: {latency_ms:.2f} ms")
        
        if response.get("fast_path"):
            fast_path_count += 1
            
        citations = response.get("citations", [])
        retrieved_chunks = response.get("retrieved_chunks", [])
        expected_page = q["source_page"]
        expected_answer = q.get("expected_answer", "").lower()
        import re
        expected_words = set(re.findall(r"\b[a-z0-9]+\b", expected_answer))
        stop_words = {"the", "a", "an", "is", "are", "was", "were", "and", "or", "in", "on", "at", "to", "for", "with", "by", "of", "it", "that", "this", "as"}
        expected_keywords = expected_words - stop_words
        
        # Retrieval metrics computation
        hits = []
        for c in retrieved_chunks:
            pg = None
            chunk_id_str = ""
            if isinstance(c, str):
                chunk_id_str = c
            else:
                chunk_id_str = c.get("chunk_id", "")
                
            if ":page:" in chunk_id_str:
                try:
                    pg = int(chunk_id_str.split(":page:")[1].split(":")[0])
                except:
                    pass
            
            # Resolve chunk text and document_id
            chunk_text = ""
            chunk_doc_id = ""
            if _cached_chunks is not None:
                chk = next((chk for chk in _cached_chunks if chk.id == chunk_id_str), None)
                if chk:
                    chunk_text = chk.text
                    chunk_doc_id = str(chk.document_id)
            else:
                try:
                    repo = PostgresRepository()
                    _cached_chunks = repo.get_all_chunks()
                    chk = next((chk for chk in _cached_chunks if chk.id == chunk_id_str), None)
                    if chk:
                        chunk_text = chk.text
                        chunk_doc_id = str(chk.document_id)
                except Exception:
                    pass
                    
            # We also need the expected document ID for verification
            expected_doc_id = ""
            expected_filename = q.get("document_filename", "")
            if expected_filename:
                # Find the document_id that has this filename
                from kre.shared.db.postgres import _IN_MEMORY_DOCS
                expected_doc = next((doc for doc in _IN_MEMORY_DOCS.values() if doc.filename == expected_filename), None)
                if not expected_doc and _cached_chunks:
                    # Infer from chunks if IN_MEMORY_DOCS is not populated
                    for chk in _cached_chunks:
                        if expected_filename in chk.source_format:  # The source format often holds the filename
                            expected_doc_id = str(chk.document_id)
                            break
                elif expected_doc:
                    expected_doc_id = str(expected_doc.id)
            
            chunk_words = set(re.findall(r"\b[a-z0-9]+\b", chunk_text.lower()))
            overlap = len(expected_keywords.intersection(chunk_words))
            overlap_ratio = overlap / max(1, len(expected_keywords))
            
            # Count as hit if the retrieved chunk is within +/- 3 pages of the expected page AND from the same document
            # OR if it has at least 20% semantic keyword overlap
            doc_matches = (expected_doc_id == chunk_doc_id) if expected_doc_id and chunk_doc_id else True
            is_hit = 1 if (doc_matches and pg is not None and abs(pg - expected_page) <= 3) or (overlap_ratio > 0.20) else 0
            hits.append(is_hit)
            
        resolved_context_texts = []
        for c in citations:
            chunk_id = ""
            if isinstance(c, str):
                chunk_id = c
            else:
                chunk_id = c.get("chunk_id", "")
                
            if not chunk_id:
                continue
                
            # Resolve chunk text for faithfulness judge
            chunk_text = chunk_id
            if _cached_chunks is not None:
                chunk_text = next((chk.text for chk in _cached_chunks if chk.id == chunk_id), chunk_id)
            else:
                try:
                    repo = PostgresRepository()
                    _cached_chunks = repo.get_all_chunks()
                    chunk_text = next((chk.text for chk in _cached_chunks if chk.id == chunk_id), chunk_id)
                except Exception:
                    pass
            resolved_context_texts.append(chunk_text)
        
        # Recall@3 & Precision@3
        hits_3 = hits[:3]
        if sum(hits_3) > 0:
            recall_3 += 1.0
        precision_3 += sum(hits_3) / 3.0 if len(hits_3) > 0 else 0.0
        
        # MRR@5 and Recall@5
        hits_5 = hits[:5]
        if sum(hits_5) > 0:
            recall_5 += 1.0
            
        for rank, hit in enumerate(hits_5):
            if hit == 1:
                mrr_5 += 1.0 / (rank + 1)
                break
                
        # nDCG@5
        if len(hits) > 0:
            import numpy as np
            from sklearn.metrics import ndcg_score
            y_true = np.asarray([hits])
            # Perfect model score is just placing hits at the top
            y_score = np.asarray([[1.0/(r+1) for r in range(len(hits))]])
            ndcg_5 += ndcg_score(y_true, y_score, k=5)
        
        # Faithfulness (LLM judge)
        answer = response.get("answer", "")
        if not response.get("fast_path"):
            llm_calls_made += 1
            
            context_text = " ".join(resolved_context_texts)
            
            llm_ground_truths.append({
                "query": q["query"],
                "retrieved_context": context_text,
                "llm_answer": answer,
                "citations": response.get("citations", [])
            })
            
            if answer.strip() == "NOT_FOUND":
                # Do NOT include in Faithfulness calculation (it's a retrieval failure)
                print(f"\n[PROVING GROUNDING - LLM CALL {i}] (NOT_FOUND - Skipped Faithfulness)")
            else:
                faithfulness_attempts += 1.0
                prompt = f"Context: {context_text}\nAnswer: {answer}\nIs the answer supported by the context? Reply strictly YES or NO."
                try:
                    # Bypass tracking for judge so it doesn't pollute model stats
                    judge_resp = _orig_generate("You are a strict judge.", prompt, provider="prod").strip().upper()
                    if "YES" in judge_resp:
                        faithfulness_score += 1.0
                except Exception as e:
                    pass
                    
                print(f"\n[PROVING GROUNDING - LLM CALL {i}]")
                print(f"Q: {q['query']}")
                print(f"A: {answer}")
                print(f"Context provided to LLM: {context_text[:200]}...\n")
            
        # Context Precision
        context_precision_score += (sum(hits) / len(hits)) if hits else 0.0
        
        # Sleep to avoid rate limits
        time.sleep(2.0)

    # Save LLM ground truths
    with open("llm_ground_truths.json", "w", encoding="utf-8") as f:
        json.dump(llm_ground_truths, f, indent=2, ensure_ascii=False)

    # Calculate final averages
    if latencies:
        avg_latency = sum(latencies) / len(latencies)
        sorted_latencies = sorted(latencies)
        p95_latency = sorted_latencies[int(0.95 * len(latencies))]
    else:
        avg_latency = p95_latency = 0.0
        
    mrr_5 /= total_queries
    ndcg_5 /= total_queries
    recall_3 /= total_queries
    recall_5 /= total_queries
    precision_3 /= total_queries
    faithfulness_score = (faithfulness_score / faithfulness_attempts) if faithfulness_attempts > 0 else 0.0
    context_precision_score /= total_queries
    llm_activation_rate = 1.0 - (fast_path_count / total_queries)
    
    print("\n=======================================================")
    print("RETRIEVAL METRICS (real embeddings, mocked LLM/reranker) — final")
    print("=======================================================")
    print(f"Total Queries: {total_queries}")
    print(f"Recall@3: {recall_3:.4f}")
    print(f"Recall@5: {recall_5:.4f}")
    print(f"MRR@5: {mrr_5:.4f}")
    print(f"nDCG@5: {ndcg_5:.4f}")
    print(f"Precision@3: {precision_3:.4f}")
    
    print("\n=======================================================")
    print("LLM-DEPENDENT METRICS")
    print("=======================================================")
    print(f"Faithfulness: {faithfulness_score:.4f}")
    print(f"Context Precision: {context_precision_score:.4f}")
    print(f"Average Latency: {avg_latency:.2f} ms")
    print(f"P95 Latency: {p95_latency:.2f} ms")
    print(f"LLM Activation Rate: {llm_activation_rate:.4f} (Fast path count: {fast_path_count})")
    
    print("\n=======================================================")
    print("MODEL USAGE STATS")
    print("=======================================================")
    for model, s in stats.items():
        print(f"--- {model.upper()} ---")
        print(f"  Calls: {s['calls']}")
        print(f"  Total Time: {s['time_ms']:.2f} ms")
        if model == "llm":
            print(f"  Prompt Chars: {s['prompt_chars']}, Response Chars: {s['response_chars']}")
        elif model == "embed":
            print(f"  Chars Embed: {s['chars']}")
        elif model == "rerank":
            print(f"  Docs Reranked: {s['docs']}")

    # Save results
    results = {
        "avg_latency_ms": avg_latency,
        "p95_latency_ms": p95_latency,
        "mrr_5": mrr_5,
        "ndcg_5": ndcg_5,
        "recall_3": recall_3,
        "recall_5": recall_5,
        "precision_3": precision_3,
        "faithfulness": faithfulness_score,
        "context_precision": context_precision_score,
        "llm_activation_rate": llm_activation_rate,
        "stats": stats
    }
    with open("benchmark_results.json", "w") as f:
        json.dump(results, f, indent=2)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run the KRE benchmark.")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N queries (default: all).")
    args = parser.parse_args()
    run_benchmark(limit=args.limit)
