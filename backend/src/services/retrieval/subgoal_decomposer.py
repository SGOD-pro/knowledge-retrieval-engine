"""Deterministic Subgoal Decomposer & Query Planning Grammar.

Performs query planning and cross-document decomposition deterministically
using grammar, conjunctions, comparative syntax, and structural hints.
ZERO generative LLM calls are made during query planning or decomposition.
"""

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any

from services.retrieval.deterministic_executor import ExecutionOperator


class QueryIntent(str, Enum):
    LOOKUP = "lookup"
    FILTER = "filter"
    CALCULATION = "calculation"
    COMPARISON = "comparison"
    AGGREGATION = "aggregation"
    TEMPORAL = "temporal"
    RELATIONAL = "relational"
    EXPLANATION = "explanation"
    SUMMARIZATION = "summarization"
    MULTI_HOP = "multi_hop"


@dataclass(frozen=True)
class Subgoal:
    subgoal_id: str
    query_text: str
    target_operator: ExecutionOperator = ExecutionOperator.COUNT
    structural_hint: str | None = None
    source_hint: str | None = None
    expected_dtype: str | None = None


@dataclass(frozen=True)
class QueryPlan:
    primary_intent: QueryIntent
    subgoals: tuple[Subgoal, ...]
    root_operator: ExecutionOperator
    output_contract: str = "text"  # "text" | "json" | "number" | "boolean" | "list"
    requires_structural_retrieval: bool = False
    requires_graph: bool = False
    confidence: float = 1.0


def detect_output_contract(query: str) -> str:
    """Detect requested output contract from query phrasing."""
    q_lower = query.lower()
    if re.search(r"\b(?:as\s+json|in\s+json|format\s+json|json\s+object)\b", q_lower) or "```json" in q_lower:
        return "json"
    if re.search(r"\b(?:true\s+or\s+false|is\s+it\s+true|does\s+.*\s+exist\?|yes\s+or\s+no)\b", q_lower):
        return "boolean"
    if re.search(r"\b(?:list\s+all|enumerate|give\s+a\s+list|bulleted\s+list)\b", q_lower):
        return "list"
    if re.search(r"\b(?:how\s+many|total\s+number\s+of|calculate\s+the\s+difference|percentage\s+difference|ratio\s+of)\b", q_lower):
        return "number"
    return "text"


def parse_execution_operator(query: str) -> ExecutionOperator:
    """Parse explicit deterministic mathematical operators from query grammar."""
    q_lower = query.lower()
    if re.search(r"\b(?:percentage\s+difference|percentage\s+change|percent\s+diff)\b", q_lower):
        return ExecutionOperator.PERCENTAGE_DIFFERENCE
    if re.search(r"\b(?:percentage\s+points?|percentage\s+point\s+difference)\b", q_lower):
        return ExecutionOperator.PERCENTAGE_POINT_DIFFERENCE
    if re.search(r"\b(?:difference\s+between|diff\s+between|subtract|minus)\b", q_lower):
        return ExecutionOperator.DIFFERENCE
    if re.search(r"\b(?:ratio\s+of|ratio\s+between|percentage\s+of)\b", q_lower):
        return ExecutionOperator.RATIO
    if re.search(r"\b(?:average|mean\s+of)\b", q_lower):
        return ExecutionOperator.AVERAGE
    if re.search(r"\b(?:sum\s+of|total\s+of)\b", q_lower):
        return ExecutionOperator.SUM
    if re.search(r"\b(?:compare|higher\s+than|lower\s+than|better\s+than|vs\.?|versus)\b", q_lower):
        return ExecutionOperator.COMPARE
    if re.search(r"\b(?:how\s+many|count\s+of|number\s+of)\b", q_lower):
        return ExecutionOperator.COUNT
    if re.search(r"\b(?:days\s+between|how\s+many\s+days|date\s+difference)\b", q_lower):
        return ExecutionOperator.DATE_DIFFERENCE
    if re.search(r"\b(?:maximum|highest|largest|max)\b", q_lower):
        return ExecutionOperator.MAX
    if re.search(r"\b(?:minimum|lowest|smallest|min)\b", q_lower):
        return ExecutionOperator.MIN
    return ExecutionOperator.COUNT


