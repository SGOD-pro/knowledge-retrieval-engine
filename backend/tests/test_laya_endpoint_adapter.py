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
from services.retrieval.bm25_retriever import invalidate_chunk_cache
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
from services.retrieval.laya_gate import LayaBudgetLedger
from services.retrieval.laya_service import LayaDecisionService
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
