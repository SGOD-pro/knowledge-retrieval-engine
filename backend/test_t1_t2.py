import json

def test_relationship_flag(query: str, add_why: bool = True) -> bool:
    q_lower = query.lower()
    
    # Original list + "why" (if add_why is True)
    keywords = ["cause", "affect", "depend", "lead to", "because", "impact", "relation between", "why did", "result of", "due to"]
    if add_why:
        keywords.append("why")
        # Ensure we don't duplicate if "why did" is there, but "why" covers "why did" anyway.
        
    # Check if any keyword is in q_lower as a whole word or substring? 
    # Current implementation in planner.py: any(k in q_lower for k in [...])
    # Which means substring match. If we add "why", it matches "why ".
    import re
    # We should match whole words for "why" to avoid matching inside other words, 
    # but since "why" is separated by spaces, 'k in q_lower' matches it.
    # To be safe and mimic the current implementation:
    return any(k in q_lower for k in keywords)

print("=== T1: Stress-test 'why' flag ===")
t1_queries = [
    # Simple factual lookups phrased with "why" (Definitional/Naming)
    "Why is the sky blue called Rayleigh scattering?", # This is a bit awkward. Better: "Why is the framework named React?"
    "Why is the company called Apple?",
    
    # Genuine synthesis/causal questions phrased with "why"
    "Why does increasing the learning rate cause the loss to diverge?",
    "Why did the housing market crash in 2008?"
]

for q in t1_queries:
    flag = test_relationship_flag(q)
    print(f"Q: {q}")
    print(f"Triggered 'why' flag: {flag}\n")


print("=== T2: Probe for synthesis queries with no flag keyword hit ===")

def test_all_flags(query: str) -> dict:
    q_lower = query.lower()
    
    import re
    temporal_words = {"q1", "q2", "q3", "q4", "2020", "2021", "2022", "2023", "2024", "2025", "between", "during", "since", "year", "month"}
    temporal_flag = any(re.search(rf"\b{w}\b", q_lower) for w in temporal_words)
    
    comparison_flag = any(k in q_lower for k in ["vs", "compare", "difference", "higher", "lower", "better", "than"])
    negation_flag = any(k in q_lower for k in ["not", "except", "without", "other than"])
    
    # Using the S3 proposed relationship_flag (with "why")
    relationship_flag = any(
        k in q_lower
        for k in ["cause", "affect", "depend", "lead to", "because", "impact", "relation between", "why", "result of", "due to"]
    )
    
    return {
        "temporal": temporal_flag,
        "comparison": comparison_flag,
        "negation": negation_flag,
        "relationship": relationship_flag,
        "fast_path": not (temporal_flag or comparison_flag or negation_flag or relationship_flag)
    }


t2_queries = [
    "Explain the connection between inflation and unemployment.",
    "Summarize how quantitative easing influences bond yields.",
    "Describe what happens when the central bank lowers interest rates.",
    "Outline the consequences of a sovereign debt default."
]

for q in t2_queries:
    flags = test_all_flags(q)
    print(f"Q: {q}")
    print(f"Flags: {flags}")
    if flags["fast_path"]:
        print("  -> INCORRECTLY FAST-PATHED (No reasoning flags triggered!)")
    print()

