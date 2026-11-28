"""Offline regressions for run_laya_smoke live-mode guards, stub path, and cap enforcement."""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

import pytest

from scripts.run_laya_smoke import (
    OutboundCallTracker,
    OutboundCapExceededError,
    build_bedrock_no_retry_config,
    run_smoke,
)


def _base_args(offline_stub: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        workspace_id="ws_b540c5784fef",
        source_version="2de945512ab7b3668c08374d4867c038",
        arms="deterministic,laya_old,laya_dynamic",
        max_laya_calls=4,
        max_synthesis_calls=6,
        max_embedding_calls=6,
        max_retries=0,
        offline_stub=offline_stub,
    )


def test_live_mode_rejects_environment_test_before_dispatch(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    with (
        patch("requests.post") as mock_post,
        patch("aws.infra.get_client") as mock_client,
    ):
        with pytest.raises(RuntimeError, match="ENVIRONMENT=test"):
            run_smoke(_base_args(offline_stub=False))
    mock_post.assert_not_called()
    mock_client.assert_not_called()


def test_live_mode_rejects_test_synthesis_provider_before_dispatch(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setattr("config.settings.ENVIRONMENT", "dev")
    monkeypatch.setenv("LLM_PROVIDER", "test")
    with (
        patch("requests.post") as mock_post,
        patch("aws.infra.get_client") as mock_client,
    ):
        with pytest.raises(RuntimeError, match="resolved synthesis provider 'test'"):
            run_smoke(_base_args(offline_stub=False))
    mock_post.assert_not_called()
    mock_client.assert_not_called()


def test_offline_stub_recovers_active_row5_and_refuses_nosup1():
    summary = run_smoke(_base_args(offline_stub=True))
    assert summary["offline_stub"] is True
    assert summary["retries"]["bedrock_retries"] == {"max_attempts": 0}
    assert summary["outbound_attempts"] == {
        "laya_attempts": 4,
        "synthesis_attempts": 4,
        "embedding_attempts": 6,
    }
    # Verify no credentials leak into serialized trace
    serialized = json.dumps(summary)
    assert "Authorization" not in serialized
    assert "Bearer" not in serialized

    for arm in ("deterministic", "laya_old", "laya_dynamic"):
        lex = next(r for r in summary["results"] if r["arm"] == arm and r["case_id"] == "LEX-1")
        assert lex["row5_in_initial"] is False
        assert "11" in lex["delivered_answer"]
        assert lex["delivered_citations"] == [
            {
                "chunk_id": "ee9fa513-fee8-54be-ba11-fc661c9ef680:csv:row5",
                "document_version": "2de945512ab7b3668c08374d4867c038",
            }
        ]
        assert lex["raw_answer"] is not None

        nosup = next(r for r in summary["results"] if r["arm"] == arm and r["case_id"] == "NOSUP-1")
        assert nosup["delivered_answer"] == "NOT_FOUND"
        assert nosup["delivered_citations"] == []


def test_outbound_caps_include_retries_and_enforce_zero_sdk_retries():
    assert build_bedrock_no_retry_config().retries == {"max_attempts": 0}
    tracker = OutboundCallTracker(max_laya=4, max_synthesis=6, max_embedding=6)
    for _ in range(4):
        tracker.record_laya()
    with pytest.raises(OutboundCapExceededError, match="LAYA cap exceeded"):
        tracker.record_laya()

    for _ in range(6):
        tracker.record_synthesis()
    with pytest.raises(OutboundCapExceededError, match="Synthesis cap exceeded"):
        tracker.record_synthesis()

    for _ in range(6):
        tracker.record_embedding()
    with pytest.raises(OutboundCapExceededError, match="Embedding cap exceeded"):
        tracker.record_embedding()
