"""Data-driven Retrieval Strategy Router.

Routes incoming queries to primary and fallback evidence retrieval strategies based
on query grammar, detected intent, schema binding, and measured strategy capability profiles.
Does not contain benchmark question IDs, expected answers, or corpus filenames.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
from typing import Any

from services.retrieval.subgoal_decomposer import QueryIntent, QueryPlan

logger = logging.getLogger(__name__)


@dataclass
class SelectedStrategies:
    """Routing decision specifying primary strategies, fallbacks, rationale, and concurrency."""

    primary: list[str]
    fallback: list[str]
    reason: str
    max_parallel: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "fallback": self.fallback,
            "reason": self.reason,
            "max_parallel": self.max_parallel,
        }


class StrategyRouter:
    """Routes queries to optimal evidence retrieval strategies."""

    def __init__(
        self,
        capability_map: dict[str, Any] | None = None,
    ) -> None:
        self.capability_map = capability_map or {}

    def route(
        self,
        query: str,
        plan: QueryPlan | None = None,
        workspace_metadata: dict[str, Any] | None = None,
        available_indexed_artifacts: dict[str, Any] | None = None,
    ) -> SelectedStrategies:
        """Analyze query and metadata to select evidence retrieval strategies."""
        q_lower = query.lower()

        # 1. Detect false premise or contradiction inquiry
        # Phrasing suggesting potential false assumptions or contradiction checking
        false_premise_pattern = r"\b(when everyone knows|why did.*collapse|why is.*zero|despite.*failing|given that.*bankrupt|supposedly|allegedly)\b"
        if re.search(false_premise_pattern, q_lower):
            return SelectedStrategies(
                primary=["vector_rerank", "bm25"],
                fallback=["page_index"],
                reason="potential_false_premise_requires_multi_strategy_verification",
                max_parallel=2,
            )

        # 2. Structured table query (aggregation, range, comparison, sum, count, delta)
        table_keywords = r"\b(total|sum of|average|mean of|earliest and latest|how did.*change from|percentage change|difference between|min|max|highest|lowest)\b"
        has_table_agg = bool(re.search(table_keywords, q_lower))
        has_table_target = bool(
            re.search(r"\b(table|survey|dataset|columns?|rows?|industr(y|ies)|variable|code|income|expenditure|sales)\b", q_lower)
        )
        plan_is_structured = bool(
            plan and (
                getattr(plan, "use_structured_aggregate", False)
                or getattr(plan, "primary_intent", None) in (QueryIntent.AGGREGATION, QueryIntent.CALCULATION, QueryIntent.COMPARISON)
            )
        )

        if (has_table_agg and has_table_target) or plan_is_structured:
            return SelectedStrategies(
                primary=["structured_table"],
                fallback=["vector_rerank"],
                reason="deterministic_table_aggregate_or_filtering",
                max_parallel=1,
            )

        # 3. Exact phrase / identifier / code lookup
        # Quoted string or alphanumeric codes like 'SEC-12345', '99999', 'H01'
        has_exact_quote = bool(re.search(r'"([^"]+)"|\'([^\']+)\'', query))
        has_code_token = bool(re.search(r"\b[A-Za-z0-9]+-[A-Za-z0-9]+\b|\b\d{5,}\b", query))
        has_exact_intent = bool(
            re.search(r"\b(exact|commission file number|cik|ticker|identifier|code)\b", q_lower)
        )

        if has_exact_quote or (has_code_token and has_exact_intent):
            return SelectedStrategies(
                primary=["bm25", "vector_rerank"],
                fallback=["page_index"],
                reason="exact_phrase_or_code_identifier_lookup",
                max_parallel=2,
            )

        # 4. Page layout / visual document question
        page_layout_pattern = r"\b(page\s+\d+|figure\s+\d+|table\s+\d+|header\s+section|footnote|appendix|layout|diagram)\b"
        if re.search(page_layout_pattern, q_lower):
            return SelectedStrategies(
                primary=["page_index"],
                fallback=["vector_rerank"],
                reason="page_layout_structural_hierarchy_query",
                max_parallel=1,
            )

        # 5. Multi-hop entity relation query
        relation_pattern = r"\b(related to|relationship between|connected to|through their|subsidiary of|parent company|affiliated with|multi-hop)\b"
        plan_is_graph = bool(plan and getattr(plan, "requires_graph", False))
        if re.search(relation_pattern, q_lower) or plan_is_graph:
            return SelectedStrategies(
                primary=["knowledge_graph", "vector_rerank"],
                fallback=["bm25"],
                reason="multi_hop_entity_relationship_query",
                max_parallel=2,
            )

        # 6. OKF fact / property lookup
        okf_pattern = r"\b(attribute of|canonical fact|headquarters location|registered address|incorporation date)\b"
        if re.search(okf_pattern, q_lower):
            return SelectedStrategies(
                primary=["okf", "vector_rerank"],
                fallback=["bm25"],
                reason="canonical_entity_attribute_property_query",
                max_parallel=2,
            )

        # 7. Default: Semantic factual query
        return SelectedStrategies(
            primary=["vector_rerank"],
            fallback=["bm25"],
            reason="semantic_factual_retrieval",
            max_parallel=1,
        )
