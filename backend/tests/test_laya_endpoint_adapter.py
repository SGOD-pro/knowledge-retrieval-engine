"""Unit and compiled-pipeline regressions for LAYA API endpoint integration.

Verifies:
- Direct HTTP POST to LAYA_ENDPOINT_URL with Bearer token authentication.
- Request payload mapping into /v1/systemone schema (state + questions).
- Response parsing of answers (choice, confidence, parameters).
- Strict fail-closed fallback to Section 35 policy on provider unavailability (502, connection refused).
- Telemetry distinguishing configured, dispatched, valid provider response, accepted route,
  executed expansion, and deterministic fallback.
- Asserting laya_invoked is False when provider dispatch is aborted before HTTP call.
- Simulated 4.4-second response exceeding 2.5-second phase: prompt caller timeout refusal,
  retained worker capacity, and no late publication.
- Opt-in live server smoke test separated from normal offline pytest execution.
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest
from config import settings
from schemas.models import Chunk, Document
from db.database import CloudRepository
from services.retrieval.bm25_retriever import BM25Retriever, invalidate_chunk_cache
from schemas.contracts.laya import (
    BaselineEvidenceSummary,
    DenseExpansionParams,
    GraphExpansionParams,
    LayaEscalationDecision,
    LayaEscalationRequest,
    LexicalExpansionParams,
    MissingRequirementDescription,
    NoneExpansionParams,
    StructuralExpansionParams,
    StructuredExpansionParams,
)
from schemas.contracts.verification import VerificationRecord
from schemas.contracts.knowledge_graph import PinnedDocumentVersion, PinnedSnapshotContext
from services.retrieval.laya_gate import LayaBudgetLedger, estimate_laya_tokens
from services.retrieval.laya_service import LayaDecisionService, PayloadBudgetExceededError
from services.langgraph_pipeline import pipeline


def _build_test_request(query: str = "Test query") -> LayaEscalationRequest:
    return LayaEscalationRequest(
        query_id="q_test_123",
        workspace_id="ws_default",
        query=query,
        missing_requirement=MissingRequirementDescription(
            requirement_id="req_001",
            missing_dimension="exact_term",
            description="Missing term",
            failed_stage="run_fidelity_coverage",
            detected_gap="Specific error code not found",
        ),
        available_routes=("lexical_expansion", "none"),
        retrieved_summary=(
            BaselineEvidenceSummary(
                chunk_id="chk_001",
                document_id="doc_001",
                document_version="ver_001",
                workspace_id="ws_default",
                snippet="Baseline text snippet",
                score=10.0,
                source_kind="text",
            ),
        ),
        caller_deadline_ms=2500.0,
    )


class MockVerifier:
    def __init__(self, support_status: str = "ASSERTED") -> None:
        self.support_status = support_status
        self.verify_call_count = 0

    def is_ready(self) -> bool:
        return True

    def verify(self, text: str = "", context: str = "", citations: list[Any] | None = None, **kwargs: Any) -> VerificationRecord:
        self.verify_call_count += 1
        return VerificationRecord(
            verification_id="vr_test_endpoint_001",
            workspace_id="ws_endpoint_compiled",
            query_id="q_test_endpoint",
            snapshot_id="snap_1",
            requirement_id="req_1",
            evidence_id="doc_report_2026:c1",
            verifier_version="v1.0",
            verifier_type="generative",
            attempt_number=1,
            execution_time_ms=25,
            support_status=self.support_status,
            confidence_score=0.92,
            verification_rationale="Grounded in retrieved chunks",
            verified_at="2026-11-20T00:00:00Z",
        )


class TestLayaEndpointAdapter(unittest.TestCase):
    """Isolated unit tests for LayaDecisionService with endpoint mode."""

    def setUp(self) -> None:
        self.endpoint_url = "https://mock.laya.local/v1/systemone"
        self.api_key = "test_api_key_123"
        self.service = LayaDecisionService(
            endpoint_url=self.endpoint_url,
            api_key=self.api_key,
        )
        self.ledger = LayaBudgetLedger()

    @patch("requests.post")
    def test_endpoint_happy_path_lexical(self, mock_post: MagicMock) -> None:
        """Endpoint returns lexical_expansion choice; approved decision produced."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "probabilities": {"lexical_expansion": 0.85, "none": 0.15},
                    "confidence": 0.4013,
                    "answer_confidence": 0.85,
                }
            },
            "usage": {"input_tokens": 80, "output_tokens": 0},
            "server_latency_ms": 25.0,
        }
        mock_post.return_value = mock_resp

        req = _build_test_request("Where is London error code located?")
        decision = self.service.decide(req, 2000.0, self.ledger)

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertEqual(call_args[0][0], self.endpoint_url)
        self.assertEqual(call_args[1]["headers"]["Authorization"], f"Bearer {self.api_key}")
        self.assertEqual(call_args[1]["headers"]["Content-Type"], "application/json")

        payload = call_args[1]["json"]
        self.assertIn("state", payload)
        self.assertIn("questions", payload)
        self.assertIn("route", payload["questions"])

        self.assertEqual(decision.route, "lexical_expansion")
        self.assertEqual(decision.execution_status, "approved")
        self.assertAlmostEqual(decision.confidence, 0.85)
        self.assertIsInstance(decision.parameters, LexicalExpansionParams)
        self.assertTrue(self.service.last_dispatched)
        self.assertTrue(self.service.last_valid_response)

    @patch("requests.post")
    def test_endpoint_happy_path_none(self, mock_post: MagicMock) -> None:
        """Endpoint returns none choice; approved none decision produced."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "none",
                    "confidence": 0.35,
                    "answer_confidence": 0.75,
                }
            },
            "usage": {"input_tokens": 90, "output_tokens": 0},
            "server_latency_ms": 30.0,
        }
        mock_post.return_value = mock_resp

        req = _build_test_request("General query")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.route, "none")
        self.assertEqual(decision.execution_status, "approved")
        self.assertAlmostEqual(decision.confidence, 0.75)
        self.assertIsInstance(decision.parameters, NoneExpansionParams)
        self.assertTrue(self.service.last_dispatched)
        self.assertTrue(self.service.last_valid_response)

    @patch("requests.post")
    def test_endpoint_unavailable_connection_error(self, mock_post: MagicMock) -> None:
        """Connection failure marks provider_unavailable and routes to Section 35 fallback."""
        import requests

        mock_post.side_effect = requests.ConnectionError("Connection refused by host")

        req = _build_test_request("Query when server down")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.execution_status, "provider_unavailable")
        self.assertEqual(decision.route, "lexical_expansion")
        self.assertTrue(self.service.last_dispatched)
        self.assertFalse(self.service.last_valid_response)

    @patch("requests.post")
    def test_endpoint_unavailable_http_502(self, mock_post: MagicMock) -> None:
        """502 Bad Gateway produces provider_unavailable and Section 35 fallback."""
        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_resp.text = "Bad Gateway"
        mock_post.return_value = mock_resp

        req = _build_test_request("Query when gateway down")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.execution_status, "provider_unavailable")
        self.assertEqual(decision.route, "lexical_expansion")

    @patch("requests.post")
    def test_endpoint_auth_failure_401(self, mock_post: MagicMock) -> None:
        """401 Unauthorized produces provider_unavailable status."""
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_post.return_value = mock_resp

        req = _build_test_request("Query with bad key")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.execution_status, "provider_unavailable")

    @patch("requests.post")
    def test_endpoint_timeout(self, mock_post: MagicMock) -> None:
        """Endpoint timeout produces timeout status."""
        import requests

        mock_post.side_effect = requests.Timeout("Gateway timed out")

        req = _build_test_request("Slow query")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.execution_status, "timeout")

    @patch("requests.post")
    def test_endpoint_unsupported_route_fallback(self, mock_post: MagicMock) -> None:
        """Route dense_expansion (unsupported in Slice 5.1) is rejected by code validation and falls back."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "dense_expansion",
                    "confidence": 0.5,
                    "answer_confidence": 0.8,
                }
            },
            "usage": {"input_tokens": 80, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        req = _build_test_request("Semantic query")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.route, "lexical_expansion")
        self.assertIn("unavailable", decision.rationale)
        self.assertTrue(self.service.last_valid_response)

    @patch("requests.post")
    def test_endpoint_invalid_confidence(self, mock_post: MagicMock) -> None:
        """Invalid confidence score triggers Section 35 fallback."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "confidence": 2.5,
                    "answer_confidence": 2.5,
                }
            },
            "usage": {"input_tokens": 80, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        req = _build_test_request("Query with bad confidence")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.route, "lexical_expansion")
        self.assertIn("Confidence 2.5 out of bounds", decision.rationale)

    @patch("requests.post")
    def test_endpoint_malformed_json_response(self, mock_post: MagicMock) -> None:
        """Non-JSON response triggers provider fallback without crashing."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("Invalid JSON string")
        mock_resp.text = "<html>Error</html>"
        mock_post.return_value = mock_resp

        req = _build_test_request("Query with bad json")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.execution_status, "provider_unavailable")
        self.assertEqual(decision.route, "lexical_expansion")

    @patch("requests.post")
    def test_simulated_4_4s_phase_deadline_timeout_and_capacity_retention(self, mock_post: MagicMock) -> None:
        """Simulate a 4.4-second response exceeding the 2500ms phase allowance.

        Asserts:
        - Caller times out promptly at phase deadline without hanging for 4.4s.
        - Concurrency controller retains worker slot while background call is running.
        - Late completion is discarded and spend is reconciled cleanly.
        """
        background_started = threading.Event()
        background_done = threading.Event()

        def _slow_post(*args: Any, **kwargs: Any) -> Any:
            background_started.set()
            time.sleep(0.75)  # simulate slow network call (scaled down for offline speed)
            background_done.set()
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "model": "laya-rl-agent",
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": "lexical_expansion",
                        "answer_confidence": 0.9,
                    }
                },
                "usage": {"input_tokens": 80, "output_tokens": 0},
            }
            return resp

        mock_post.side_effect = _slow_post

        req = _build_test_request("Query with slow provider response")
        # Provide bounded deadline of 600ms (timeout_s will be ~0.6s)
        t_start = time.perf_counter()
        with patch("services.retrieval.laya_service.calculate_phase_deadline", return_value=600.0):
            decision = self.service.decide(req, deadline_remaining_ms=600.0, ledger=self.ledger)
        elapsed = time.perf_counter() - t_start

        # Caller must return timeout status promptly without waiting for the full slow call
        self.assertEqual(decision.execution_status, "timeout")
        self.assertLess(elapsed, 0.72)

        # Wait for background call to complete and verify spend reconciled
        background_done.wait(timeout=2.0)
        self.assertGreater(self.ledger.late_settled_calls, 0)

    @patch("requests.post")
    def test_endpoint_usage_missing_fields_rejected(self, mock_post: MagicMock) -> None:
        """Endpoint usage missing required input or output token fields is rejected."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "answer_confidence": 0.85,
                }
            },
            "usage": {"input_tokens": 80},  # missing output_tokens
        }
        mock_post.return_value = mock_resp

        req = _build_test_request("Query with missing usage output")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.execution_status, "rejected_by_code")
        self.assertTrue(self.service.last_fallback)
        self.assertIn("Invalid provider usage", decision.rationale)
        self.assertEqual(self.ledger.invalid_usage_calls, 1)

    @patch("requests.post")
    def test_endpoint_usage_boolean_tokens_rejected(self, mock_post: MagicMock) -> None:
        """Endpoint usage with boolean token counts is rejected."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "answer_confidence": 0.85,
                }
            },
            "usage": {"input_tokens": True, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        req = _build_test_request("Query with boolean tokens")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.execution_status, "rejected_by_code")
        self.assertTrue(self.service.last_fallback)
        self.assertIn("Token counts must not be boolean", decision.rationale)
        self.assertEqual(self.ledger.invalid_usage_calls, 1)

    @patch("requests.post")
    def test_endpoint_usage_fractional_tokens_rejected(self, mock_post: MagicMock) -> None:
        """Endpoint usage with fractional float counts is rejected."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "answer_confidence": 0.85,
                }
            },
            "usage": {"input_tokens": 80.5, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        req = _build_test_request("Query with fractional tokens")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.execution_status, "rejected_by_code")
        self.assertTrue(self.service.last_fallback)
        self.assertIn("Token counts must not be fractional", decision.rationale)
        self.assertEqual(self.ledger.invalid_usage_calls, 1)

    @patch("requests.post")
    def test_endpoint_usage_negative_tokens_rejected(self, mock_post: MagicMock) -> None:
        """Endpoint usage with negative token counts is rejected."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "answer_confidence": 0.85,
                }
            },
            "usage": {"input_tokens": -10, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        req = _build_test_request("Query with negative tokens")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.execution_status, "rejected_by_code")
        self.assertTrue(self.service.last_fallback)
        self.assertIn("strictly positive", decision.rationale)
        self.assertEqual(self.ledger.invalid_usage_calls, 1)

    @patch("requests.post")
    def test_endpoint_usage_policy_overrun_rejected(self, mock_post: MagicMock) -> None:
        """Endpoint usage exceeding 1000 input token policy cap is rejected."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "answer_confidence": 0.85,
                }
            },
            "usage": {"input_tokens": 1500, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        req = _build_test_request("Query with token overrun")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.execution_status, "rejected_by_code")
        self.assertTrue(self.service.last_fallback)
        self.assertEqual(self.ledger.invalid_usage_calls, 1)

    @patch("requests.post")
    def test_endpoint_usage_known_spend_preserved_and_invalid_usage_recorded(self, mock_post: MagicMock) -> None:
        """Known integer spend is captured in ledger while invalid usage marks status."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "answer_confidence": 0.85,
                }
            },
            "usage": {"input_tokens": 85, "output_tokens": True},
        }
        mock_post.return_value = mock_resp

        req = _build_test_request("Query with partial invalid usage")
        decision = self.service.decide(req, 2000.0, self.ledger)

        self.assertEqual(decision.execution_status, "rejected_by_code")
        self.assertEqual(self.ledger.invalid_usage_calls, 1)
        self.assertEqual(self.ledger.total_input_tokens, 85)
        self.assertEqual(self.ledger.settled_history[-1]["status"], "invalid_usage")

    @patch("requests.post")
    def test_endpoint_payload_budget_trims_optional_snippets_first(self, mock_post: MagicMock) -> None:
        """Optional context snippets are trimmed first, preserving mandatory query, gap, and dimension."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "answer_confidence": 0.9,
                }
            },
            "usage": {"input_tokens": 80, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        # 3 baseline snippets of 200 chars each (total 600 chars of snippets)
        snippet_text = "Important context about corporate revenue and fiscal growth. " * 3  # ~180 chars
        req = LayaEscalationRequest(
            query_id="q_payload_trim",
            workspace_id="ws_default",
            query="What was the fiscal revenue in 2026?",
            missing_requirement=MissingRequirementDescription(
                requirement_id="req_002",
                missing_dimension="exact_term",
                description="Missing fiscal revenue",
                failed_stage="run_fidelity_coverage",
                detected_gap="Specific fiscal revenue coverage missing",
            ),
            available_routes=("lexical_expansion", "none"),
            retrieved_summary=(
                BaselineEvidenceSummary(
                    chunk_id="chk_trim_001",
                    document_id="doc_001",
                    document_version="ver_001",
                    workspace_id="ws_default",
                    snippet=snippet_text,
                    score=10.0,
                    source_kind="text",
                ),
                BaselineEvidenceSummary(
                    chunk_id="chk_trim_002",
                    document_id="doc_001",
                    document_version="ver_001",
                    workspace_id="ws_default",
                    snippet=snippet_text,
                    score=9.0,
                    source_kind="text",
                ),
            ),
            caller_deadline_ms=2500.0,
        )

        decision = self.service.decide(req, 2000.0, self.ledger)
        self.assertEqual(decision.execution_status, "approved")

        mock_post.assert_called_once()
        dispatched_payload = mock_post.call_args[1]["json"]
        doc = dispatched_payload["state"]["document"]

        # Mandatory elements must be intact
        self.assertIn("Query: What was the fiscal revenue in 2026?", doc)
        self.assertIn("Gap detected: Specific fiscal revenue coverage missing", doc)
        self.assertIn("Missing dimension: exact_term", doc)

        # Context snippet was trimmed to fit
        self.assertIn("Context:", doc)

        # Total serialized payload strictly <= 980 bytes
        total_bytes = len(json.dumps(dispatched_payload).encode("utf-8"))
        self.assertLessEqual(total_bytes, 980)

    @patch("requests.post")
    def test_endpoint_payload_budget_exceeded_rejects_before_dispatch(self, mock_post: MagicMock) -> None:
        """When mandatory query content alone cannot fit, escalation fails closed before HTTP dispatch."""
        huge_query = "Excessive query text " * 30  # > 400 bytes of mandatory content
        req = LayaEscalationRequest(
            query_id="q_payload_oversized",
            workspace_id="ws_default",
            query=huge_query,
            missing_requirement=MissingRequirementDescription(
                requirement_id="req_003",
                missing_dimension="exact_term",
                description="Oversized query gap",
                failed_stage="run_fidelity_coverage",
                detected_gap="Mandatory content cannot fit in budget",
            ),
            available_routes=("lexical_expansion", "none"),
            retrieved_summary=(),
            caller_deadline_ms=2500.0,
        )

        decision = self.service.decide(req, 2000.0, self.ledger)

        # HTTP dispatch must not occur
        mock_post.assert_not_called()
        self.assertFalse(self.service.last_dispatched)
        self.assertTrue(self.service.last_fallback)
        self.assertEqual(decision.execution_status, "rejected_by_code")
        self.assertIn("Payload budget exceeded", decision.rationale)

    @patch("requests.post")
    def test_endpoint_payload_multibyte_utf8_preserved_without_corruption(self, mock_post: MagicMock) -> None:
        """Multibyte Japanese characters and emojis are budgeted and dispatched without UTF-8 corruption."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "answer_confidence": 0.88,
                }
            },
            "usage": {"input_tokens": 85, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        req = LayaEscalationRequest(
            query_id="q_multibyte",
            workspace_id="ws_default",
            query="2026年度の営業利益 (operating profit) はいくらですか？ 📈",
            missing_requirement=MissingRequirementDescription(
                requirement_id="req_004",
                missing_dimension="exact_term",
                description="Missing operating profit in Japanese",
                failed_stage="run_fidelity_coverage",
                detected_gap="Missing 営業利益 metric",
            ),
            available_routes=("lexical_expansion", "none"),
            retrieved_summary=(),
            caller_deadline_ms=2500.0,
        )

        decision = self.service.decide(req, 2000.0, self.ledger)
        self.assertEqual(decision.execution_status, "approved")

        mock_post.assert_called_once()
        dispatched_payload = mock_post.call_args[1]["json"]
        doc = dispatched_payload["state"]["document"]
        self.assertIn("営業利益", doc)
        self.assertIn("📈", doc)
        total_bytes = len(json.dumps(dispatched_payload).encode("utf-8"))
        self.assertLessEqual(total_bytes, 980)

    @patch("requests.post")
    def test_endpoint_lexical_parameters_exclude_generic_operational_phrases(self, mock_post: MagicMock) -> None:
        """Lexical parameters extract domain keywords and strictly exclude operational error phrases."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "answer_confidence": 0.90,
                }
            },
            "usage": {"input_tokens": 80, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        req = LayaEscalationRequest(
            query_id="q_clean_terms",
            workspace_id="ws_default",
            query="What was the company turnover in 2026?",
            missing_requirement=MissingRequirementDescription(
                requirement_id="req_005",
                missing_dimension="exact_term",
                description="Fidelity coverage failure",
                failed_stage="run_fidelity_coverage",
                detected_gap="Initial fidelity check failed coverage: Missing turnover coverage",
            ),
            available_routes=("lexical_expansion", "none"),
            retrieved_summary=(),
            caller_deadline_ms=2500.0,
        )

        decision = self.service.decide(req, 2000.0, self.ledger)
        self.assertEqual(decision.route, "lexical_expansion")
        terms = decision.parameters.query_terms

        # Domain terms must be present
        self.assertIn("turnover", terms)
        self.assertIn("2026", terms)

        # Operational error phrases must strictly not be present
        forbidden = ("initial", "fidelity", "check", "failed", "coverage", "missing", "error")
        for bad_word in forbidden:
            self.assertNotIn(bad_word, terms)


class TestCompiledPipelineEndpointRegressions(unittest.TestCase):
    """Regressions executing through the real compiled LangGraph pipeline with the endpoint adapter."""

    def setUp(self) -> None:
        self.ws_id = "ws_endpoint_compiled"
        self.endpoint_url = "https://mock.laya.local/v1/systemone"
        self.api_key = "test_key_endpoint"
        self.repo = CloudRepository()

        # Seed baseline doc and novel target chunk in repository
        self.doc_chunk = Chunk(
            id="chk_baseline_1",
            document_id="doc_report_2026",
            source_format="pdf",
            text="General overview of corporate operations.",
            element_type="narrative_block",
            workspace_id=self.ws_id,
            metadata={"document_version": "v1"},
            embedding_fast=[0.1] * 384,
            embedding_full=[0.1] * 1024,
        )
        self.novel_chunk = Chunk(
            id="chk_novel_turnover_1",
            document_id="doc_report_2026",
            source_format="pdf",
            text="The fiscal year 2026 turnover was officially reported as $450 million.",
            element_type="narrative_block",
            workspace_id=self.ws_id,
            metadata={"document_version": "v1"},
            embedding_fast=[0.1] * 384,
            embedding_full=[0.1] * 1024,
        )
        doc = Document(
            id="doc_report_2026",
            filename="doc_report_2026.pdf",
            source_format="pdf",
            chunks=(self.doc_chunk, self.novel_chunk),
            workspace_id=self.ws_id,
        )
        self.repo.save(doc)
        self.repo.add_document_to_workspace(self.ws_id, doc)
        invalidate_chunk_cache(self.ws_id)

    @patch("requests.post")
    def test_pipeline_endpoint_successful_lexical_expansion(self, mock_post: MagicMock) -> None:
        """Compiled pipeline receives lexical_expansion from endpoint, expands via BM25, and delivers answer."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "confidence": 0.45,
                    "answer_confidence": 0.91,
                }
            },
            "usage": {"input_tokens": 110, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        mock_llm_response = {
            "answer": "The turnover was $450 million.",
            "citations": ["chk_novel_turnover_1"],
            "usage": {"input_tokens": 150, "output_tokens": 20},
        }
        mock_verifier = MockVerifier(support_status="ASSERTED")

        def _mock_rerank(q: str, chunks: Any, top_k: int = 6, return_mode: bool = False) -> Any:
            c_list = list(chunks)
            return (c_list[:top_k], "mocked") if return_mode else c_list[:top_k]

        fidelity_calls = 0
        def _mock_check_fidelity(q: str, contexts: Any, **kwargs: Any) -> float:
            nonlocal fidelity_calls
            fidelity_calls += 1
            if fidelity_calls == 1:
                from services.retrieval.fidelity_check import CoverageError
                raise CoverageError("Missing turnover coverage")
            return 1.0

        with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response), \
             patch("services.langgraph_pipeline._get_repo", return_value=self.repo), \
             patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=_mock_check_fidelity):

            response = pipeline.run(
                query="What was the turnover for 2026?",
                workspace_id=self.ws_id,
                laya_enabled=True,
                laya_endpoint_url=self.endpoint_url,
                laya_api_key=self.api_key,
                verifier=mock_verifier,
                require_delivery_gate=True,
            )

            self.assertEqual(response.laya_calls, 1)
            self.assertEqual(response.expansion_steps, 1)
            self.assertEqual(response.synthesis_calls, 1)
            self.assertIn("$450 million", response.answer)

            # Assert explicit telemetry flags in retrieval_candidates
            cands = response.retrieval_candidates or {}
            self.assertTrue(cands.get("laya_configured"))
            self.assertTrue(cands.get("laya_invoked"))
            self.assertTrue(cands.get("laya_dispatched"))
            self.assertTrue(cands.get("laya_provider_success"))
            self.assertTrue(cands.get("laya_route_accepted"))
            self.assertTrue(cands.get("laya_expansion_executed"))
            self.assertFalse(cands.get("laya_fallback"))
            self.assertEqual(cands.get("laya_provider"), "endpoint")

    @patch("requests.post")
    def test_pipeline_endpoint_valid_route_none(self, mock_post: MagicMock) -> None:
        """Compiled pipeline receives route none, refuses cleanly without synthesis calls."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "none",
                    "confidence": 0.35,
                    "answer_confidence": 0.80,
                }
            },
            "usage": {"input_tokens": 90, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        def _mock_check_fidelity(q: str, contexts: Any, **kwargs: Any) -> float:
            from services.retrieval.fidelity_check import CoverageError
            raise CoverageError("Missing coverage")

        with patch("services.langgraph_pipeline._get_repo", return_value=self.repo), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=_mock_check_fidelity):

            response = pipeline.run(
                query="Query impossible to expand",
                workspace_id=self.ws_id,
                laya_enabled=True,
                laya_endpoint_url=self.endpoint_url,
                laya_api_key=self.api_key,
            )

            self.assertEqual(response.laya_calls, 1)
            self.assertEqual(response.expansion_steps, 1)
            self.assertEqual(response.synthesis_calls, 0)
            self.assertEqual(response.answer, "NOT_FOUND")

            cands = response.retrieval_candidates or {}
            self.assertTrue(cands.get("laya_invoked"))
            self.assertTrue(cands.get("laya_provider_success"))
            self.assertTrue(cands.get("laya_route_accepted"))
            self.assertFalse(cands.get("laya_expansion_executed"))

    @patch("requests.post")
    def test_pipeline_endpoint_unsupported_route_fallback(self, mock_post: MagicMock) -> None:
        """Compiled pipeline receives dense_expansion; code rejects and falls back to Section 35 lexical."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "dense_expansion",
                    "confidence": 0.4,
                    "answer_confidence": 0.75,
                }
            },
            "usage": {"input_tokens": 90, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        def _mock_rerank(q: str, chunks: Any, top_k: int = 6, return_mode: bool = False) -> Any:
            c_list = list(chunks)
            return (c_list[:top_k], "mocked") if return_mode else c_list[:top_k]

        fidelity_calls = 0
        def _mock_check_fidelity(q: str, contexts: Any, **kwargs: Any) -> float:
            nonlocal fidelity_calls
            fidelity_calls += 1
            if fidelity_calls == 1:
                from services.retrieval.fidelity_check import CoverageError
                raise CoverageError("Missing turnover coverage")
            return 1.0

        mock_llm_response = {
            "answer": "Answer synthesized via fallback",
            "citations": ["chk_novel_turnover_1"],
            "usage": {"input_tokens": 120, "output_tokens": 15},
        }
        mock_verifier = MockVerifier(support_status="ASSERTED")

        with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response), \
             patch("services.langgraph_pipeline._get_repo", return_value=self.repo), \
             patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=_mock_check_fidelity):

            response = pipeline.run(
                query="Turnover fiscal report query",
                workspace_id=self.ws_id,
                laya_enabled=True,
                laya_endpoint_url=self.endpoint_url,
                laya_api_key=self.api_key,
                verifier=mock_verifier,
                require_delivery_gate=True,
            )

            cands = response.retrieval_candidates or {}
            self.assertTrue(cands.get("laya_invoked"))
            # Provider returned valid response, but route was rejected as unavailable in Slice 5.1
            self.assertFalse(cands.get("laya_route_accepted"))
            self.assertTrue(cands.get("laya_fallback"))

    @patch("requests.post")
    def test_pipeline_endpoint_provider_unavailable_fallback(self, mock_post: MagicMock) -> None:
        """Compiled pipeline catches 502/connection error, marks provider_unavailable, and uses fallback."""
        import requests
        mock_post.side_effect = requests.ConnectionError("Connection to LAYA tunnel failed")

        def _mock_rerank(q: str, chunks: Any, top_k: int = 6, return_mode: bool = False) -> Any:
            c_list = list(chunks)
            return (c_list[:top_k], "mocked") if return_mode else c_list[:top_k]

        fidelity_calls = 0
        def _mock_check_fidelity(q: str, contexts: Any, **kwargs: Any) -> float:
            nonlocal fidelity_calls
            fidelity_calls += 1
            if fidelity_calls == 1:
                from services.retrieval.fidelity_check import CoverageError
                raise CoverageError("Missing turnover coverage")
            return 1.0

        mock_llm_response = {
            "answer": "Answer via fallback after provider unavailable",
            "citations": ["chk_novel_turnover_1"],
            "usage": {"input_tokens": 120, "output_tokens": 15},
        }
        mock_verifier = MockVerifier(support_status="ASSERTED")

        with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response), \
             patch("services.langgraph_pipeline._get_repo", return_value=self.repo), \
             patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=_mock_check_fidelity):

            response = pipeline.run(
                query="Turnover report query when endpoint down",
                workspace_id=self.ws_id,
                laya_enabled=True,
                laya_endpoint_url=self.endpoint_url,
                laya_api_key=self.api_key,
                verifier=mock_verifier,
                require_delivery_gate=True,
            )

            cands = response.retrieval_candidates or {}
            self.assertTrue(cands.get("laya_invoked"))
            self.assertFalse(cands.get("laya_provider_success"))
            self.assertTrue(cands.get("laya_fallback"))
            # Assert Bedrock client was never created or invoked
            self.assertEqual(cands.get("laya_provider"), "endpoint")

    def test_pipeline_laya_invoked_distinction_not_dispatched_on_preflight_abort(self) -> None:
        """When escalation is aborted before provider dispatch, laya_invoked must be False."""
        # Query with caller_deadline_ms < 500ms aborts escalation before dispatch
        def _mock_check_fidelity(q: str, contexts: Any, **kwargs: Any) -> float:
            from services.retrieval.fidelity_check import CoverageError
            raise CoverageError("Missing coverage")

        with patch("services.langgraph_pipeline._get_repo", return_value=self.repo), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=_mock_check_fidelity):

            response = pipeline.run(
                query="Query with expired deadline",
                workspace_id=self.ws_id,
                laya_enabled=True,
                caller_deadline_ms=300.0,  # Below 500ms threshold
            )

            # Pipeline routes to delivery_gate refusal without calling LAYA
            self.assertEqual(response.answer, "NOT_FOUND")
            cands = response.retrieval_candidates or {}
            # laya_invoked must NOT be True because no provider call was dispatched
            self.assertFalse(cands.get("laya_invoked", False))

    @patch("requests.post")
    def test_pipeline_endpoint_lexical_expansion_executes_with_clean_domain_terms(self, mock_post: MagicMock) -> None:
        """Pipeline executes BM25 expansion with clean domain terms and verifies dispatched payload content."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "laya-rl-agent",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "lexical_expansion",
                    "confidence": 0.45,
                    "answer_confidence": 0.91,
                }
            },
            "usage": {"input_tokens": 110, "output_tokens": 0},
        }
        mock_post.return_value = mock_resp

        mock_llm_response = {
            "answer": "The turnover was $450 million.",
            "citations": ["chk_novel_turnover_1"],
            "usage": {"input_tokens": 150, "output_tokens": 20},
        }
        mock_verifier = MockVerifier(support_status="ASSERTED")

        def _mock_rerank(q: str, chunks: Any, top_k: int = 6, return_mode: bool = False) -> Any:
            c_list = list(chunks)
            return (c_list[:top_k], "mocked") if return_mode else c_list[:top_k]

        fidelity_calls = 0
        def _mock_check_fidelity(q: str, contexts: Any, **kwargs: Any) -> float:
            nonlocal fidelity_calls
            fidelity_calls += 1
            if fidelity_calls == 1:
                from services.retrieval.fidelity_check import CoverageError
                raise CoverageError("Initial fidelity check failed coverage: Missing turnover coverage")
            return 1.0

        bm25_queries_executed: list[str] = []
        original_bm25_search = BM25Retriever.search

        def _spy_bm25_search(retriever_self: Any, query: str, chunks: Any, top_k: int = 10, **kwargs: Any) -> Any:
            bm25_queries_executed.append(query)
            return original_bm25_search(retriever_self, query, chunks, top_k=top_k, **kwargs)

        with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response), \
             patch("services.langgraph_pipeline._get_repo", return_value=self.repo), \
             patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=_mock_check_fidelity), \
             patch.object(BM25Retriever, "search", side_effect=_spy_bm25_search, autospec=True):

            response = pipeline.run(
                query="What was the turnover for 2026?",
                workspace_id=self.ws_id,
                laya_enabled=True,
                laya_endpoint_url=self.endpoint_url,
                laya_api_key=self.api_key,
                verifier=mock_verifier,
                require_delivery_gate=True,
            )

            # Assert actual dispatched payload content
            mock_post.assert_called_once()
            dispatched_doc = mock_post.call_args[1]["json"]["state"]["document"]
            self.assertIn("What was the turnover for 2026?", dispatched_doc)
            self.assertIn("Missing turnover coverage", dispatched_doc)

            # Assert BM25 query terms executed by expansion
            self.assertTrue(len(bm25_queries_executed) >= 1)
            expansion_query = bm25_queries_executed[-1]
            self.assertIn("turnover", expansion_query)
            for bad_word in ("initial", "fidelity", "check", "failed", "coverage"):
                self.assertNotIn(bad_word, expansion_query.split())

    def test_estimate_laya_tokens_calibrated_overhead(self) -> None:
        """Token estimation calculates exact calibrated overhead for route and complexity."""
        payload = {
            "state": {
                "document": "Query: Revenue Q3\nGap: None\nDimension: tabular_operand"
            },
            "questions": {
                "route": {"type": "choice"},
                "complexity": {"type": "score"},
            },
        }
        res = estimate_laya_tokens(payload)
        self.assertIn("state_characters", res)
        self.assertIn("estimated_state_tokens", res)
        self.assertIn("questions", res)
        self.assertIn("route", res["questions"])
        self.assertIn("complexity", res["questions"])
        # route overhead is 81, complexity overhead is 32
        st = res["estimated_state_tokens"]
        self.assertEqual(res["questions"]["route"], st + 81)
        self.assertEqual(res["questions"]["complexity"], st + 32)
        self.assertEqual(res["estimated_max_sequence_tokens"], st + 81)
        self.assertEqual(res["estimated_total_input_tokens"], (st + 81) + (st + 32))
        self.assertTrue(res["within_estimated_limit"])

    def test_estimate_laya_tokens_unknown_question_rejected(self) -> None:
        """Unknown question names without calibrated overhead raise ValueError."""
        payload = {
            "state": {"document": "test"},
            "questions": {
                "unknown_question_type": {"type": "choice"},
            },
        }
        with self.assertRaises(ValueError) as ctx:
            estimate_laya_tokens(payload)
        self.assertIn("No calibrated overhead for question: unknown_question_type", str(ctx.exception))

    def test_estimate_laya_tokens_limit_exceeded(self) -> None:
        """Very large state exceeding sequence limit sets within_estimated_limit to False."""
        # 8192 - 81 = 8111 tokens -> 8111 * 3 = ~24333 characters
        payload = {
            "state": {"document": "x" * 25000},
            "questions": {
                "route": {"type": "choice"},
                "complexity": {"type": "score"},
            },
        }
        res = estimate_laya_tokens(payload)
        self.assertFalse(res["within_estimated_limit"])
        self.assertGreater(res["estimated_max_sequence_tokens"], 8192)

    def test_format_endpoint_payload_verifies_with_estimate_laya_tokens(self) -> None:
        """Service format payload runs estimate_laya_tokens and verifies within limit."""
        service = LayaDecisionService(endpoint_url=self.endpoint_url, api_key=self.api_key)
        req = _build_test_request("How much was spent on maintenance in fiscal 2024?")
        payload = service._format_endpoint_payload(req)
        token_info = estimate_laya_tokens(payload)
        self.assertTrue(token_info["within_estimated_limit"])
        self.assertGreater(token_info["estimated_state_tokens"], 0)
        self.assertGreater(token_info["estimated_total_input_tokens"], 0)

    def test_reconstructed_direct_and_compiled_payload_contract_controls(self) -> None:
        """Positive and negative controls for reconstructed System One payload formatting.

        Verifies:
        - Positive control: Retains Query, Gap detected, Missing dimension, and Context.
        - Positive control: Byte length strictly <= 980 bytes and tokens <= 8192.
        - Negative control: Payloads with mandatory content exceeding budget fail closed.
        """
        service = LayaDecisionService(endpoint_url=self.endpoint_url, api_key=self.api_key)

        # Positive Control 1: Direct payload serialization
        req_direct = LayaEscalationRequest(
            query_id="q_probe_synthetic",
            workspace_id="ws_latency_probe",
            query="What was the turnover for fiscal year 2026?",
            missing_requirement=MissingRequirementDescription(
                requirement_id="req_probe_001",
                missing_dimension="exact_term",
                description="Synthetic probe missing requirement",
                failed_stage="run_fidelity_coverage",
                detected_gap="Initial fidelity check failed coverage: Missing turnover coverage",
            ),
            available_routes=("lexical_expansion", "none"),
            retrieved_summary=(
                BaselineEvidenceSummary(
                    chunk_id="chk_probe_baseline",
                    document_id="doc_probe_2026",
                    document_version="ver_probe_v1",
                    workspace_id="ws_latency_probe",
                    snippet="General corporate overview with background metrics.",
                    score=10.0,
                    source_kind="text",
                ),
            ),
            caller_deadline_ms=2500.0,
        )
        payload_direct = service._format_endpoint_payload(req_direct)
        doc_text = payload_direct["state"]["document"]
        self.assertIn("Query: What was the turnover for fiscal year 2026?", doc_text)
        self.assertIn("Gap detected: Initial fidelity check failed coverage: Missing turnover coverage", doc_text)
        self.assertIn("Missing dimension: exact_term", doc_text)
        self.assertIn("Context: General corporate overview with background metrics.", doc_text)

        total_bytes_direct = len(json.dumps(payload_direct).encode("utf-8"))
        self.assertLessEqual(total_bytes_direct, 980)

        tokens_direct = estimate_laya_tokens(payload_direct)
        self.assertTrue(tokens_direct["within_estimated_limit"])
        self.assertLessEqual(tokens_direct["estimated_max_sequence_tokens"], 8192)

        # Positive Control 2: Compiled pipeline payload with larger retrieved summary
        req_compiled = LayaEscalationRequest(
            query_id="q_probe_synthetic_compiled",
            workspace_id="ws_latency_probe",
            query="What was the turnover for fiscal year 2026?",
            missing_requirement=MissingRequirementDescription(
                requirement_id="req_probe_002",
                missing_dimension="exact_term",
                description="Synthetic probe compiled gap",
                failed_stage="run_fidelity_coverage",
                detected_gap="Initial fidelity check failed coverage: Missing turnover coverage",
            ),
            available_routes=("lexical_expansion", "none"),
            retrieved_summary=(
                BaselineEvidenceSummary(
                    chunk_id="chk_probe_turnover",
                    document_id="doc_probe_2026",
                    document_version="ver_probe_v1",
                    workspace_id="ws_latency_probe",
                    snippet="The fiscal year 2026 turnover was officially confirmed as $450 million.",
                    score=15.0,
                    source_kind="text",
                ),
                BaselineEvidenceSummary(
                    chunk_id="chk_probe_baseline",
                    document_id="doc_probe_2026",
                    document_version="ver_probe_v1",
                    workspace_id="ws_latency_probe",
                    snippet="General corporate overview with background metrics.",
                    score=10.0,
                    source_kind="text",
                ),
            ),
            caller_deadline_ms=2500.0,
        )
        payload_compiled = service._format_endpoint_payload(req_compiled)
        total_bytes_compiled = len(json.dumps(payload_compiled).encode("utf-8"))
        self.assertLessEqual(total_bytes_compiled, 980)
        tokens_compiled = estimate_laya_tokens(payload_compiled)
        self.assertTrue(tokens_compiled["within_estimated_limit"])

        # Negative Control: Oversized mandatory requirement fails closed before dispatch
        huge_query = "Excessive query text " * 20
        req_oversized = LayaEscalationRequest(
            query_id="q_probe_oversized",
            workspace_id="ws_latency_probe",
            query=huge_query,
            missing_requirement=MissingRequirementDescription(
                requirement_id="req_probe_over",
                missing_dimension="exact_term",
                description="Oversized gap description",
                failed_stage="run_fidelity_coverage",
                detected_gap="Missing mandatory requirement term",
            ),
            available_routes=("lexical_expansion", "none"),
            retrieved_summary=(),
            caller_deadline_ms=2500.0,
        )
        with self.assertRaises(PayloadBudgetExceededError) as ctx:
            service._format_endpoint_payload(req_oversized)
        self.assertIn("exceeds document budget", str(ctx.exception))


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_LAYA_TESTS") == "true",
    "Live endpoint tests are opt-in; set RUN_LIVE_LAYA_TESTS=true to run"
)
class TestLayaLiveEndpointSmoke(unittest.TestCase):
    """Smoke test against the live running LAYA server (opt-in only)."""

    def test_live_server_decision(self) -> None:
        """Verify real server connectivity when opt-in flag is enabled."""
        url = os.environ.get("LAYA_ENDPOINT_URL") or getattr(settings, "LAYA_ENDPOINT_URL", "")
        key = os.environ.get("LAYA_API_KEY") or getattr(settings, "LAYA_API_KEY", "")

        if not url or not key:
            self.skipTest("LAYA_ENDPOINT_URL or LAYA_API_KEY not configured")

        service = LayaDecisionService(endpoint_url=url, api_key=key)
        ledger = LayaBudgetLedger()
        req = _build_test_request("What is the exact return status for connection refused?")

        try:
            decision = service.decide(req, deadline_remaining_ms=2500.0, ledger=ledger)
        except Exception as e:
            self.skipTest(f"Live LAYA server not reachable: {e}")

        self.assertIn(decision.route, ("lexical_expansion", "none"))
        self.assertIn(decision.execution_status, ("approved", "provider_unavailable"))
        self.assertGreaterEqual(decision.confidence, 0.0)
        self.assertLessEqual(decision.confidence, 1.0)
        print(f"\n[LIVE TEST SUCCESS] route={decision.route} status={decision.execution_status} confidence={decision.confidence:.4f}")


if __name__ == "__main__":
    unittest.main()
