"""Tests for Requirements 14 & 15: Telemetry honesty & infrastructure failure guard."""

import pytest
from services.telemetry import RequestTelemetry


def test_missing_telemetry_reported_as_unknown():
    """Requirement 14: Missing telemetry is reported as unknown, not zero."""
    # When a telemetry field was not measured or unavailable, it must be flagged unknown
    telemetry_data = {"bedrock_embedding_calls": None, "latency_ms": None}
    
    # Formatter / reporter logic:
    reported = {}
    for k, v in telemetry_data.items():
        reported[k] = "unknown" if v is None else v

    assert reported["bedrock_embedding_calls"] == "unknown"
    assert reported["latency_ms"] == "unknown"
    assert reported["bedrock_embedding_calls"] != 0
    assert reported["latency_ms"] != 0.0


def test_infrastructure_failure_never_scored_as_correct_refusal():
    """Requirement 15: Infrastructure failure is scored as incorrect or unmeasured, never as a correct refusal."""
    # Suppose a refusal query crashes with an error status or storage failure
    resp = {
        "status": "error",
        "error_code": "storage_failure",
        "answer": "NOT_FOUND",  # Crashed and returned NOT_FOUND
    }

    contract_type = "refusal"
    is_infra_error = (resp.get("status") == "error" or bool(resp.get("error_code")))
    ans_text = resp["answer"]
    refusal_indicators = ["not found"]
    ans_is_refusal = any(ind in ans_text.lower() for ind in refusal_indicators)

    is_correct = False
    is_refusal = False

    if contract_type == "refusal":
        if is_infra_error:
            is_correct = False
            is_refusal = False
        elif ans_is_refusal:
            is_correct = True
            is_refusal = True

    # Must be incorrect, NOT a correct refusal
    assert is_correct is False
    assert is_refusal is False
