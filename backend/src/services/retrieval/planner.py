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
    # Hybrid entity extraction: capitalization-based + quoted terms + known patterns
    # R2: match consecutive capitalized tokens (TitleCase or ALLCAPS), separated by spaces or hyphens
    pattern = r"\b(?:[A-Z][a-zA-Z0-9]*)(?:(?:-|\s+)(?:[A-Z][a-zA-Z0-9]*))*\b"
    matches = re.findall(pattern, query)

    stop_words = {
        "What", "How", "Why", "Who", "When", "Where", "Which",
        "Is", "Are", "Do", "Does", "Can", "Could", "Should", "Would",
        "The", "A", "An", "In", "On", "At", "To", "For", "Of", "With", "By",
        "According", "Answer", "List", "Tell", "Give", "Find", "Show",
    }
    stop_words_lower = {w.lower() for w in stop_words}

    entities = []
    for span in matches:
        span = span.strip()
        if span in stop_words:
            continue

        parts = re.split(r"[- ]+", span)
        filtered_parts = [p for p in parts if p not in stop_words and not p.isdigit()]

        if filtered_parts:
            merged = " ".join(filtered_parts)
            if merged not in entities:
                entities.append(merged)

    # Fallback for lowercase queries or queries with quoted terms / filenames
    if not entities:
        # Extract quoted strings
        quoted = re.findall(r'["\']([^"\']+)["\']', query)
        for q_term in quoted:
            clean = q_term.strip()
            if clean and clean.lower() not in stop_words_lower and clean not in entities:
                entities.append(clean)

        # Look for filenames (e.g. survay.csv, 2412.20875v1.pdf)
        filenames = re.findall(r'\b[\w.-]+\.(?:csv|xls|xlsx|pdf|json|txt|md|doc|docx|pptx)\b', query, re.IGNORECASE)
        for fname in filenames:
            if fname not in entities:
                entities.append(fname)

        if not entities:
            # Multi-word nouns or capitalized technical tokens fallback
            words = re.findall(r'\b\w+\b', query)
            _extended_stops = stop_words_lower | {
                "what", "how", "why", "who", "when", "where", "which",
                "is", "are", "was", "were", "do", "does", "did", "has", "have", "had",
                "the", "a", "an", "in", "on", "at", "to", "for", "of", "with", "by",
                "and", "or", "but", "not", "from", "as", "if", "than", "that", "this",
                "be", "been", "being", "it", "its", "about", "between", "through",
                "many", "much", "some", "any", "all", "each", "every", "summarize",
            }
            content_words = [w for w in words if w.lower() not in _extended_stops and len(w) > 2]
            for w in content_words:
                if w not in entities and not w.isdigit():
                    entities.append(w)

    return entities


def compute_complexity(query: str) -> tuple[float, dict[str, bool | int]]:
    q_lower = query.lower()
    entities = extract_entities(query)
    entity_count = len(entities)

    multi_entity_flag = entity_count > 1
    import re

    temporal_words = {
        "q1",
        "q2",
        "q3",
        "q4",
        "quarter",
        "between",
        "during",
        "since",
        "year",
        "years",
        "month",
        "months",
        "date",
        "dates",
        "timeline",
        "chronological",
        "in order",
        "earliest",
        "latest",
        "history",
        "trend",
    }
    has_year = bool(re.search(r"\b(?:19|20)\d{2}\b", q_lower))
    temporal_flag = has_year or any(re.search(rf"\b{re.escape(w)}\b", q_lower) for w in temporal_words)
    comparison_flag = any(
        k in q_lower
        for k in [
            "vs",
            "compare",
            "difference",
            "higher",
            "lower",
            "better",
            "than",
            "calculate",
            "minus",
            "subtract",
            "plus",
            "divide",
            "sum of",
        ]
    )
    negation_flag = any(
        k in q_lower for k in ["not", "except", "without", "other than"]
    )
    relationship_flag = any(
        k in q_lower
        for k in [
            "cause",
            "affect",
            "depend",
            "lead to",
            "because",
            "impact",
            "relation between",
            "why",
            "result of",
            "due to",
        ]
    )
    synthesis_flag = any(
        k in q_lower
        for k in [
            "explain",
            "describe",
            "summarize",
            "summary",
            "outline",
            "connection",
            "influence",
            "consequence",
            "effect",
            "role of",
            "meaning",
            # Mechanism / definitional queries
            "what is",
            "what are",
            "how does",
            "how do",
            "how is",
            "how are",
            "mechanism",
            "define",
            "definition",
            "overview of",
        ]
    )
    # Aggregation: queries that need to list/enumerate multiple items from a corpus
    aggregation_flag = any(
        k in q_lower
        for k in [
            "list ",
            "enumerate",
            "all the",
            "all models",
            "all variables",
            "all countries",
            "all districts",
            "all categories",
            "all outcomes",
            "all areas",
            "focus areas",
            "mentioned in",
            "in order",
            "chronological",
        ]
    )
    # Numeric: queries specifically asking for precise numbers, counts, or values
    numeric_flag = any(
        k in q_lower
        for k in [
            "how many",
            "what was the",
            "what is the value",
            "variable code",
            "net income",
            "total",
            "ratio",
            "percentage",
            "accuracy",
            "surveyed",
            "households",
            "top-1",
            "top-3",
            "top-5",
        ]
    )
    # Format-specific: queries that reference a specific named file, table, or section
    format_flag = any(
        k in q_lower
        for k in [
            "in the csv",
            "in the excel",
            "in the spreadsheet",
            "in the xls",
            "in table",
            "in figure",
            "in note",
            "in sec-form",
            "form 10-q",
            "factsheet",
            "variable_code",
        ]
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
        "synthesis_flag": synthesis_flag,
        "aggregation_flag": aggregation_flag,
        "numeric_flag": numeric_flag,
        "format_flag": format_flag,
    }
    return min(score, 1.0), flags


