"""Unit tests for StrategyRouter."""

import inspect
import re
import pytest
from services.retrieval.strategy_router import StrategyRouter, SelectedStrategies
from services.retrieval.subgoal_decomposer import QueryPlan, QueryIntent, ExecutionOperator, Subgoal


def test_router_never_references_question_ids_or_filenames():
    """Requirement 9: Router never references question IDs, expected answers, or benchmark filenames."""
    import services.retrieval.strategy_router as router_module
    source = inspect.getsource(router_module)

    # Must not contain question IDs like Q136, Q137, etc.
    assert not re.search(r"\bQ\d{2,3}\b", source)
    # Must not contain benchmark filenames like SEC-Form-10Q.pdf or survay.csv
    assert "SEC-Form-10Q" not in source
    assert "survay.csv" not in source
    assert "expected_answer" not in source


def test_route_structured_table_query():
    """Structured table queries route to structured_table strategy."""
    router = StrategyRouter()
    # E.g. aggregation query
    res = router.route(
        query="What is the total income across all industries for year 2025?",
    )
    assert isinstance(res, SelectedStrategies)
    assert "structured_table" in res.primary


def test_route_exact_phrase_or_code_lookup():
    """Exact phrase or identifier lookup routes to bm25 + vector_rerank."""
    router = StrategyRouter()
    res = router.route(
        query='Find the section with identifier "001-36743" exactly',
    )
    assert "bm25" in res.primary
    assert "vector_rerank" in res.primary


def test_route_semantic_factual_query():
    """General semantic factual query routes to vector_rerank."""
    router = StrategyRouter()
    res = router.route(
        query="What are the key risk factors disclosed regarding international operations?",
    )
    assert "vector_rerank" in res.primary


def test_route_multi_hop_query():
    """Multi-hop entity relations route to knowledge_graph + vector_rerank."""
    router = StrategyRouter()
    res = router.route(
        query="How is entity Alpha related to entity Beta through their common partner?",
    )
    assert "knowledge_graph" in res.primary
    assert "vector_rerank" in res.primary


def test_route_page_layout_query():
    """Page layout and visual document structure questions route to page_index."""
    router = StrategyRouter()
    res = router.route(
        query="On page 4 header section, what is the title of Figure 2?",
    )
    assert "page_index" in res.primary


def test_route_okf_fact_query():
    """Canonical attribute/fact lookup routes to okf strategy."""
    router = StrategyRouter()
    res = router.route(
        query="What is the headquarters location attribute of Acme Corp?",
    )
    assert "okf" in res.primary


def test_route_false_premise_multi_strategy():
    """Potential false premise queries dispatch at least two eligible strategies for verification."""
    router = StrategyRouter()
    res = router.route(
        query="Why did the company report a 90% drop in net sales when everyone knows it collapsed?",
    )
    assert len(res.primary) >= 2
    assert "vector_rerank" in res.primary
    assert "bm25" in res.primary
