import os
import json
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from src.services.langgraph_pipeline import pipeline

def get_ngrams(text, n=3):
    text = text.lower().replace(" ", "")
    if len(text) < n:
        return set([text])
    return set([text[i:i+n] for i in range(len(text)-n+1)])

def content_match(retrieved_text, expected_text):
    if not retrieved_text or not expected_text:
        return False, 0.0
    ret_grams = get_ngrams(retrieved_text)
    exp_grams = get_ngrams(expected_text)
    if not ret_grams or not exp_grams:
        return False, 0.0
    
    intersection = len(ret_grams.intersection(exp_grams))
    union = len(ret_grams.union(exp_grams))
    jaccard = intersection / union
    
    # Substring check
    if expected_text.lower() in retrieved_text.lower():
        return True, jaccard
    if retrieved_text.lower() in expected_text.lower() and len(retrieved_text) > 100:
        return True, jaccard
            
    return jaccard >= 0.8, jaccard

def parse_page(citation) -> int | None:
    if isinstance(citation, str):
        if ":page:" in citation:
            try: return int(citation.split(":page:")[1].split(":")[0])
            except: pass
        return None
    bb = citation.get("bounding_box")
    if isinstance(bb, dict):
        pn = bb.get("page_number")
        if pn is not None: return int(pn)
    loc = str(citation.get("location_reference") or "")
    if loc.startswith("Page: "):
        try: return int(loc[6:].strip())
        except: pass
    c_id = str(citation.get("chunk_id") or "")
    if ":page:" in c_id:
        try: return int(c_id.split(":page:")[1].split(":")[0])
        except: pass
    return None

def test_n2():
    with open('d:/WORK/knowledge-retrieval-engine/backend/llm_ground_truths.json', 'r', encoding='utf-8') as f:
        ground_truths = json.load(f)
        
    print(f"{'Query':<30} | {'Old_R@5':<8} | {'New_R@5':<8} | {'Jaccard':<10}")
    print("-" * 65)
    
    flips = []
    
    for idx, gt in enumerate(ground_truths):
        query = gt["query"]
        expected_pages = set()
        for citation in gt.get("citations", []):
            p = parse_page(citation)
            if p is not None:
                expected_pages.add(p)
        expected_texts = [gt.get("retrieved_context", "")]
            
        response = pipeline.run(query)
        top_chunks = response.top_chunks
        
        # Check old criterion
        old_hit = False
        for chunk in top_chunks[:5]:
            p = parse_page(chunk.id)
            if p is not None and p in expected_pages:
                old_hit = True
                break
                
        # Check new criterion
        new_hit = False
        best_jaccard = 0.0
        for chunk in top_chunks[:5]:
            # Exact chunk id match? (Not testing here, just testing content)
            for exp_text in expected_texts:
                is_match, jaccard = content_match(chunk.text, exp_text)
                if jaccard > best_jaccard:
                    best_jaccard = jaccard
                if is_match:
                    new_hit = True
                    break
            if new_hit:
                break
                
        print(f"Q{idx:<28} | {str(old_hit):<8} | {str(new_hit):<8} | {best_jaccard:.4f}")
        if not old_hit and new_hit:
            flips.append((idx, query, best_jaccard))
            
    print("\n--- FLIPS (False Negatives caught by new matcher) ---")
    for idx, q, j in flips:
        print(f"Q{idx}: {q[:50]}... (Jaccard: {j:.4f})")

if __name__ == "__main__":
    test_n2()
