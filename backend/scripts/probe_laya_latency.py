#!/usr/bin/env python3
"""Bounded LAYA Latency Readiness Probe.

Inspects direct endpoint and compiled LangGraph pipeline performance under
controlled synthetic inputs, bounded to at most five LAYA requests.

Features:
- Completely synthetic in-memory input (no corpus or ground truth files).
- Distinguishes first request and subsequent request timings.
- Records client roundtrip, reported server latency, unaccounted elapsed time,
  dispatch, valid response, accepted route, and deterministic fallback status.
- Separates direct endpoint results from compiled pipeline results.
- Preserves the strict 2500ms escalation phase deadline.
- Tests targeted lexical expansion as well as route none refusal.
- Uses offline doubles for synthesis, embeddings, and reranking (zero paid calls).
- Completely masks all credentials with zero key fragments printed.
- Validates and enforces a strict ceiling of at most 5 requests before dispatch.
- Defaults to offline dry-run preparation; live execution requires explicit --live opt-in.

Usage:
    # 1. Default offline preparation (zero live requests):
    cd backend && uv run python scripts/probe_laya_latency.py

    # 2. Explicit opt-in live probe against running LAYA server (at most 5 requests):
    cd backend && uv run python scripts/probe_laya_latency.py --live
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

# Ensure test environment mode so repository and models run offline in-memory
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("MODEL_PROVIDER", "test")

# Add src to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import requests
from config import settings
from db.database import CloudRepository
from schemas.contracts.laya import (
    BaselineEvidenceSummary,
    LayaEscalationDecision,
    LayaEscalationRequest,
    LexicalExpansionParams,
    MissingRequirementDescription,
    NoneExpansionParams,
)
from schemas.contracts.verification import VerificationRecord
from schemas.models import Chunk, Document
from services.langgraph_pipeline import pipeline
from services.retrieval.bm25_retriever import invalidate_chunk_cache
from services.retrieval.laya_gate import LayaBudgetLedger
from services.retrieval.laya_service import LayaDecisionService

logger = logging.getLogger("laya_probe")

MAX_ALLOWED_PROBE_REQUESTS = 5


@dataclass
class ProbeMetricRecord:
    probe_id: str
    target: str  # "direct_endpoint" or "compiled_pipeline"
    request_sequence: str  # "first_request" or "subsequent_request"
    query_type: str  # "lexical_expansion" or "none"
    client_roundtrip_ms: float
    server_latency_ms: float | None
    unaccounted_elapsed_ms: float | None
    dispatched: bool
    valid_response: bool
    proposed_route: str
    accepted_route: str
    fallback: bool
    execution_status: str
    pipeline_total_ms: float | None = None
    delivery_outcome: str | None = None
    token_usage: dict[str, int] = field(default_factory=dict)


class MockOfflineVerifier:
    """Offline test double for the generative semantic verifier."""

    def __init__(self, support_status: str = "ASSERTED") -> None:
        self.support_status = support_status
        self.verify_call_count = 0

    def is_ready(self) -> bool:
        return True

    def verify(
        self,
        text: str = "",
        context: str = "",
        citations: list[Any] | None = None,
        **kwargs: Any,
    ) -> VerificationRecord:
        self.verify_call_count += 1
        return VerificationRecord(
            verification_id="vr_probe_synthetic_001",
            workspace_id="ws_latency_probe",
            query_id="q_probe_live",
            snapshot_id="snap_probe_1",
            requirement_id="req_probe_turnover",
            evidence_id="doc_probe_2026:chk_probe_turnover",
            verifier_version="v1.0",
            verifier_type="generative",
            attempt_number=1,
            execution_time_ms=15,
            support_status=self.support_status,
            confidence_score=0.95,
            verification_rationale="Synthetic verification asserted",
            verified_at="2026-11-20T00:00:00Z",
        )


def _mask_credential(key: str) -> str:
    """Mask sensitive API key for safe display without printing any key fragments."""
    if not key:
        return "[NOT CONFIGURED]"
    return "[CONFIGURED: REDACTED]"


def _build_synthetic_request(
    query: str,
    gap: str,
    missing_dimension: str = "exact_term",
    caller_deadline_ms: float = 2500.0,
) -> LayaEscalationRequest:
    """Build a synthetic LayaEscalationRequest without touching disk or corpus."""
    return LayaEscalationRequest(
        query_id="q_probe_synthetic",
        workspace_id="ws_latency_probe",
        query=query,
        missing_requirement=MissingRequirementDescription(
            requirement_id="req_probe_001",
            missing_dimension=missing_dimension,  # type: ignore[arg-type]
            description="Synthetic probe missing requirement",
            failed_stage="run_fidelity_coverage",
            detected_gap=gap,
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
        caller_deadline_ms=caller_deadline_ms,
    )


def _setup_synthetic_repository() -> CloudRepository:
    """Seed synthetic documents in an in-memory repository without disk persistence."""
    repo = CloudRepository()
    ws_id = "ws_latency_probe"

    doc_chunk = Chunk(
        id="chk_probe_baseline",
        document_id="doc_probe_2026",
        source_format="pdf",
        text="General corporate overview with background metrics.",
        element_type="narrative_block",
        workspace_id=ws_id,
        metadata={"document_version": "ver_probe_v1"},
        embedding_fast=[0.1] * 384,
        embedding_full=[0.1] * 1024,
    )
    novel_chunk = Chunk(
        id="chk_probe_turnover",
        document_id="doc_probe_2026",
        source_format="pdf",
        text="The fiscal year 2026 turnover was officially confirmed as $450 million.",
        element_type="narrative_block",
        workspace_id=ws_id,
        metadata={"document_version": "ver_probe_v1"},
        embedding_fast=[0.1] * 384,
        embedding_full=[0.1] * 1024,
    )
    doc = Document(
        id="doc_probe_2026",
        filename="doc_probe_2026.pdf",
        source_format="pdf",
        chunks=(doc_chunk, novel_chunk),
        workspace_id=ws_id,
    )
    repo.save(doc)
    repo.add_document_to_workspace(ws_id, doc)
    invalidate_chunk_cache(ws_id)
    return repo


class BoundedNetworkSpy:
    """Monitors, measures, and strictly bounds network dispatches before execution."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.request_count = 0
        self.last_resp_data: dict[str, Any] = {}
        self.last_roundtrip_ms: float = 0.0
        self.last_server_latency_ms: float | None = None
        self._orig_post = requests.post

    def spy_post(self, *args: Any, **kwargs: Any) -> Any:
        # Validate request limit BEFORE dispatch
        if self.request_count >= MAX_ALLOWED_PROBE_REQUESTS:
            raise RuntimeError(
                f"Pre-dispatch validation failed: Request count ({self.request_count}) has reached maximum ceiling ({MAX_ALLOWED_PROBE_REQUESTS}). Dispatch blocked."
            )

        self.request_count += 1

        # Confirm credentials are kept private (never logged or exposed)
        headers = kwargs.get("headers", {})
        if "Authorization" in headers:
            auth_val = headers["Authorization"]
            assert auth_val.startswith("Bearer "), "Authorization header must use Bearer token format"

        t0 = time.perf_counter()
        if self.dry_run:
            # Simulate realistic offline response double
            time.sleep(0.04)  # 40ms simulated roundtrip
            resp = MagicMock()
            resp.status_code = 200

            # Determine response from payload questions
            req_json = kwargs.get("json", {})
            doc_str = req_json.get("state", {}).get("document", "")
            if "none" in doc_str.lower() or "unexpandable" in doc_str.lower():
                choice = "none"
                conf = 0.80
            else:
                choice = "lexical_expansion"
                conf = 0.91

            resp_data = {
                "model": "laya-rl-agent",
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": choice,
                        "confidence": 0.45,
                        "answer_confidence": conf,
                    }
                },
                "usage": {"input_tokens": 85, "output_tokens": 0},
                "server_latency_ms": 32.5,
            }
            resp.json.return_value = resp_data
            self.last_resp_data = resp_data
            self.last_roundtrip_ms = (time.perf_counter() - t0) * 1000.0
            self.last_server_latency_ms = 32.5
            return resp

        # Real network dispatch
        resp = self._orig_post(*args, **kwargs)
        self.last_roundtrip_ms = (time.perf_counter() - t0) * 1000.0
        try:
            self.last_resp_data = resp.json()
            # Extract server reported latency if provided by System One endpoint
            srv_lat = self.last_resp_data.get("server_latency_ms")
            if srv_lat is None:
                srv_lat = self.last_resp_data.get("latency_ms")
            self.last_server_latency_ms = float(srv_lat) if srv_lat is not None else None
        except Exception:
            self.last_resp_data = {}
            self.last_server_latency_ms = None
        return resp


