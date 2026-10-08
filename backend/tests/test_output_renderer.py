from decimal import Decimal
import json

import pytest

from services.retrieval.output_renderer import (
    OutputContractViolation,
    render_output,
)


def test_json_contract_clean_and_valid():
    raw_response = "```json\n{\n  \"metric\": \"revenue\",\n  \"value\": 500\n}\n```"
    rendered = render_output(raw_response, contract="json", strict_contract=True)
    obj = json.loads(rendered)
    assert obj["metric"] == "revenue"
    assert obj["value"] == 500


def test_strict_json_contract_violation_missing_fields():
    raw_response = '{"other_key": 123}'
    # Require total_revenue
    rendered = render_output(
        raw_response,
        contract="json",
        strict_contract=True,
        required_json_keys=["total_revenue"],
    )
    obj = json.loads(rendered)
    assert obj["status"] == "output_contract_violation"
    assert "Missing required fields" in obj["error"]


def test_number_contract_cleaning():
    assert render_output(Decimal("882.00"), contract="number") == "882"
    assert render_output("882.0", contract="number") == "882"
    assert render_output("$1,234.50", contract="number") == "1234.50"
    assert render_output(100, contract="number") == "100"


def test_boolean_contract():
    assert render_output("yes", contract="boolean") == "True"
    assert render_output(True, contract="boolean") == "True"
    assert render_output("no", contract="boolean") == "False"
    assert render_output(False, contract="boolean") == "False"


def test_list_contract():
    items = ("First item", "Second item", "Third item")
    rendered = render_output(items, contract="list")
    assert "- First item" in rendered
    assert "- Second item" in rendered
    assert "- Third item" in rendered
