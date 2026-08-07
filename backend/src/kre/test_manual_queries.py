import json
import random
from kre.db.postgres import PostgresRepository
from kre.graph.langgraph_pipeline import Pipeline

def run_manual_test():
    repo = PostgresRepository()
    all_chunks = repo.get_all_chunks()
    
    # Filter for chunks from the PDF that have a reasonable length
    pdf_chunks = [c for c in all_chunks if 30 < len(c.text.split()) < 150]
    
    if not pdf_chunks:
        print("No suitable PDF chunks found.")
        return
        
    random.seed(42) # Deterministic
    sample_chunks = random.sample(pdf_chunks, min(10, len(pdf_chunks)))
    
    queries = []
    for c in sample_chunks:
        # Extract a specific factual sentence from the chunk to act as a query
        sentences = [s.strip() for s in c.text.split('.') if len(s.split()) > 10]
        if sentences:
            q_text = sentences[len(sentences)//2] + "?" # Take a middle sentence
            queries.append((q_text, c))
            
    print(f"Generated {len(queries)} queries.")
    
    pipeline = Pipeline()
    
    for i, (q, expected_chunk) in enumerate(queries):
        print(f"\n=== Q{i+1}: {q[:100]}... ===")
        print(f"Expected Chunk ID: {expected_chunk.id}, Page: {expected_chunk.page_number}")
        
        # Test BM25
        from kre.retrieval.bm25_retriever import BM25Okapi, _tokenize
        tokenized_corpus = [_tokenize(c.text) for c in all_chunks]
        bm25 = BM25Okapi(tokenized_corpus)
        scores = bm25.get_scores(_tokenize(q))
        scored = list(zip(all_chunks, scores))
        scored.sort(key=lambda x: x[1], reverse=True)
        bm25_top = scored[:5]
        
        bm25_found = any(c.id == expected_chunk.id for c, _ in bm25_top)
        print("BM25 Top 5:")
        for rank, (c, score) in enumerate(bm25_top):
            print(f"  {rank+1}. ID: {c.id} (Page {c.page_number}) - Score: {score:.2f}")
        print(f"BM25 Hit: {bm25_found}")
        
        # Test Vector (Full Path)
        from kre.providers.embedding_provider import embed_text
        q_emb = embed_text(q, provider="prod")
        vec_results = repo.search_vector(q_emb, embedding_column="embedding_full", limit=5)
        
        vec_found = any(c.id == expected_chunk.id for c, _ in vec_results)
        print("Vector Top 5:")
        for rank, (c, score) in enumerate(vec_results):
            print(f"  {rank+1}. ID: {c.id} (Page {c.page_number}) - Score: {score:.4f}")
        print(f"Vector Hit: {vec_found}")

if __name__ == "__main__":
    run_manual_test()
