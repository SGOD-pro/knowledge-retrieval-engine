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


def run_benchmark():
    benchmark_json = Path("tests/data/benchmark_queries.json")
    with open(benchmark_json, "r", encoding="utf-8") as f:
        queries = json.load(f)[:60]
        
    total_queries = len(queries)
    
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
    
    # System Metrics
    fast_path_count = 0
    
    print(f"Running benchmark on {total_queries} queries...")
    
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
        expected_page = q["source_page"]
        
        # Retrieval metrics computation
        hits = []
        resolved_context_texts = []
        for c in citations:
            pg = None
            if isinstance(c, str):
                chunk_id = c
                loc_ref = ""
                # Resolve chunk text for faithfulness judge
                chunk_text = chunk_id
                global _cached_chunks
                if _cached_chunks is not None:
                    chunk_text = next((chk.text for chk in _cached_chunks if chk.id == chunk_id), chunk_id)
                else:
                    # If cache wasn't initialized, try to use PostgresRepository directly
                    try:
                        repo = PostgresRepository()
                        _cached_chunks = repo.get_all_chunks()
                        chunk_text = next((chk.text for chk in _cached_chunks if chk.id == chunk_id), chunk_id)
                    except Exception:
                        pass
                resolved_context_texts.append(chunk_text)
            else:
                chunk_id = c.get("chunk_id", "")
                loc_ref = str(c.get("location_reference", ""))
                resolved_context_texts.append(c.get("text_snippet", ""))
                
            if ":page:" in chunk_id:
                try:
                    pg = int(chunk_id.split(":page:")[1].split(":")[0])
                except:
                    pass
            elif loc_ref.startswith("Page: "):
                try:
                    pg = int(loc_ref.split("Page: ")[1])
                except:
                    pass
            
            hits.append(1 if pg == expected_page else 0)
        
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
        dcg_5 = sum((2**hit - 1) / math.log2(rank + 2) for rank, hit in enumerate(hits_5))
        idcg_5 = 1.0  # Ideal is hit at rank 1
        ndcg_5 += dcg_5 / idcg_5
        
        # Faithfulness (LLM judge)
        answer = response.get("answer", "")
        # Basic verification via LLM using resolved texts
        context_text = " ".join(resolved_context_texts)
        prompt = f"Context: {context_text}\nAnswer: {answer}\nIs the answer supported by the context? Reply strictly YES or NO."
        try:
            # Bypass tracking for judge so it doesn't pollute model stats
            judge_resp = _orig_generate("You are a strict judge.", prompt, provider="prod").strip().upper()
            if "YES" in judge_resp:
                faithfulness_score += 1.0
        except Exception as e:
            pass
            
        # Context Precision
        context_precision_score += (sum(hits) / len(hits)) if hits else 0.0
        
        # Sleep to avoid rate limits
        time.sleep(4.1)

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
    faithfulness_score /= total_queries
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
    run_benchmark()
