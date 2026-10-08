import pytest

from services.retrieval.deterministic_executor import ExecutionOperator
from services.retrieval.llm_budget import LLMCallBudget, LLMBudgetExceeded
from services.retrieval.subgoal_decomposer import (
    QueryIntent,
    decompose_query,
    detect_output_contract,
    parse_execution_operator,
)


def test_detect_output_contracts():
    assert detect_output_contract("Return the summary as JSON") == "json"
    assert detect_output_contract("Is it true that revenue increased?") == "boolean"
    assert detect_output_contract("List all colleges in Karnataka") == "list"
    assert detect_output_contract("How many districts were surveyed?") == "number"
    assert detect_output_contract("Explain the mechanism of attention") == "text"


def test_parse_operators():
    assert parse_execution_operator("Calculate percentage difference between A and B") == ExecutionOperator.PERCENTAGE_DIFFERENCE
    assert parse_execution_operator("What is the difference between 2021 and 2022?") == ExecutionOperator.DIFFERENCE
    assert parse_execution_operator("Find the ratio of debt to equity") == ExecutionOperator.RATIO
    assert parse_execution_operator("What was the average score?") == ExecutionOperator.AVERAGE
    assert parse_execution_operator("How many households were surveyed?") == ExecutionOperator.COUNT


def test_structural_target_detection():
    plan = decompose_query("In Table 4, what was the total capital expenditure?")
    assert plan.requires_structural_retrieval is True
    assert plan.subgoals[0].structural_hint == "table 4"


def test_multi_hop_query_decomposition():
    query = "Compare revenue from SEC Filing with turnover from Annual Report"
    plan = decompose_query(query)
    assert plan.primary_intent == QueryIntent.MULTI_HOP
    assert len(plan.subgoals) == 2
    assert "revenue" in plan.subgoals[0].query_text.lower()
    assert "turnover" in plan.subgoals[1].query_text.lower()
    assert plan.root_operator == ExecutionOperator.COMPARE


def test_mechanical_llm_budget_enforcer():
    budget = LLMCallBudget(max_calls=1)
    # First call must succeed
    budget.acquire()
    assert budget.calls_made == 1

    # Second call must raise LLMBudgetExceeded
    with pytest.raises(LLMBudgetExceeded) as exc:
        budget.acquire()
    assert "maximum budget is 1" in str(exc.value)

    # Reset allows reuse in tests
    budget.reset()
    assert budget.calls_made == 0
    budget.acquire()
