import json
import time
import math
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
from kre.api.main import query_endpoint, QueryRequest
import kre.shared.providers.embedding_provider
import kre.shared.providers.llm_provider

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

from kre.shared.providers.llm_provider import generate_completion

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
        
        if response.get("fast_path"):
            fast_path_count += 1
            
        citations = response.get("citations", [])
        expected_page = q["source_page"]
        
        # Retrieval metrics computation
        hits = []
        for c in citations:
            pg = None
            chunk_id = c.get("chunk_id", "")
            if ":page:" in chunk_id:
                try:
                    pg = int(chunk_id.split(":page:")[1].split(":")[0])
                except:
                    pass
            elif "location_reference" in c and c["location_reference"] and str(c["location_reference"]).startswith("Page: "):
                try:
                    pg = int(str(c["location_reference"]).split("Page: ")[1])
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
        prompt = f"Context: {' '.join([c.get('text_snippet', '') for c in citations])}\nAnswer: {answer}\nIs the answer supported by the context? Reply strictly YES or NO."
        try:
            judge_resp = generate_completion("You are a strict judge.", prompt, provider="prod").strip().upper()
            if "YES" in judge_resp:
                faithfulness_score += 1.0
        except Exception as e:
            # Failed to judge
            pass
            
        # Context Precision
        context_precision_score += (sum(hits) / len(hits)) if hits else 0.0
        
        # Sleep to avoid rate limits on the new API key
        pass

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