class FidelityMockSequence:
    """Controls fidelity check behavior across probe queries."""

    def __init__(self, mode: str = "lexical") -> None:
        self.calls = 0
        self.mode = mode

    def __call__(self, q: str, contexts: Any, **kwargs: Any) -> float:
        self.calls += 1
        if self.mode == "lexical":
            if self.calls == 1:
                from services.retrieval.fidelity_check import CoverageError
                raise CoverageError("Initial fidelity check failed coverage: Missing turnover coverage")
            return 1.0
        else:  # mode == "none"
            from services.retrieval.fidelity_check import CoverageError
            raise CoverageError("Unexpandable general concept")


def run_latency_probe(endpoint_url: str, api_key: str, dry_run: bool = True) -> int:
    """Run the 5-request bounded latency probe."""
    print("=" * 80)
    print("BOUNDED LAYA LATENCY READINESS PROBE (SPEC 0008)")
    print("=" * 80)
    print(f"Target Endpoint URL:  {endpoint_url or 'https://mock.laya.local/v1/systemone (dry-run)'}")
    print(f"Authentication Key:   {_mask_credential(api_key)}")
    print(f"Execution Mode:       {'OFFLINE PREPARATION (Dry Run)' if dry_run else 'LIVE NETWORK (Opt-In)'}")
    print(f"Request Ceiling:      At most {MAX_ALLOWED_PROBE_REQUESTS} requests total (enforced before dispatch)")
    print(f"Escalation Phase SLA: 2500.0 ms hard ceiling")
    print("-" * 80)

    effective_url = endpoint_url or "https://mock.laya.local/v1/systemone"
    effective_key = api_key or "dry_run_key"

    spy = BoundedNetworkSpy(dry_run=dry_run)
    metrics: list[ProbeMetricRecord] = []
    repo = _setup_synthetic_repository()

    # Offline synthesis and reranking doubles
    mock_llm_response = {
        "answer": "The fiscal year 2026 turnover was $450 million.",
        "citations": ["chk_probe_turnover"],
        "usage": {"input_tokens": 120, "output_tokens": 18},
    }
    mock_verifier = MockOfflineVerifier(support_status="ASSERTED")

    def _mock_rerank(q: str, chunks: Any, top_k: int = 6, return_mode: bool = False) -> Any:
        c_list = list(chunks)
        return (c_list[:top_k], "mocked") if return_mode else c_list[:top_k]

    with patch("requests.post", side_effect=spy.spy_post):
        # ---------------------------------------------------------------------
        # SECTION 1: Direct Endpoint Probes (Requests 1 and 2)
        # ---------------------------------------------------------------------
        print("\n[SECTION 1] Direct Endpoint Decision Service Probes")
        service = LayaDecisionService(endpoint_url=effective_url, api_key=effective_key)

        # Request 1: Direct Endpoint (First Request)
        print("\n--> Request 1/5: Direct Endpoint (First Request, Lexical Intent)...")
        ledger_1 = LayaBudgetLedger()
        req_1 = _build_synthetic_request(
            query="What was the turnover for fiscal year 2026?",
            gap="Initial fidelity check failed coverage: Missing turnover coverage",
        )
        t_start = time.perf_counter()
        dec_1 = service.decide(req_1, deadline_remaining_ms=2500.0, ledger=ledger_1)
        total_time_1 = (time.perf_counter() - t_start) * 1000.0

        unacc_1 = (
            (spy.last_roundtrip_ms - spy.last_server_latency_ms)
            if spy.last_server_latency_ms is not None
            else None
        )
        rec_1 = ProbeMetricRecord(
            probe_id="probe_direct_req1",
            target="direct_endpoint",
            request_sequence="first_request",
            query_type="lexical_expansion",
            client_roundtrip_ms=spy.last_roundtrip_ms,
            server_latency_ms=spy.last_server_latency_ms,
            unaccounted_elapsed_ms=unacc_1,
            dispatched=service.last_dispatched,
            valid_response=service.last_valid_response,
            proposed_route=dec_1.route,
            accepted_route=dec_1.route if service.last_route_accepted else "fallback_lexical",
            fallback=service.last_fallback,
            execution_status=dec_1.execution_status,
            pipeline_total_ms=total_time_1,
            token_usage={"inputTokens": ledger_1.total_input_tokens, "outputTokens": ledger_1.total_output_tokens},
        )
        metrics.append(rec_1)
        print(f"    Status: {dec_1.execution_status} | Route: {dec_1.route} | Conf: {dec_1.confidence:.4f}")
        print(f"    Client Roundtrip: {spy.last_roundtrip_ms:.2f} ms | Server Latency: {spy.last_server_latency_ms} ms")

        # Request 2: Direct Endpoint (Subsequent Request, default client behavior)
        print("\n--> Request 2/5: Direct Endpoint (Subsequent Request, default client behavior)...")
        ledger_2 = LayaBudgetLedger()
        req_2 = _build_synthetic_request(
            query="What was the turnover for fiscal year 2026?",
            gap="Initial fidelity check failed coverage: Missing turnover coverage",
        )
        t_start = time.perf_counter()
        dec_2 = service.decide(req_2, deadline_remaining_ms=2500.0, ledger=ledger_2)
        total_time_2 = (time.perf_counter() - t_start) * 1000.0

        unacc_2 = (
            (spy.last_roundtrip_ms - spy.last_server_latency_ms)
            if spy.last_server_latency_ms is not None
            else None
        )
        rec_2 = ProbeMetricRecord(
            probe_id="probe_direct_req2",
            target="direct_endpoint",
            request_sequence="subsequent_request",
            query_type="lexical_expansion",
            client_roundtrip_ms=spy.last_roundtrip_ms,
            server_latency_ms=spy.last_server_latency_ms,
            unaccounted_elapsed_ms=unacc_2,
            dispatched=service.last_dispatched,
            valid_response=service.last_valid_response,
            proposed_route=dec_2.route,
            accepted_route=dec_2.route if service.last_route_accepted else "fallback_lexical",
            fallback=service.last_fallback,
            execution_status=dec_2.execution_status,
            pipeline_total_ms=total_time_2,
            token_usage={"inputTokens": ledger_2.total_input_tokens, "outputTokens": ledger_2.total_output_tokens},
        )
        metrics.append(rec_2)
        print(f"    Status: {dec_2.execution_status} | Route: {dec_2.route} | Conf: {dec_2.confidence:.4f}")
        print(f"    Client Roundtrip: {spy.last_roundtrip_ms:.2f} ms | Server Latency: {spy.last_server_latency_ms} ms")

        # ---------------------------------------------------------------------
        # SECTION 2: Compiled LangGraph Pipeline Probes (Requests 3, 4, and 5)
        # ---------------------------------------------------------------------
        print("\n[SECTION 2] Compiled LangGraph Pipeline End-to-End Probes")

        # Request 3: Compiled Pipeline (First Pipeline Request)
        print("\n--> Request 3/5: Compiled Pipeline (First Pipeline Request, Lexical Expansion)...")
        with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response), \
             patch("services.langgraph_pipeline._get_repo", return_value=repo), \
             patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=FidelityMockSequence(mode="lexical")):

            t_start = time.perf_counter()
            resp_3 = pipeline.run(
                query="What was the turnover for fiscal year 2026?",
                workspace_id="ws_latency_probe",
                laya_enabled=True,
                laya_endpoint_url=effective_url,
                laya_api_key=effective_key,
                verifier=mock_verifier,
                require_delivery_gate=True,
                caller_deadline_ms=2500.0,
            )
            total_time_3 = (time.perf_counter() - t_start) * 1000.0

            cands_3 = resp_3.retrieval_candidates or {}
            unacc_3 = (
                (spy.last_roundtrip_ms - spy.last_server_latency_ms)
                if spy.last_server_latency_ms is not None
                else None
            )
            rec_3 = ProbeMetricRecord(
                probe_id="probe_pipeline_req3",
                target="compiled_pipeline",
                request_sequence="first_request",
                query_type="lexical_expansion",
                client_roundtrip_ms=spy.last_roundtrip_ms,
                server_latency_ms=spy.last_server_latency_ms,
                unaccounted_elapsed_ms=unacc_3,
                dispatched=bool(cands_3.get("laya_dispatched")),
                valid_response=bool(cands_3.get("laya_provider_success")),
                proposed_route=str(cands_3.get("laya_route", "lexical_expansion")),
                accepted_route="lexical_expansion" if cands_3.get("laya_route_accepted") else "fallback",
                fallback=bool(cands_3.get("laya_fallback")),
                execution_status=str(cands_3.get("laya_status", "unknown")),
                pipeline_total_ms=total_time_3,
                delivery_outcome="DELIVERED" if "$450 million" in resp_3.answer else "REFUSAL",
            )
            metrics.append(rec_3)
            print(f"    Outcome: {rec_3.delivery_outcome} | Synthesis Calls: {resp_3.synthesis_calls}")
            print(f"    Client Roundtrip: {spy.last_roundtrip_ms:.2f} ms | Pipeline Total: {total_time_3:.2f} ms")

        # Request 4: Compiled Pipeline (Subsequent Pipeline Request, Lexical Expansion)
        print("\n--> Request 4/5: Compiled Pipeline (Subsequent Request, Lexical Expansion)...")
        with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response), \
             patch("services.langgraph_pipeline._get_repo", return_value=repo), \
             patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=FidelityMockSequence(mode="lexical")):

            t_start = time.perf_counter()
            resp_4 = pipeline.run(
                query="What was the turnover for fiscal year 2026?",
                workspace_id="ws_latency_probe",
                laya_enabled=True,
                laya_endpoint_url=effective_url,
                laya_api_key=effective_key,
                verifier=mock_verifier,
                require_delivery_gate=True,
                caller_deadline_ms=2500.0,
            )
            total_time_4 = (time.perf_counter() - t_start) * 1000.0

            cands_4 = resp_4.retrieval_candidates or {}
            unacc_4 = (
                (spy.last_roundtrip_ms - spy.last_server_latency_ms)
                if spy.last_server_latency_ms is not None
                else None
            )
            rec_4 = ProbeMetricRecord(
                probe_id="probe_pipeline_req4",
                target="compiled_pipeline",
                request_sequence="subsequent_request",
                query_type="lexical_expansion",
                client_roundtrip_ms=spy.last_roundtrip_ms,
                server_latency_ms=spy.last_server_latency_ms,
                unaccounted_elapsed_ms=unacc_4,
                dispatched=bool(cands_4.get("laya_dispatched")),
                valid_response=bool(cands_4.get("laya_provider_success")),
                proposed_route=str(cands_4.get("laya_route", "lexical_expansion")),
                accepted_route="lexical_expansion" if cands_4.get("laya_route_accepted") else "fallback",
                fallback=bool(cands_4.get("laya_fallback")),
                execution_status=str(cands_4.get("laya_status", "unknown")),
                pipeline_total_ms=total_time_4,
                delivery_outcome="DELIVERED" if "$450 million" in resp_4.answer else "REFUSAL",
            )
            metrics.append(rec_4)
            print(f"    Outcome: {rec_4.delivery_outcome} | Synthesis Calls: {resp_4.synthesis_calls}")
            print(f"    Client Roundtrip: {spy.last_roundtrip_ms:.2f} ms | Pipeline Total: {total_time_4:.2f} ms")

        # Request 5: Compiled Pipeline (Subsequent Pipeline Request, Route None Refusal)
        print("\n--> Request 5/5: Compiled Pipeline (Subsequent Request, Route None Refusal)...")
        with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response), \
             patch("services.langgraph_pipeline._get_repo", return_value=repo), \
             patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank), \
             patch("services.langgraph_pipeline.check_fidelity", side_effect=FidelityMockSequence(mode="none")):

            t_start = time.perf_counter()
            resp_5 = pipeline.run(
                query="Unexpandable general concept with no evidence in existence",
                workspace_id="ws_latency_probe",
                laya_enabled=True,
                laya_endpoint_url=effective_url,
                laya_api_key=effective_key,
                caller_deadline_ms=2500.0,
            )
            total_time_5 = (time.perf_counter() - t_start) * 1000.0

            cands_5 = resp_5.retrieval_candidates or {}
            unacc_5 = (
                (spy.last_roundtrip_ms - spy.last_server_latency_ms)
                if spy.last_server_latency_ms is not None
                else None
            )
            rec_5 = ProbeMetricRecord(
                probe_id="probe_pipeline_req5",
                target="compiled_pipeline",
                request_sequence="subsequent_request",
                query_type="none",
                client_roundtrip_ms=spy.last_roundtrip_ms,
                server_latency_ms=spy.last_server_latency_ms,
                unaccounted_elapsed_ms=unacc_5,
                dispatched=bool(cands_5.get("laya_dispatched")),
                valid_response=bool(cands_5.get("laya_provider_success")),
                proposed_route=str(cands_5.get("laya_route", "none")),
                accepted_route="none" if cands_5.get("laya_route_accepted") else "fallback",
                fallback=bool(cands_5.get("laya_fallback")),
                execution_status=str(cands_5.get("laya_status", "unknown")),
                pipeline_total_ms=total_time_5,
                delivery_outcome="REFUSAL" if resp_5.answer == "NOT_FOUND" else "DELIVERED",
            )
            metrics.append(rec_5)
            print(f"    Outcome: {rec_5.delivery_outcome} | Synthesis Calls: {resp_5.synthesis_calls}")
            print(f"    Client Roundtrip: {spy.last_roundtrip_ms:.2f} ms | Pipeline Total: {total_time_5:.2f} ms")

    # -------------------------------------------------------------------------
    # PRINT STRUCTURED READINESS REPORT
    # -------------------------------------------------------------------------
    direct_records = [m for m in metrics if m.target == "direct_endpoint"]
    pipeline_records = [m for m in metrics if m.target == "compiled_pipeline"]

    print("\n" + "=" * 90)
    print("PROBE RESULTS: DIRECT ENDPOINT (SECTION 1)")
    print("=" * 90)
    h_direct = f"{'Req':<4} {'Sequence':<20} {'Query Intent':<18} {'Client(ms)':<12} {'Server(ms)':<12} {'Unacc(ms)':<12} {'Status':<12}"
    print(h_direct)
    print("-" * 90)
    for idx, m in enumerate(direct_records, 1):
        srv_str = f"{m.server_latency_ms:.1f}" if m.server_latency_ms is not None else "N/A"
        unacc_str = f"{m.unaccounted_elapsed_ms:.1f}" if m.unaccounted_elapsed_ms is not None else "N/A"
        print(
            f"{idx:<4} {m.request_sequence:<20} {m.query_type:<18} "
            f"{m.client_roundtrip_ms:<12.1f} {srv_str:<12} {unacc_str:<12} "
            f"{m.execution_status:<12}"
        )

    print("\n" + "=" * 90)
    print("PROBE RESULTS: COMPILED PIPELINE (SECTION 2)")
    print("=" * 90)
    h_pipe = f"{'Req':<4} {'Sequence':<20} {'Query Intent':<18} {'Pipeline(ms)':<14} {'LayaCall(ms)':<14} {'Status':<12} {'Outcome':<10}"
    print(h_pipe)
    print("-" * 90)
    for idx, m in enumerate(pipeline_records, 3):
        pipe_str = f"{m.pipeline_total_ms:.1f}" if m.pipeline_total_ms is not None else "N/A"
        print(
            f"{idx:<4} {m.request_sequence:<20} {m.query_type:<18} "
            f"{pipe_str:<14} {m.client_roundtrip_ms:<14.1f} "
            f"{m.execution_status:<12} {m.delivery_outcome or 'N/A':<10}"
        )

    print("\n" + "-" * 90)
    print("[ANALYSIS: OBSERVABLE TIMING BREAKDOWN]")

    first_req_times = [m.client_roundtrip_ms for m in metrics if m.request_sequence == "first_request"]
    subsequent_req_times = [m.client_roundtrip_ms for m in metrics if m.request_sequence == "subsequent_request"]
    avg_first_ms = sum(first_req_times) / len(first_req_times) if first_req_times else 0.0
    avg_subsequent_ms = sum(subsequent_req_times) / len(subsequent_req_times) if subsequent_req_times else 0.0

    print(f"1. First Request Average Client Roundtrip:        {avg_first_ms:.1f} ms")
    print(f"2. Subsequent Requests Average Client Roundtrip:   {avg_subsequent_ms:.1f} ms")

    server_latencies = [m.server_latency_ms for m in metrics if m.server_latency_ms is not None]
    if server_latencies:
        avg_server_ms = sum(server_latencies) / len(server_latencies)
        print(f"3. Average Reported Server Model Latency:         {avg_server_ms:.1f} ms")
        avg_unacc_ms = avg_subsequent_ms - avg_server_ms
        print(f"4. Average Subsequent Unaccounted Elapsed Time:   {avg_unacc_ms:.1f} ms")

    print("\n[METHODOLOGY & LIMITATION NOTES]")
    print("* Connection Behavior: Production LayaDecisionService uses default requests.post per call.")
    print("  TCP connection reuse is not verified across separate invocations; production behavior is left unmodified.")
    print("* Timing Attribution: Unaccounted elapsed time reflects client roundtrip minus reported server execution.")
    print("  It represents the complete path outside server model processing and is not attributed specifically to")
    print("  Cloudflare, TLS, or DNS without supporting network-level instrumentation.")
    print("* Statistical Reliability: Five requests provide boundary validation and operational sanity checks.")
    print("  Five samples do NOT provide statistical reliability or SLA percentiles (p95/p99).")

    if dry_run:
        print("\n" + "=" * 90)
        print(">> VERDICT: HARNESS PASS; LIVE LATENCY READINESS UNKNOWN")
        print("=" * 90)
        print("Harness execution and contract logic verified using offline test doubles.")
        print("Real network readiness cannot be derived from mocked timings.")
        print("To measure live network latency, run with explicit opt-in: --live")
        return 0

    # Live Evaluation Pass / Fail Criteria
    sla_threshold_ms = 2500.0
    subsequent_sla_pass = all(t <= sla_threshold_ms for t in subsequent_req_times)
    routes_valid = all(m.valid_response for m in metrics)
    lexical_verified = any(m.delivery_outcome == "DELIVERED" for m in pipeline_records)
    none_refused = any(m.delivery_outcome == "REFUSAL" and m.query_type == "none" for m in pipeline_records)

    print("\n" + "=" * 90)
    if subsequent_sla_pass and routes_valid and lexical_verified and none_refused:
        print(">> VERDICT: PASS (SAMPLE OBSERVED)")
        print(f"   Subsequent roundtrips ({avg_subsequent_ms:.1f} ms) completed within the 2500ms phase SLA.")
        print("   Both lexical expansion delivery and route none refusal verified.")
        return 0
    elif not subsequent_sla_pass:
        print(">> VERDICT: FAIL (LATENCY BLOCKER)")
        max_subsequent = max(subsequent_req_times) if subsequent_req_times else 0.0
        print(f"   Subsequent roundtrips peaked at {max_subsequent:.1f} ms, exceeding the 2500.0 ms escalation phase limit.")
        print("   Escalation will trigger caller phase timeouts and deterministic fallback.")
        return 1
    else:
        print(">> VERDICT: FAIL (FUNCTIONAL DEFECT)")
        print(f"   Routes valid: {routes_valid}, Lexical delivery: {lexical_verified}, None refusal: {none_refused}")
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded LAYA Latency Readiness Probe")
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Explicit opt-in to execute live HTTP requests against configured LAYA server (defaults to offline preparation)",
    )
    parser.add_argument(
        "--url",
        type=str,
        default="",
        help="Optional override for LAYA_ENDPOINT_URL",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default="",
        help="Optional override for LAYA_API_KEY",
    )
    args = parser.parse_args()

    # Read from environment or explicit flags
    url = args.url or os.environ.get("LAYA_ENDPOINT_URL") or getattr(settings, "LAYA_ENDPOINT_URL", "")
    key = args.api_key or os.environ.get("LAYA_API_KEY") or getattr(settings, "LAYA_API_KEY", "")

    dry_run = not args.live

    if not dry_run:
        if not url:
            print("[ERROR] LAYA_ENDPOINT_URL is not configured in environment or CLI flag.", file=sys.stderr)
            return 1
        if not key:
            print("[ERROR] LAYA_API_KEY is not configured in environment or CLI flag.", file=sys.stderr)
            return 1

    return run_latency_probe(endpoint_url=url, api_key=key, dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main())
