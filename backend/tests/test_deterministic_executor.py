from decimal import Decimal

import pytest

from services.retrieval.deterministic_executor import (
    DeterministicExecutor,
    ExecutionError,
    ExecutionOperator,
    Operand,
    parse_date_value,
    parse_decimal_value,
)


def test_parse_decimal_values():
    assert parse_decimal_value("149818") == Decimal("149818")
    assert parse_decimal_value("$1,234,567.89") == Decimal("1234567.89")
    assert parse_decimal_value("50M") == Decimal("50000000")
    assert parse_decimal_value("1.5B") == Decimal("1500000000")
    assert parse_decimal_value("10k") == Decimal("10000")
    assert parse_decimal_value("75.5%") == Decimal("75.5")


def test_exact_difference_calculation():
    executor = DeterministicExecutor()
    op1 = Operand(
        raw_value="149818",
        normalized_value=Decimal("149818"),
        source_citation={"document_id": "doc1", "row": 5},
        binding_confidence=0.92,
    )
    op2 = Operand(
        raw_value="149326",
        normalized_value=Decimal("149326"),
        source_citation={"document_id": "doc1", "row": 6},
        binding_confidence=0.88,
    )

    res = executor.execute(ExecutionOperator.DIFFERENCE, [op1, op2])
    assert res.result_value == Decimal("492")
    assert res.execution_correctness == 1.0
    # Input binding confidence is min(0.92, 0.88) = 0.88
    assert res.input_binding_confidence == 0.88
    assert res.overall_confidence == 0.88
    assert len(res.provenance_citations) == 2


def test_percentage_difference_and_points():
    executor = DeterministicExecutor()
    op1 = Operand(raw_value="600", normalized_value=Decimal("600"))
    op2 = Operand(raw_value="500", normalized_value=Decimal("500"))

    pct_diff = executor.execute(ExecutionOperator.PERCENTAGE_DIFFERENCE, [op1, op2])
    # ((600 - 500) / 500) * 100 = 20%
    assert pct_diff.result_value == Decimal("20")

    pct_point = executor.execute(ExecutionOperator.PERCENTAGE_POINT_DIFFERENCE, [op1, op2])
    # 600 - 500 = 100
    assert pct_point.result_value == Decimal("100")


def test_ratio_and_division_by_zero():
    executor = DeterministicExecutor()
    op1 = Operand(raw_value="100", normalized_value=Decimal("100"))
    op2 = Operand(raw_value="25", normalized_value=Decimal("25"))

    ratio = executor.execute(ExecutionOperator.RATIO, [op1, op2])
    assert ratio.result_value == Decimal("4")

    # Division by zero
    op_zero = Operand(raw_value="0", normalized_value=Decimal("0"))
    with pytest.raises(ExecutionError) as exc:
        executor.execute(ExecutionOperator.RATIO, [op1, op_zero])
    assert "Division by zero" in str(exc.value)


def test_aggregations_sum_avg_min_max():
    executor = DeterministicExecutor()
    operands = [
        Operand("10", Decimal("10")),
        Operand("20", Decimal("20")),
        Operand("30", Decimal("30")),
    ]

    res_sum = executor.execute(ExecutionOperator.SUM, operands)
    assert res_sum.result_value == Decimal("60")

    res_avg = executor.execute(ExecutionOperator.AVERAGE, operands)
    assert res_avg.result_value == Decimal("20")

    res_min = executor.execute(ExecutionOperator.MIN, operands)
    assert res_min.result_value == Decimal("10")

    res_max = executor.execute(ExecutionOperator.MAX, operands)
    assert res_max.result_value == Decimal("30")


def test_date_difference():
    executor = DeterministicExecutor()
    op1 = Operand(raw_value="2023-05-15", normalized_value="2023-05-15")
    op2 = Operand(raw_value="2023-05-01", normalized_value="2023-05-01")

    res = executor.execute(ExecutionOperator.DATE_DIFFERENCE, [op1, op2])
    assert res.result_value == Decimal("14")
