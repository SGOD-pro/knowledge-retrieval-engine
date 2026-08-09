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
    # Simple capitalization / noun phrase entity extraction without external NER (per Section 3)
    words = re.findall(r"\b[A-Z][a-z0-9]+\b|\b[0-9]+\b", query)
    stop_words = {
        "What", "Who", "How", "Why", "When", "Where", "Is", "Are", "Can", "Do", "Does", "Which", "Did", "Will", "Would", "Should", "Could",
        "The", "A", "An", "In", "On", "At", "To", "For", "With", "By", "About", "From", "As", "If", "It", "They", "We", "He", "She",
        "This", "That", "These", "Those", "There", "Here", "And", "Or", "But", "Of", "According", "Based", "Has", "Have", "Had", "Been",
        "Was", "Were", "Not", "No", "Any", "All", "Some", "Many", "Much", "More", "Most", "Less", "Least", "Few", "Fewer", "Between",
        "Among", "Through", "During", "Before", "After", "Above", "Below", "Under", "Over", "Since", "Until", "Explain", "Describe", "Tell", "List"
    }
    return list(dict.fromkeys(w for w in words if w not in stop_words))


def compute_complexity(query: str) -> tuple[float, dict[str, bool | int]]:
    q_lower = query.lower()
    entities = extract_entities(query)
    entity_count = len(entities)

    multi_entity_flag = entity_count > 3
    # Only fire on explicit quarter/year tokens, NOT generic words like 'year'/'month'
    # that appear in simple definitional queries ("What is a fiscal year?").
    temporal_flag = any(k in q_lower for k in ["q1 ", "q2 ", "q3 ", "q4 ", "fy20", "202", "201", "between ", "during ", "since "])
    comparison_flag = any(k in q_lower for k in ["vs", "compare", "difference between", "higher than", "lower than", "better than"])
    negation_flag = any(k in q_lower for k in ["not", "except", "without", "other than"])
    # Only fire relationship_flag on explicit multi-word causal phrases, not single words
    # like 'impact' which appear in simple factual queries ("What is the impact of risk?").
    relationship_flag = any(
        k in q_lower
        for k in ["lead to", "because of", "impact of", "relation between", "why did", "result of", "due to", "caused by", "depends on"]
    )

    score = (
        min(entity_count, 3) * 0.04
        + (0.20 if multi_entity_flag else 0.0)
        + (0.15 if temporal_flag else 0.0)
        + (0.15 if comparison_flag else 0.0)
        + (0.10 if negation_flag else 0.0)
        + (0.15 if relationship_flag else 0.0)
    )

    flags = {
        "entity_count": entity_count,
        "multi_entity_flag": multi_entity_flag,
        "temporal_flag": temporal_flag,
        "comparison_flag": comparison_flag,
        "negation_flag": negation_flag,
        "relationship_flag": relationship_flag,
    }
    return min(1.0, score), flags


class Planner:
    """Deterministic Query Planner.

    Rules from DECISION.md:
    Rule 1: Fast Path (complexity < 0.30, entity_count <= 1, no relationship/temporal/comparison flags).
    Rule 2: Relationship Path (relationship_flag=True -> use_graph=True).
    Rule 3: Analytical Path (temporal or comparison flag -> fast_path=False, use_graph=False).
    Rule 4: Full Path default.
    """

    # Rule 1 threshold — lowered from 0.30 → 0.15 so simple single-entity
    # factual queries reliably take the fast path and avoid LLM invocation.
    FAST_PATH_THRESHOLD = 0.15

    def route(self, query: str) -> Plan:
        score, flags = compute_complexity(query)

        # Rule 1 — FAST PATH
        if (
            score < self.FAST_PATH_THRESHOLD
            and flags["entity_count"] <= 3
            and not flags["relationship_flag"]
            and not flags["temporal_flag"]
            and not flags["comparison_flag"]
        ):
            return Plan(
                fast_path=True,
                use_graph=False,
                stages=["bm25", "page_index", "vector"],
                complexity_score=score,
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
