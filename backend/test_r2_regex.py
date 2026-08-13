import re

def extract_entities(query: str) -> list[str]:
    # Stop words that should NEVER be part of an entity or start an entity
    stop_words = {
        "What", "How", "Why", "Who", "When", "Where", "Which", "Is", "Are", 
        "Do", "Does", "Can", "Could", "Should", "Would", 
        "The", "A", "An", "In", "On", "At", "To", "For", "Of", "With", "By"
    }

    # Regex to match sequences of Capitalized words, optionally separated by spaces or hyphens
    # Matches: "Multi-Head Attention", "Transformer", "National Strategy", "Artificial Intelligence"
    # Doesn't match: "big model", "did the"
    matches = re.finditer(r"\b(?:[A-Z][a-z0-9]*)+(?:(?:-|\s+)[A-Z][a-z0-9]*)*\b", query)
    
    entities = []
    for match in matches:
        span = match.group(0).strip()
        # Sometimes a stop word starts a sentence (e.g. "What BLEU..."). 
        # The regex might just match "What" as a single entity, or "What BLEU" if BLEU was capitalized differently.
        # Let's filter out spans that are purely stop words or digits.
        
        # Split the span into its constituent words to filter out leading/trailing stop words if needed,
        # but if we just check if the WHOLE span is a stop word, that might be enough.
        # However, "What BLEU" would be captured if BLEU was TitleCase.
        # Actually BLEU is ALL CAPS, so the regex [A-Z][a-z0-9]* doesn't capture BLEU as a single token unless we allow all caps.
        # Let's allow ALL CAPS words too!
        pass

# Let's refine the regex:
# A word is either TitleCase or ALLCAPS.
# \b[A-Z][A-Za-z0-9]*\b
# We want to match one or more of these words, separated by space or hyphen.
def extract_entities_2(query: str) -> list[str]:
    stop_words = {
        "What", "How", "Why", "Who", "When", "Where", "Which", "Is", "Are", 
        "Do", "Does", "Can", "Could", "Should", "Would", 
        "The", "A", "An", "In", "On", "At", "To", "For", "Of", "With", "By"
    }

    # Matches one or more TitleCase/ALLCAPS words, separated by space or hyphen.
    # Note: [A-Z][a-zA-Z0-9]* handles both TitleCase (Transformer) and ALLCAPS (BLEU, WMT).
    # We use non-capturing groups for the repetition.
    pattern = r"\b(?:[A-Z][a-zA-Z0-9]*)(?:(?:-|\s+)(?:[A-Z][a-zA-Z0-9]*))*\b"
    
    matches = re.findall(pattern, query)
    
    entities = []
    for span in matches:
        span = span.strip()
        
        # If the span is just a stop word, ignore it.
        if span in stop_words:
            continue
            
        # If the span starts with a stop word, strip it (e.g. "What BLEU" -> "BLEU").
        parts = re.split(r'[- ]+', span)
        
        filtered_parts = []
        for p in parts:
            if p not in stop_words and not p.isdigit():
                filtered_parts.append(p)
                
        if filtered_parts:
            # Reconstruct the entity
            # For simplicity, we just join with spaces, though hyphens might be lost.
            # To keep it exact, we can just take the raw string but we must be careful.
            # Let's just use the filtered parts joined by space.
            merged = " ".join(filtered_parts)
            if merged not in entities:
                entities.append(merged)
                
    return entities

test_queries = [
    "What BLEU score did the Transformer big model achieve on the WMT 2014 English-to-German translation task?",
    "How many parallel attention heads h are employed in the Multi-Head Attention mechanism of the base Transformer model?",
    "Who are the primary NITI Aayog authors credited with writing the National Strategy for Artificial Intelligence report?",
    "John and Mary went to the store.", # Unrelated adjacent? Wait, "John and Mary" are not adjacent.
    "I saw John Smith and Mary Jones.", # "John Smith" (1 entity), "Mary Jones" (1 entity)
    "Compare Debt Mutual Funds versus Bank Fixed Deposits regarding liquidity, flexibility, and penalty structures."
]

for q in test_queries:
    print(f"Q: {q}")
    print(f"Entities: {extract_entities_2(q)}\n")
