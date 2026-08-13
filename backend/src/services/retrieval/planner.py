import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Plan:
    fast_path: bool
    use_graph: bool
    stages: list[str] = field(default_factory=list)
    complexity_score: float = 0.0


def extract_entities(query: str) -> list[str]:
    # Simple capitalization / noun phrase entity extraction without external NER
    # R2: match consecutive capitalized tokens (TitleCase or ALLCAPS), separated by spaces or hyphens
    pattern = r"\b(?:[A-Z][a-zA-Z0-9]*)(?:(?:-|\s+)(?:[A-Z][a-zA-Z0-9]*))*\b"
    matches = re.findall(pattern, query)
    
    stop_words = {
        "What", "How", "Why", "Who", "When", "Where", "Which", "Is", "Are", 
        "Do", "Does", "Can", "Could", "Should", "Would", 
        "The", "A", "An", "In", "On", "At", "To", "For", "Of", "With", "By"
    }
    
    entities = []
    for span in matches:
        span = span.strip()
        if span in stop_words:
            continue
            
        parts = re.split(r'[- ]+', span)
        filtered_parts = [p for p in parts if p not in stop_words and not p.isdigit()]
        
        if filtered_parts:
            merged = " ".join(filtered_parts)
            if merged not in entities:
                entities.append(merged)
                
    return entities


def compute_complexity(query: str) -> tuple[float, dict[str, bool | int]]:
    q_lower = query.lower()
    entities = extract_entities(query)
    entity_count = len(entities)

    multi_entity_flag = entity_count > 1
    import re
    temporal_words = {"q1", "q2", "q3", "q4", "2020", "2021", "2022", "2023", "2024", "2025", "between", "during", "since", "year", "month"}
    temporal_flag = any(re.search(rf"\b{w}\b", q_lower) for w in temporal_words)
    comparison_flag = any(k in q_lower for k in ["vs", "compare", "difference", "higher", "lower", "better", "than"])
    negation_flag = any(k in q_lower for k in ["not", "except", "without", "other than"])
    relationship_flag = any(
        k in q_lower
        for k in ["cause", "affect", "depend", "lead to", "because", "impact", "relation between", "why", "result of", "due to"]
    )
    synthesis_flag = any(
        k in q_lower
        for k in ["explain", "describe", "summarize", "outline", "connection", "influence", "consequence", "effect", "role of", "meaning"]
    )

    # Base score using entities and flags as soft indicators (kept for logging/analytics)
    score = (
        min(entity_count, 3) * 0.25
        + (0.20 if multi_entity_flag else 0.0)
        + (0.15 if temporal_flag else 0.0)
        + (0.30 if comparison_flag else 0.0)
        + (0.25 if negation_flag else 0.0)
        + (0.30 if relationship_flag else 0.0)
        + (0.30 if synthesis_flag else 0.0)
    )

    flags = {
        "entity_count": entity_count,
        "multi_entity_flag": multi_entity_flag,
        "temporal_flag": temporal_flag,
        "comparison_flag": comparison_flag,
        "negation_flag": negation_flag,
        "relationship_flag": relationship_flag,
        "synthesis_flag": synthesis_flag
    }
    return min(score, 1.0), flags


class Planner:
    """Deterministic Query Planner.

    Rules from DECISION.md:
    Rule 1: Fast Path (complexity < 0.30, entity_count <= 1, no relationship/temporal/comparison flags).
    Rule 2: Relationship Path (relationship_flag=True -> use_graph=True).
    Rule 3: Analytical Path (temporal or comparison flag -> fast_path=False, use_graph=False).
    Rule 4: Full Path default.
    """

    def route(self, query: str) -> Plan:
        score, flags = compute_complexity(query)
        
        # Redesigned Fast Path Gate: Purely flag-based.
        fast_path = not (
            flags["temporal_flag"] or 
            flags["comparison_flag"] or 
            flags["negation_flag"] or 
            flags["relationship_flag"] or
            flags["synthesis_flag"]
        )

        use_graph = False

        if fast_path:
            return Plan(
                fast_path=True,
                use_graph=False,
                stages=["bm25", "page_index", "vector"],
                complexity_score=score
            )

        # Rule 2 — RELATIONSHIP PATH
        if flags["relationship_flag"]:
            return Plan(
                fast_path=False,
                use_graph=True,
                stages=["bm25", "page_index", "vector", "okf", "graph", "reranker", "fidelity_check", "compressor", "llm"],
                complexity_score=score,
            )

        # Rule 3 — ANALYTICAL PATH
        if flags["temporal_flag"] or flags["comparison_flag"]:
            return Plan(
                fast_path=False,
                use_graph=False,
                stages=["bm25", "page_index", "vector", "okf", "reranker", "fidelity_check", "compressor", "llm"],
                complexity_score=score,
            )

        # Rule 4 — FULL PATH (default)
        return Plan(
            fast_path=False,
            use_graph=False,
            stages=["bm25", "page_index", "vector", "okf", "reranker", "fidelity_check", "compressor", "llm"],
            complexity_score=score,
        )


planner = Planner()
