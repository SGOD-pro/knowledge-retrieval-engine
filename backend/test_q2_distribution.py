import os
import json
import statistics
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from src.services.langgraph_pipeline import pipeline
from src.run_benchmark import _parse_page, _expected_pages, _content_match

def test_q2():
    with open('d:/WORK/knowledge-retrieval-engine/backend/llm_ground_truths.json', 'r', encoding='utf-8') as f:
        ground_truths = json.load(f)
        
    tp_scores = []
    tn_scores = []
    
    for idx, gt in enumerate(ground_truths):
        query = gt["query"]
        expected_pages = _expected_pages(gt)
        expected_text = gt.get("retrieved_context", "")
        
        # We need the PRE-RERANKER candidate pool to get scores for everything.
        # But wait! We need to disable the reranker floor to see all scores!
        # If we use pipeline.run(), the Reranker node will drop anything < 0.5!
        # Let's bypass the graph and just run the nodes manually up to reranker, 
        # or we can just mock the threshold in the state?
        # Actually, let's just temporarily patch the threshold!
        
        from config import settings
        old_threshold = settings.RERANKER_THRESHOLD
        settings.RERANKER_THRESHOLD = -10.0  # Let everything through
        
        response = pipeline.run(query)
        
        settings.RERANKER_THRESHOLD = old_threshold # Restore
        
        # Now top_chunks has EVERYTHING that came out of the reranker
        for chunk in response.top_chunks:
            p = _parse_page(chunk.id)
            is_page_match = (p is not None and p in expected_pages)
            is_content_match = (expected_text and _content_match(chunk.text, expected_text))
            
            score = getattr(chunk, "reranker_score", 0.0)
            
            if is_page_match or is_content_match:
                tp_scores.append(score)
            else:
                tn_scores.append(score)

    print("\n--- RERANKER SCORE DISTRIBUTION ---")
    print(f"Correct Chunks (N={len(tp_scores)}):")
    if tp_scores:
        print(f"  Min:    {min(tp_scores):.4f}")
        print(f"  Median: {statistics.median(tp_scores):.4f}")
        print(f"  Max:    {max(tp_scores):.4f}")
        
    print(f"\nIncorrect Chunks (N={len(tn_scores)}):")
    if tn_scores:
        print(f"  Min:    {min(tn_scores):.4f}")
        print(f"  Median: {statistics.median(tn_scores):.4f}")
        print(f"  Max:    {max(tn_scores):.4f}")
        
    # Find what gets admitted at 0.2 but dropped at 0.5
    print("\n--- NOISE RATIO ANALYSIS ---")
    admitted_at_02 = [s for s in tn_scores if 0.2 <= s < 0.5]
    print(f"Incorrect chunks admitted if floor drops from 0.5 to 0.2: {len(admitted_at_02)}")

if __name__ == "__main__":
    test_q2()
