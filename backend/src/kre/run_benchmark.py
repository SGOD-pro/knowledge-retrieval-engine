import json
import time
import math
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
from kre.api.main import query_endpoint, QueryRequest
from kre.providers.llm_provider import generate_completion

def run_benchmark():
    benchmark_json = Path("tests/data/benchmark_queries.json")
    with open(benchmark_json, "r", encoding="utf-8") as f:
        queries = json.load(f)
        
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
        
        if response.get("fast_path"):
            fast_path_count += 1
            
        citations = response.get("citations", [])
        expected_page = q["source_page"]
        
        # Retrieval metrics computation
        hits = [1 if c.get("page_number") == expected_page else 0 for c in citations]
        
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
        prompt = f"Context: {' '.join([c.get('text_snippet', '') for c in citations])}\nAnswer: {answer}\nIs the answer supported by the context? Reply strictly YES or NO."
        try:
            judge_resp = generate_completion("You are a strict judge.", prompt, provider="dev").strip().upper()
            if "YES" in judge_resp:
                faithfulness_score += 1.0
        except Exception as e:
            # Failed to judge
            pass
            
        # Context Precision
        context_precision_score += (sum(hits) / len(hits)) if hits else 0.0
        
        # Sleep to avoid rate limits on the new API key
        time.sleep(2.0)

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
        "llm_activation_rate": llm_activation_rate
    }
    with open("benchmark_results.json", "w") as f:
        json.dump(results, f, indent=2)

if __name__ == "__main__":
    run_benchmark()
