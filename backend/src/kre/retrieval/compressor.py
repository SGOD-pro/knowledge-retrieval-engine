import logging
import time
from kre.models import Chunk

logger = logging.getLogger(__name__)

def compress_chunks(query: str, chunks: list[Chunk]) -> str:
    """
    Stage 8: Compression
    Extracts relevant snippets from chunks to minimize token count.
    Pure Python logic.
    """
    start_time = time.perf_counter()
    
    query_words = set(w.lower() for w in query.split() if len(w) > 3)
    compressed_text = []
    
    for c in chunks:
        # Simple extraction logic: Keep paragraphs containing query words
        paragraphs = [p for p in c.text.split("\n") if p.strip()]
        kept_paragraphs = []
        for p in paragraphs:
            p_lower = p.lower()
            if any(w in p_lower for w in query_words) or len(paragraphs) == 1:
                kept_paragraphs.append(p)
        
        if kept_paragraphs:
            compressed_text.append(f"[{c.id}] " + " ... ".join(kept_paragraphs))
            
    final_text = "\n\n".join(compressed_text)
    
    # If compression dropped everything (e.g. no query word matched exactly), 
    # fallback to just sending the raw text of the top chunks.
    if not final_text and chunks:
        final_text = "\n\n".join(f"[{c.id}] {c.text}" for c in chunks)
        
    latency_ms = (time.perf_counter() - start_time) * 1000.0
    # Compression doesn't score confidence natively, but we must log it
    logger.info("compressor.latency_ms=%.2f compressor.confidence_score=1.00", latency_ms)
    
    return final_text
