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
    return list(dict.fromkeys(words))


def compute_complexity(query: str) -> tuple[float, dict[str, bool | int]]:
    q_lower = query.lower()
    entities = extract_entities(query)
    entity_count = len(entities)

    multi_entity_flag = entity_count > 1
    temporal_flag = any(k in q_lower for k in ["q1", "q2", "q3", "q4", "202", "201", "between", "during", "since", "year", "month"])
    comparison_flag = any(k in q_lower for k in ["vs", "compare", "difference", "higher", "lower", "better", "than"])
    negation_flag = any(k in q_lower for k in ["not", "except", "without", "other than"])
    relationship_flag = any(
        k in q_lower
        for k in ["cause", "affect", "depend", "lead to", "because", "impact", "relation between", "why did", "result of", "due to"]
    )

    score = (
        min(entity_count, 3) * 0.25
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

    def route(self, query: str) -> Plan:
        score, flags = compute_complexity(query)

        # Rule 1 — FAST PATH
        if (
            score < 0.30
            and flags["entity_count"] <= 1
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
