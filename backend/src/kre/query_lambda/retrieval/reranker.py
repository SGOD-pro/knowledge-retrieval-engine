import logging
import time
from kre.shared.models import Chunk
from kre.providers.reranker_provider import rerank_documents

logger = logging.getLogger(__name__)

def rerank(query: str, candidates: list[Chunk], top_k: int = 6, provider: str | None = None) -> list[Chunk]:
    """
    Reranks candidate chunks using the provider layer API.
    Returns the top_k chunks.
    """
    start_time = time.perf_counter()
    if not candidates:
        return []
        
    # Extract text for the API call
    documents = [c.text for c in candidates]
    
    # Call the provider
    scores = rerank_documents(query, documents, provider=provider)
    
    # Assign scores back to chunks
    for chunk, score in zip(candidates, scores):
        # We temporarily store the score on the chunk object. 
        # Since Chunk might be frozen, we use object.__setattr__
        object.__setattr__(chunk, "reranker_score", score)
        
    # Sort descending by score
    candidates.sort(key=lambda c: getattr(c, "reranker_score", 0.0), reverse=True)
    
    # Take top_k
    top_chunks = candidates[:top_k]
    
    # Log latency and confidence score for this stage
    avg_score = sum(getattr(c, "reranker_score", 0.0) for c in top_chunks) / len(top_chunks) if top_chunks else 0.0
    latency_ms = (time.perf_counter() - start_time) * 1000.0
    logger.info("reranker.latency_ms=%.2f reranker.confidence_score=%.2f", latency_ms, avg_score)
    
    return top_chunks
