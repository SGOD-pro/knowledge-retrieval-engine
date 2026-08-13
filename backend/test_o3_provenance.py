import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from src.services.langgraph_pipeline import pipeline

def test_provenance():
    print("Testing Pipeline Provenance Invariant...")
    query = "How many parallel attention heads h are employed in the Multi-Head Attention mechanism of the base Transformer model?"
    
    # Run the pipeline
    response = pipeline.run(query)
    
    # 1. Check if citations exist
    citations = response.citations
    print(f"Returned {len(citations)} citations.")
    
    # 2. Check provenance: Every citation must be a dictionary, have a chunk_id, and that chunk_id must be in top_chunks
    top_chunk_ids = {c.id for c in response.top_chunks}
    
    for i, c in enumerate(citations):
        assert isinstance(c, dict), f"Citation {i} is not a dictionary! Found: {type(c)}"
        assert "chunk_id" in c, f"Citation {i} is missing chunk_id! Content: {c}"
        
        cid = c["chunk_id"]
        assert cid in top_chunk_ids, f"PROVENANCE FAILURE! Citation chunk_id '{cid}' was NOT in top_chunks!"
        
    print("ALL CITATIONS PASSED PROVENANCE CHECK!")

if __name__ == "__main__":
    test_provenance()
