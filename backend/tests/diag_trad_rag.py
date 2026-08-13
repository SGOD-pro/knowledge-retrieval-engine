import logging
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from tests.benchmark_comparison import DOCS, QUERIES
from src.db.database import CloudRepository, _IN_MEMORY_CHUNKS

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

def test_q03():
    query_str = "What are the key financial metrics mentioned in the document?"
    
    from src.db.database import CloudRepository
    import numpy as np

    from src.providers.embedding_provider import embed_text
    repo = CloudRepository()
    all_chunks = repo.get_all_chunks()    
    q_emb = embed_text(query_str)
    
    scored = []
    for chunk in all_chunks:
        if chunk.embedding_full:
            sim = float(np.dot(q_emb, chunk.embedding_full))
            scored.append((sim, chunk))
            
    scored.sort(key=lambda x: x[0], reverse=True)
    top5 = scored[:5]
    
    print("\n=== TOP 5 CHUNKS ===")
    for i, (sim, c) in enumerate(top5):
        print(f"\n[Rank {i+1} | Sim: {sim:.4f} | Doc: {c.document_id[:8]}]")
        print(c.text.encode('cp1252', errors='replace').decode('cp1252'))
        
    context = "\n\n".join(c.text for _, c in top5)
    
    print("\n=== EXACT PROMPT TO LLM ===")
    # Simulate what llm_service.call() does:
    prompt = f"Answer the query using ONLY the provided context.\n\nContext:\n{context}\n\nQuery: {query_str}\n\nIf the answer is not in the context, reply exactly with NOT_FOUND."
    print(prompt)
    
    print("\n=== EXACT LLM RESPONSE ===")
    from src.services.llm.llm_service import call as llm_call
    resp = llm_call(query_str, context)
    print(resp)

if __name__ == "__main__":
    test_q03()
