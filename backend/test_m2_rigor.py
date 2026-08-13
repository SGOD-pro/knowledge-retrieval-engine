import os
import json
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from src.config import settings
from src.services.langgraph_pipeline import pipeline
from src.db.database import CloudRepository

def get_ngrams(text, n=3):
    text = text.lower().replace(" ", "")
    if len(text) < n:
        return set([text])
    return set([text[i:i+n] for i in range(len(text)-n+1)])

def content_match(retrieved_text, expected_text, threshold=0.8):
    if not retrieved_text or not expected_text:
        return False
    ret_grams = get_ngrams(retrieved_text)
    exp_grams = get_ngrams(expected_text)
    if not ret_grams or not exp_grams:
        return False
    
    intersection = len(ret_grams.intersection(exp_grams))
    union = len(ret_grams.union(exp_grams))
    jaccard = intersection / union
    
    # Also check substring just in case it's a complete subset
    if expected_text.lower() in retrieved_text.lower():
        return True
    if retrieved_text.lower() in expected_text.lower():
        # But only if the retrieved text isn't trivial
        if len(retrieved_text) > 100:
            return True
            
    return jaccard >= threshold

def test_m2():
    with open('d:/WORK/knowledge-retrieval-engine/backend/llm_ground_truths.json', 'r', encoding='utf-8') as f:
        ground_truths = json.load(f)
        
    repo = CloudRepository()
    
    # 5 obvious misses from previous baseline
    # Indices in ground truths: 0, 1, 8, 12, 15
    test_indices = [0, 1, 8, 12, 15]
    
    for idx in test_indices:
        gt = ground_truths[idx]
        query = gt["query"]
        expected_texts = []
        for citation in gt.get("citations", []):
            expected_texts.append(citation.get("content", ""))
            
        print(f"\n=== Testing Query: {query[:50]}... ===")
        # Run pipeline to get the top chunks
        response = pipeline.run(query)
        top_chunks = response.top_chunks
        
        any_hit = False
        for i, chunk in enumerate(top_chunks):
            chunk_text = chunk.text
            for exp_text in expected_texts:
                if content_match(chunk_text, exp_text, threshold=0.8):
                    print(f"  [!] FALSE POSITIVE! Chunk {i} matched expected text!")
                    print(f"      Retrieved: {chunk_text[:100]}...")
                    any_hit = True
                    
        if not any_hit:
            print("  [OK] Confirmed TRUE NEGATIVE (STILL a miss).")

if __name__ == "__main__":
    test_m2()