class Planner:
    """Deterministic Query Planner using Semantic Centroids."""

    def route(self, query: str, query_embedding: list[float] | None = None) -> Plan:
        score, flags = compute_complexity(query)

        # Rule 2 — RELATIONSHIP PATH
        if flags["relationship_flag"]:
            return Plan(
                fast_path=False,
                use_graph=True,
                stages=[
                    "bm25",
                    "page_index",
                    "vector",
                    "okf",
                    "graph",
                    "reranker",
                    "fidelity_check",
                    "compressor",
                    "llm",
                ],
                complexity_score=score,
            )

        # Rule 3 — SYNTHESIS PATH (early-return override)
        # Mirrors Rule 2: definitional / mechanistic queries need full LLM synthesis;
        # centroid distance is unreliable for typo-heavy or short "what is X" queries.
        if flags["synthesis_flag"]:
            return Plan(
                fast_path=False,
                use_graph=False,
                stages=[
                    "bm25",
                    "page_index",
                    "vector",
                    "okf",
                    "reranker",
                    "fidelity_check",
                    "compressor",
                    "llm",
                ],
                complexity_score=score,
            )

        # Rule 3b — AGGREGATION PATH: list/enumerate queries need LLM to gather
        # multiple items from multiple chunks; extractive fast-path cannot do this.
        if flags["aggregation_flag"]:
            return Plan(
                fast_path=False,
                use_graph=False,
                stages=[
                    "bm25",
                    "page_index",
                    "vector",
                    "okf",
                    "reranker",
                    "fidelity_check",
                    "compressor",
                    "llm",
                ],
                complexity_score=score,
            )

        # Rule 3c — NUMERIC / FORMAT PATH: questions asking for specific numbers,
        # table values, or named file data require LLM extraction from context.
        if flags["numeric_flag"] or flags["format_flag"]:
            return Plan(
                fast_path=False,
                use_graph=False,
                stages=[
                    "bm25",
                    "page_index",
                    "vector",
                    "okf",
                    "reranker",
                    "fidelity_check",
                    "compressor",
                    "llm",
                ],
                complexity_score=score,
            )

        fast_path = False
        if query_embedding is not None:
            import numpy as np

            from services.retrieval.centroids import CENTROID_SIMPLE, CENTROID_SYNTHESIS

            q_emb = np.array(query_embedding)
            sim_simple = np.dot(q_emb, CENTROID_SIMPLE)
            sim_synth = np.dot(q_emb, CENTROID_SYNTHESIS)
            # If similarity to simple is greater, tentatively use fast path
            # but still override for data-extraction queries.
            centroid_suggests_fast = sim_simple > sim_synth
            # Block fast path for complex analytical queries even if centroid says simple
            hard_block = (
                flags["aggregation_flag"]
                or flags["numeric_flag"]
                or flags["format_flag"]
                or flags["temporal_flag"]
                or flags["comparison_flag"]
                or flags["synthesis_flag"]
            )
            fast_path = centroid_suggests_fast and not hard_block
        else:
            # Fallback if embedding fails
            fast_path = not (
                flags["temporal_flag"]
                or flags["comparison_flag"]
                or flags["negation_flag"]
                or flags["relationship_flag"]
                or flags["synthesis_flag"]
                or flags["aggregation_flag"]
                or flags["numeric_flag"]
                or flags["format_flag"]
            )

        if fast_path:
            return Plan(
                fast_path=True,
                use_graph=False,
                stages=["bm25", "page_index", "vector"],
                complexity_score=score,
            )

        # Rule 4 — FULL PATH (default analytical/synthesis)
        return Plan(
            fast_path=False,
            use_graph=False,
            stages=[
                "bm25",
                "page_index",
                "vector",
                "okf",
                "reranker",
                "fidelity_check",
                "compressor",
                "llm",
            ],
            complexity_score=score,
        )


planner = Planner()