def decompose_query(query: str) -> QueryPlan:
    """Decompose user query into execution subgoals and plan deterministically."""
    q_lower = query.lower()
    contract = detect_output_contract(query)
    operator = parse_execution_operator(query)

    # Check structural targets (e.g. "Table 3", "Figure 2", "Sheet2")
    struct_match = re.search(r"\b(table\s+\d+|figure\s+\d+|sheet\s*[A-Za-z0-9_]+|slide\s+\d+)\b", q_lower)
    struct_hint = struct_match.group(1) if struct_match else None
    requires_structural = bool(struct_hint)

    # Check relational / graph targets
    requires_graph = any(
        w in q_lower
        for w in ["cause", "affect", "lead to", "relationship between", "connection between", "influence"]
    )

    # Multi-hop comparative patterns:
    # Pattern: "Compare [X in/from A] with/and [Y in/from B]"
    # Pattern: "Contrast [X] with/to [Y]"
    # Pattern: "Difference between [X in A] and [Y in B]"
    compare_m = re.search(
        r"(?:compare|difference\s+between|contrast)\s+(.+?)\s+(?:with|and|versus|to)\s+(.+)",
        query,
        re.IGNORECASE,
    )

    if compare_m:
        part1 = compare_m.group(1).strip()
        part2 = compare_m.group(2).strip()

        # Extract source hints if present (e.g. "from SEC filing", "in doc A")
        s1_match = re.search(r"(?:in|from)\s+([A-Za-z0-9_\-\. ]+?)(?:\s+and|\s+with|\s*$)", part1, re.IGNORECASE)
        s2_match = re.search(r"(?:in|from)\s+([A-Za-z0-9_\-\. ]+?)(?:\s*$)", part2, re.IGNORECASE)

        subgoal1 = Subgoal(
            subgoal_id="sub_1",
            query_text=part1,
            target_operator=operator,
            source_hint=s1_match.group(1).strip() if s1_match else None,
        )
        subgoal2 = Subgoal(
            subgoal_id="sub_2",
            query_text=part2,
            target_operator=operator,
            source_hint=s2_match.group(1).strip() if s2_match else None,
        )
        return QueryPlan(
            primary_intent=QueryIntent.MULTI_HOP,
            subgoals=(subgoal1, subgoal2),
            root_operator=operator,
            output_contract=contract,
            requires_structural_retrieval=requires_structural,
            requires_graph=requires_graph,
            confidence=0.95,
        )

    # Multi-sentence queries
    sentences = [s.strip() for s in re.split(r"(?<=[.?!])\s+", query) if len(s.strip()) > 15]
    if len(sentences) >= 2:
        subgoals = tuple(
            Subgoal(
                subgoal_id=f"sub_{i+1}",
                query_text=s,
                target_operator=operator,
            )
            for i, s in enumerate(sentences)
        )
        return QueryPlan(
            primary_intent=QueryIntent.MULTI_HOP,
            subgoals=subgoals,
            root_operator=operator,
            output_contract=contract,
            requires_structural_retrieval=requires_structural,
            requires_graph=requires_graph,
            confidence=0.95,
        )

    # Multi-clause queries with conjunctions (e.g. "... and how does that relate to ...")
    clause_m = re.search(r"^(.+?),\s*(?:and\s+how|and\s+what|and\s+why|and\s+whether)\s+(.+)", query, re.IGNORECASE)
    if clause_m:
        subgoal1 = Subgoal(subgoal_id="sub_1", query_text=clause_m.group(1).strip(), target_operator=operator)
        subgoal2 = Subgoal(subgoal_id="sub_2", query_text=clause_m.group(2).strip(), target_operator=operator)
        return QueryPlan(
            primary_intent=QueryIntent.MULTI_HOP,
            subgoals=(subgoal1, subgoal2),
            root_operator=operator,
            output_contract=contract,
            requires_structural_retrieval=requires_structural,
            requires_graph=requires_graph,
            confidence=0.95,
        )

    # Single-goal query
    primary_intent = QueryIntent.LOOKUP
    if operator in (ExecutionOperator.DIFFERENCE, ExecutionOperator.PERCENTAGE_DIFFERENCE, ExecutionOperator.RATIO, ExecutionOperator.SUM):
        primary_intent = QueryIntent.CALCULATION
    elif operator == ExecutionOperator.COMPARE:
        primary_intent = QueryIntent.COMPARISON
    elif requires_graph:
        primary_intent = QueryIntent.RELATIONAL

    single_subgoal = Subgoal(
        subgoal_id="sub_root",
        query_text=query,
        target_operator=operator,
        structural_hint=struct_hint,
    )
    return QueryPlan(
        primary_intent=primary_intent,
        subgoals=(single_subgoal,),
        root_operator=operator,
        output_contract=contract,
        requires_structural_retrieval=requires_structural,
        requires_graph=requires_graph,
        confidence=1.0,
    )
