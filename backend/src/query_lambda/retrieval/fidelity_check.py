import logging
import time

logger = logging.getLogger(__name__)

class CoverageError(Exception):
    """Raised when context compression drops critical query entities."""
    pass

def extract_query_entities(query: str) -> list[str]:
    """Very naive entity extraction for validation.
    In a real system, this might use NLP or OKF concepts, but for tests 
    we extract capitalized words and quoted phrases.
    """
    import re
    # Extract quoted strings
    quotes = re.findall(r'"([^"]*)"', query)
    # Extract capitalized words that are not at the start (basic heuristic)
    words = query.split()
    caps = [w.strip('?,.!') for w in words[1:] if w and w[0].isupper()]
    
    entities = quotes + caps
    if not entities:
        # Fallback to nouns if possible, but here we just take words longer than 5 chars
        entities = [w.strip('?,.!') for w in words if len(w) > 5]
    
    return list(set(entities))

def check_fidelity(query: str, compressed_text: str) -> float:
    """
    Stage 7: Fidelity Check.
    Validates that query entities are present in the compressed text.
    Raises CoverageError if ratio drops below 1.0 (100%).
    """
    start_time = time.perf_counter()
    
    entities = extract_query_entities(query)
    if not entities:
        logger.info("fidelity_check.latency_ms=%.2f fidelity_check.confidence_score=1.00", (time.perf_counter() - start_time) * 1000.0)
        return 1.0
        
    found = 0
    text_lower = compressed_text.lower()
    for e in entities:
        if e.lower() in text_lower:
            found += 1
            
    coverage = float(found) / len(entities)
    latency_ms = (time.perf_counter() - start_time) * 1000.0
    logger.info("fidelity_check.latency_ms=%.2f fidelity_check.confidence_score=%.2f", latency_ms, coverage)
    
    if coverage < 1.0:
        raise CoverageError(f"Coverage ratio {coverage} is below 1.0 threshold.")
        
    return coverage
