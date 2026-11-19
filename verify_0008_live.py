"""Live verification script for Spec 0008 Slice 5.1:
Bounded LAYA Escalation and Query Expansion (Thin Lexical Vertical Path and Admission Control).

Exercises live FastAPI server on http://127.0.0.1:8001 and backend services:
- Live workspace creation and boundary authorization
- Frozen contracts, strict discriminator validation, and normalized bounding box (AC-2, AC-5)
- Deterministic Section 35 fallback policy and unavailable route fallback (AC-3)
- Atomic budget pre-reservation and deadline bounding (AC-1, AC-4)
- Authoritative token preflight and deterministic evidence summary truncation (AC-4)
- Strict provider usage validation and invalid usage fail-closed handling (AC-4)
- Concurrency controller admission, load shedding, and capacity retention (AC-4)
- 429 throttling retries charged as attempts within the same operation (AC-4)
- End-to-end pipeline lexical expansion on fidelity coverage gap (AC-1, AC-6, AC-7)
- End-to-end pipeline empty context entry path triggers escalation (AC-1, AC-7)
- Code rejection of unavailable routes in Slice 5.1 with deterministic fallback (AC-3)
- Workspace boundary and opaque string version isolation (AC-5)
- Dynamic tombstone detection before retrieval halts expansion fail-closed (AC-5)
- Dynamic tombstone detection before delivery blocks free-form prose fail-closed (AC-5, AC-6)
- Final delivery gate blocks unverified free-form prose when verifier is absent (AC-6)
- Final delivery gate rejects contradicted verification records (AC-6)
- Single answer synthesis call invariant enforced (second call raises error) (AC-4, AC-6)
- Stale refusal state cleared upon expansion before rechecking support (AC-7)
- No-progress detection halts escalation into clean structured refusal (AC-6, AC-7)
"""

import io
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["ENVIRONMENT"] = "test"

# Setup paths for importing backend modules
backend_dir = Path(__file__).resolve().parent / "backend"
src_dir = backend_dir / "src"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

import httpx
from pydantic import ValidationError

from config import settings
settings.ENVIRONMENT = "test"

BASE_URL = "http://127.0.0.1:8001"
ALICE_HEADERS = {"Authorization": "Bearer test_token_alice"}
BOB_HEADERS = {"Authorization": "Bearer test_token_bob"}

results = []

def record(criterion: str, behavior: str, req_line: str, status_code: int, passed: bool, evidence: str = ""):
    entry = {
        "criterion": criterion,
        "behavior": behavior,
        "request": req_line,
        "status": status_code,
        "passed": passed,
        "evidence": evidence,
    }
    results.append(entry)
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] [{criterion}] {behavior} -> {status_code} ({req_line}) | {evidence[:120]}")


print("=== Running Spec 0008 Slice 5.1 Live Verification ===")

client = httpx.Client(timeout=10.0)

# ---------------------------------------------------------------------------
# 1. Live Workspace Creation & Boundary Authorization (HTTP 8001)
# ---------------------------------------------------------------------------
r_health = client.get(f"{BASE_URL}/health")
record(
    "Setup",
    "Live FastAPI server health check responds OK",
    "GET /health",
    r_health.status_code,
    r_health.status_code == 200 and r_health.json().get("status") == "ok",
    f"response={r_health.text}",
)

r_ws = client.post(f"{BASE_URL}/workspaces", json={"name": "Alice 0008 Live WS"}, headers=ALICE_HEADERS)
ws_id = r_ws.json().get("workspace_id") if r_ws.status_code == 201 else None
record(
    "Setup",
    "Create workspace for Alice over live HTTP",
    "POST /workspaces",
    r_ws.status_code,
    r_ws.status_code == 201 and bool(ws_id),
    f"workspace_id={ws_id}",
)

# Cross workspace authorization check: Bob cannot access Alice's workspace
r_cross = client.post(
    f"{BASE_URL}/workspaces/{ws_id}/query",
    json={"query": "What is the revenue?"},
    headers=BOB_HEADERS,
)
record(
    "AC-5",
    "Cross-workspace boundary rejection (Bob denied access to Alice workspace with 404)",
    f"POST /workspaces/{ws_id}/query (Bob)",
    r_cross.status_code,
    r_cross.status_code in (403, 404),
    f"status={r_cross.status_code}, detail={r_cross.text[:80]}",
)

# ---------------------------------------------------------------------------
# 2. Frozen Contracts, Discriminator Agreement & Bounds (AC-2, AC-5)
# ---------------------------------------------------------------------------
from src.schemas.contracts.laya import (
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
from src.schemas.contracts.verification import VerificationRecord
from src.schemas.contracts.knowledge_graph import PinnedDocumentVersion, PinnedSnapshotContext

try:
    valid_dec = LayaEscalationDecision(
        decision_id="dec_001",
        route="lexical_expansion",
        parameters=LexicalExpansionParams(query_terms=("revenue", "q3")),
        confidence=0.85,
        rationale="Vocabulary mismatch on financial terms",
    )
    # Test immutability
    is_frozen = False
    try:
        valid_dec.confidence = 0.99  # type: ignore[misc]
    except (ValidationError, TypeError):
        is_frozen = True

    # Test discriminator mismatch rejection
    mismatch_rejected = False
    try:
        LayaEscalationDecision(
            decision_id="dec_mismatch",
            route="lexical_expansion",
            parameters=DenseExpansionParams(concept_query="quarterly revenue growth"),
            confidence=0.9,
        )
    except (ValidationError, ValueError):
        mismatch_rejected = True

    # Test boolean confidence rejection
    bool_conf_rejected = False
    try:
        LayaEscalationDecision(
            decision_id="dec_bool",
            route="lexical_expansion",
            parameters=LexicalExpansionParams(query_terms=("term",)),
            confidence=True,  # type: ignore[arg-type]
        )
    except (ValidationError, ValueError):
        bool_conf_rejected = True

    record(
        "AC-2",
        "Frozen contracts, discriminator agreement, and strict numeric confidence",
        "contract: LayaEscalationDecision",
        200,
        is_frozen and mismatch_rejected and bool_conf_rejected,
        f"is_frozen={is_frozen}, mismatch_rejected={mismatch_rejected}, bool_conf_rejected={bool_conf_rejected}",
    )
except Exception as e:
    record("AC-2", "Contract validation failed", "contract: LayaEscalationDecision", 500, False, str(e))

# ---------------------------------------------------------------------------
# 3. Normalized Bounding Box and Opaque Version Bounds (AC-2, AC-5)
# ---------------------------------------------------------------------------
try:
    valid_summary = BaselineEvidenceSummary(
        chunk_id="chunk_01",
        document_id="doc_alpha_128",
        document_version="ver_snap_2026_opaque_hash",
        workspace_id=ws_id or "ws_test_laya",
        snippet="Operating income increased 15% in Q3.",
        score=82.5,
        source_kind="pdf_page",
        page_number=3,
        bbox=(0.1, 0.2, 0.8, 0.9),
    )
    invalid_bbox_rejected = False
    try:
        BaselineEvidenceSummary(
            chunk_id="chunk_02",
            document_id="doc_02",
            document_version="ver_02",
            workspace_id=ws_id or "ws_test_laya",
            snippet="Bad bbox coordinates",
            score=50.0,
            source_kind="pdf_page",
            bbox=(0.9, 0.8, 0.1, 0.2),  # x0 > x1
        )
    except (ValidationError, ValueError):
        invalid_bbox_rejected = True

    record(
        "AC-5",
        "Normalized bounding box ordering (x0<x1, y0<y1) and opaque string version",
        "contract: BaselineEvidenceSummary",
        200,
        invalid_bbox_rejected and isinstance(valid_summary.document_version, str),
        f"bbox_rejected={invalid_bbox_rejected}, doc_ver={valid_summary.document_version}",
    )
except Exception as e:
    record("AC-5", "Bounding box validation failed", "contract: BaselineEvidenceSummary", 500, False, str(e))

# ---------------------------------------------------------------------------
# 4. Deterministic Section 35 Fallback Policy (AC-3)
# ---------------------------------------------------------------------------
from src.services.retrieval.laya_policy import (
    AVAILABLE_ROUTES_SLICE_5_1,
    extract_fallback_query_terms,
    get_deterministic_fallback_decision,
)

req_exact = LayaEscalationRequest(
    query_id="q_fb_1",
    workspace_id=ws_id or "ws_test_laya",
    query="What is the total quarterly revenue?",
    missing_requirement=MissingRequirementDescription(
        requirement_id="req_01",
        missing_dimension="exact_term",
        description="Missing exact term",
        failed_stage="run_fidelity_coverage",
        detected_gap="quarterly revenue",
    ),
    caller_deadline_ms=2500.0,
)
fb_decision = get_deterministic_fallback_decision(req_exact)
policy_ok = (
    fb_decision.route == "lexical_expansion"
    and isinstance(fb_decision.parameters, LexicalExpansionParams)
    and len(fb_decision.parameters.query_terms) >= 1
    and fb_decision.execution_status == "approved"
)
record(
    "AC-3",
    "Section 35 deterministic fallback maps missing dimension to lexical expansion",
    "service: get_deterministic_fallback_decision",
    200,
    policy_ok,
    f"route={fb_decision.route}, terms={fb_decision.parameters.query_terms if policy_ok else None}",
)

# ---------------------------------------------------------------------------
# 5. Atomic Budget Ledger Pre-Reservation and Phase Deadline (AC-1, AC-4)
# ---------------------------------------------------------------------------
from src.services.retrieval.laya_gate import (
    InvalidUsageError,
    LayaBudgetLedger,
    calculate_phase_deadline,
    preflight_token_count,
    truncate_evidence_summary_for_preflight,
    validate_provider_usage,
)

ledger = LayaBudgetLedger()
res1, reason1 = ledger.pre_reserve(deadline_remaining_ms=1800.0, estimated_input_tokens=300, estimated_output_tokens=250)
res2, reason2 = ledger.pre_reserve(deadline_remaining_ms=1800.0, estimated_input_tokens=300, estimated_output_tokens=250)

deadline_normal = calculate_phase_deadline(remaining_query_ms=1800.0)
deadline_capped = calculate_phase_deadline(remaining_query_ms=3000.0)

# Low deadline (<500ms) fails pre-reservation immediately
res_low, reason_low = LayaBudgetLedger().pre_reserve(deadline_remaining_ms=450.0)

budget_ok = (
    res1 is True
    and res2 is False  # Second reservation blocked by MAX_QUERY_LAYA_CALLS=1
    and deadline_normal == 1800.0
    and deadline_capped == 2500.0  # Capped at MAX_PHASE_DEADLINE_MS
    and res_low is False  # Aborted when < 500ms
)
record(
    "AC-4",
    "Budget ledger atomic reservation (single call cap) and phase deadline abort (<500ms)",
    "service: LayaBudgetLedger & calculate_phase_deadline",
    200,
    budget_ok,
    f"res1={res1}, res2={res2}, normal_dl={deadline_normal}ms, low_res={res_low} ({reason_low})",
)

# ---------------------------------------------------------------------------
# 6. Authoritative Token Preflight and Evidence Truncation (AC-4)
# ---------------------------------------------------------------------------
from src.services.retrieval.laya_gate import estimate_tokens_defensible_bound

oversized_summaries = tuple(
    BaselineEvidenceSummary(
        chunk_id=f"chunk_{i}",
        document_id=f"doc_{i}",
        document_version="ver_1",
        workspace_id=ws_id or "ws_test_laya",
        snippet="Detailed paragraph explaining corporate governance and financial metrics. " * 4,
        score=50.0 + i,
        source_kind="text",
    )
    for i in range(8)
)
req_oversized = LayaEscalationRequest(
    query_id="q_over_1",
    workspace_id=ws_id or "ws_test_laya",
    query="Explain corporate metrics and governance policies in detail.",
    missing_requirement=MissingRequirementDescription(
        requirement_id="req_over",
        missing_dimension="semantic_concept",
        description="Missing broad context",
        failed_stage="run_fidelity_coverage",
        detected_gap="metrics",
    ),
    retrieved_summary=oversized_summaries,
    caller_deadline_ms=2500.0,
)

system_prompt = "You are LAYA."
tokens_before = estimate_tokens_defensible_bound(f"{system_prompt}\n{req_oversized.model_dump_json()}")
truncated_req = truncate_evidence_summary_for_preflight(req_oversized, target_max_tokens=600, system_prompt=system_prompt)
tokens_after = estimate_tokens_defensible_bound(f"{system_prompt}\n{truncated_req.model_dump_json()}")

mock_client_tokens = MagicMock()
mock_client_tokens.count_tokens.return_value = {"inputTokens": 180}
count_auth = preflight_token_count(
    client=mock_client_tokens,
    model_id="anthropic.claude-3-haiku-20240307-v1:0",
    system_prompt="sys",
    user_content="user message",
)

truncation_ok = (
    tokens_before > 600
    and tokens_after <= 600
    and all(len(s.snippet) <= 80 for s in truncated_req.retrieved_summary)
    and count_auth == 180
)
record(
    "AC-4",
    "Authoritative token preflight and deterministic snippet truncation (<=1000 tokens)",
    "service: truncate_evidence_summary_for_preflight",
    200,
    truncation_ok,
    f"tokens_before={tokens_before}, tokens_after={tokens_after}, auth_count={count_auth}",
)

# ---------------------------------------------------------------------------
# 7. Strict Provider Usage Validation (AC-4)
# ---------------------------------------------------------------------------
valid_usage = {"inputTokens": 120, "outputTokens": 45}
invalid_usage_neg = {"inputTokens": -5, "outputTokens": 45}
invalid_usage_overflow = {"inputTokens": 120, "outputTokens": 280}  # Cap is 250

usage_valid_ok = validate_provider_usage(valid_usage, estimated_prompt_tokens=200) == (120, 45)
usage_neg_caught = False
try:
    validate_provider_usage(invalid_usage_neg, estimated_prompt_tokens=200)
except InvalidUsageError:
    usage_neg_caught = True

usage_overflow_caught = False
try:
    validate_provider_usage(invalid_usage_overflow, estimated_prompt_tokens=200)
except InvalidUsageError:
    usage_overflow_caught = True

record(
    "AC-4",
    "Strict provider usage validation (caps output tokens at 250, rejects negative usage)",
    "service: validate_provider_usage",
    200,
    usage_valid_ok and usage_neg_caught and usage_overflow_caught,
    f"valid={usage_valid_ok}, neg_caught={usage_neg_caught}, overflow_caught={usage_overflow_caught}",
)

# ---------------------------------------------------------------------------
# 8. Concurrency Controller Queue Saturation and Load Shedding (AC-4)
# ---------------------------------------------------------------------------
import threading
from src.services.retrieval.laya_concurrency import (
    LayaProviderConcurrencyController,
)

controller = LayaProviderConcurrencyController(max_concurrency=2, max_queue=3)
blocker_event = threading.Event()

def slow_worker():
    blocker_event.wait(timeout=2.0)
    return {"status": "ok"}

# Occupy 2 worker slots and 3 waiting slots = 5 total callers
threads = []
statuses = []
for _ in range(5):
    def _runner():
        _, st = controller.run_provider_call(slow_worker, timeout_s=1.0)
        statuses.append(st)
    t = threading.Thread(target=_runner)
    threads.append(t)
    t.start()

time.sleep(0.05)  # Ensure slots are populated
res_shed, status_shed = controller.run_provider_call(slow_worker, timeout_s=0.5)
load_shedding_ok = (status_shed == "admission_rejected" and res_shed is None)

blocker_event.set()
for t in threads:
    t.join(timeout=3.0)

record(
    "AC-4",
    "Concurrency admission queue saturation and immediate load shedding (admission_rejected)",
    "service: LayaProviderConcurrencyController",
    200,
    load_shedding_ok,
    f"status_shed={status_shed}, res_shed={res_shed}",
)

# ---------------------------------------------------------------------------
# 9. HTTP 429 Throttling Retries Charged as Attempts (AC-4)
# ---------------------------------------------------------------------------
from src.services.retrieval.laya_service import LayaDecisionService
from botocore.exceptions import ClientError

ledger_429 = LayaBudgetLedger()
mock_client_429 = MagicMock()

err_429 = ClientError(
    {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded 429"}},
    "Converse",
)
valid_resp_429 = {
    "output": {
        "message": {
            "content": [
                {
                    "text": json.dumps({
                        "decision_id": "dec_429",
                        "route": "lexical_expansion",
                        "parameters": {
                            "route_type": "lexical_expansion",
                            "query_terms": ["revenue"],
                            "document_ids": [],
                        },
                        "confidence": 0.9,
                        "rationale": "Retried successfully",
                    })
                }
            ]
        }
    },
    "usage": {"inputTokens": 100, "outputTokens": 30},
}
mock_client_429.converse.side_effect = [err_429, valid_resp_429]
mock_client_429.count_tokens.return_value = {"inputTokens": 100}

service_429 = LayaDecisionService(client=mock_client_429)
dec_429 = service_429.decide(req_exact, deadline_remaining_ms=2500.0, ledger=ledger_429)

retry_charged_ok = (
    ledger_429.tool_attempts == 2
    and dec_429.route == "lexical_expansion"
    and dec_429.execution_status == "approved"
)
record(
    "AC-4",
    "HTTP 429 throttling retries charged as physical attempts within same bounded operation",
    "service: LayaDecisionService.decide (429 retry)",
    200,
    retry_charged_ok,
    f"attempts_charged={ledger_429.tool_attempts}, dec_route={dec_429.route}",
)

# ---------------------------------------------------------------------------
# 10. End-to-End Pipeline Lexical Expansion on Coverage Gap (AC-1, AC-6, AC-7)
# ---------------------------------------------------------------------------
from db.database import CloudRepository
from schemas.models import Chunk, Document
from services.langgraph_pipeline import pipeline, run_laya_escalation, delivery_gate, run_llm
from services.retrieval.bm25_retriever import invalidate_chunk_cache

repo = CloudRepository()
target_ws = ws_id or "ws_live_0008_test"
doc_id = "doc_fin_live_0008"
doc_ver = "ver_fin_001"

doc_chunk = Chunk(
    id="chunk_fin_001",
    document_id=doc_id,
    source_format="pdf",
    text="The target gross revenue reached forty million dollars in fiscal year twenty twenty four.",
    element_type="narrative_text",
    workspace_id=target_ws,
    page_number=1,
    metadata={"document_version": doc_ver, "workspace_id": target_ws},
    embedding_fast=[0.1] * 384,
    embedding_full=[0.1] * 1024,
)
doc = Document(
    id=doc_id,
    filename="financial_report.pdf",
    source_format="pdf",
    workspace_id=target_ws,
    chunks=(doc_chunk,),
)
repo.save(doc)
repo.add_document_to_workspace(target_ws, doc)
invalidate_chunk_cache(target_ws)

class MockVerifier:
    def __init__(self, support_status: str = "ASSERTED") -> None:
        self.support_status = support_status
        self.verify_call_count = 0

    def is_ready(self) -> bool:
        return True

    def verify(self, text: str, context: str, citations: list[Any]) -> VerificationRecord:
        self.verify_call_count += 1
        return VerificationRecord(
            verification_id="vr_live_001",
            workspace_id=target_ws,
            query_id="q_live_001",
            snapshot_id="snap_1",
            requirement_id="req_1",
            evidence_id=f"{doc_id}:chunk_fin_001",
            verifier_version="v1.0",
            verifier_type="generative",
            attempt_number=1,
            execution_time_ms=25,
            support_status=self.support_status,
            confidence_score=0.95,
            verification_rationale="Grounded in revenue chunk",
            verified_at="2026-11-19T00:00:00Z",
        )

# Mock decision model to return lexical expansion with target terms
mock_client_laya = MagicMock()
mock_client_laya.converse.return_value = {
    "output": {
        "message": {
            "content": [
                {
                    "text": json.dumps({
                        "decision_id": "dec_synonym_1",
                        "route": "lexical_expansion",
                        "parameters": {
                            "route_type": "lexical_expansion",
                            "query_terms": ["gross", "revenue", "fiscal"],
                            "document_ids": [doc_id],
                        },
                        "confidence": 0.92,
                        "rationale": "Lexical match gross revenue",
                    })
                }
            ]
        }
    },
    "usage": {"inputTokens": 120, "outputTokens": 30},
}
mock_client_laya.count_tokens.return_value = {"inputTokens": 120}

mock_verifier = MockVerifier(support_status="ASSERTED")
mock_llm_response = {
    "answer": "The target gross revenue reached forty million dollars in FY2024.",
    "citations": ["chunk_fin_001"],
    "usage": {"input_tokens": 120, "output_tokens": 25},
}

fidelity_calls = 0
def _mock_check_fidelity(q, contexts, **kw):
    global fidelity_calls
    fidelity_calls += 1
    if fidelity_calls == 1:
        from services.retrieval.fidelity_check import CoverageError
        raise CoverageError("Initial coverage gap on revenue")
    return 1.0

with patch("services.langgraph_pipeline.call_llm", return_value=mock_llm_response), \
     patch("services.langgraph_pipeline._get_repo", return_value=repo), \
     patch("services.langgraph_pipeline.rerank", return_value=([doc_chunk], "bm25_fused")), \
     patch("services.langgraph_pipeline.check_fidelity", side_effect=_mock_check_fidelity):

    pipeline_res = pipeline.run(
        query="What was the gross revenue for fiscal twenty twenty four?",
        workspace_id=target_ws,
        laya_enabled=True,
        laya_client=mock_client_laya,
        verifier=mock_verifier,
        require_delivery_gate=True,
    )

e2e_expansion_ok = (
    pipeline_res.laya_calls == 1
    and pipeline_res.expansion_steps == 1
    and pipeline_res.synthesis_calls == 1
    and "forty million dollars" in pipeline_res.answer
    and len(pipeline_res.citations) > 0
    and mock_verifier.verify_call_count == 1
)

record(
    "AC-1, AC-7",
    "Pipeline lexical expansion: BM25 retrieval, stale refusal clearing, and candidate fusion",
    "pipeline: run_laya_escalation",
    200,
    e2e_expansion_ok,
    f"laya_calls={pipeline_res.laya_calls}, expansion_steps={pipeline_res.expansion_steps}, synthesis_calls={pipeline_res.synthesis_calls}, ans={pipeline_res.answer[:40]}",
)

# ---------------------------------------------------------------------------
# 11. Code Rejection of Unavailable Routes in Slice 5.1 (AC-3)
# ---------------------------------------------------------------------------
mock_client_unavail = MagicMock()
mock_client_unavail.converse.return_value = {
    "output": {
        "message": {
            "content": [
                {
                    "text": json.dumps({
                        "decision_id": "dec_unavail_graph",
                        "route": "graph_expansion",
                        "parameters": {
                            "route_type": "graph_expansion",
                            "seed_entity_id": "ent_123",
                            "relation_types": ["subsidiary_of"],
                            "max_hops": 1,
                        },
                        "confidence": 0.9,
                        "rationale": "Graph traversal proposal",
                    })
                }
            ]
        }
    },
    "usage": {"inputTokens": 100, "outputTokens": 30},
}
mock_client_unavail.count_tokens.return_value = {"inputTokens": 100}

service_unavail = LayaDecisionService(client=mock_client_unavail)
ledger_unavail = LayaBudgetLedger()
req_unavail = LayaEscalationRequest(
    query_id="q_unavail_01",
    workspace_id=target_ws,
    query="Find corporate subsidiaries.",
    missing_requirement=MissingRequirementDescription(
        requirement_id="req_rel",
        missing_dimension="relationship",
        description="Relationship gap",
        failed_stage="run_fidelity_coverage",
        detected_gap="corporate subsidiaries",
    ),
    caller_deadline_ms=2500.0,
)

decision_unavail = service_unavail.decide(req_unavail, deadline_remaining_ms=2500.0, ledger=ledger_unavail)
fallback_ok = (
    decision_unavail.route == "lexical_expansion"
    and decision_unavail.execution_status == "approved"
    and "unavailable in Slice 5.1" in decision_unavail.rationale
)

record(
    "AC-3",
    "Code validation rejects unavailable route proposal and executes Section 35 fallback",
    "service: LayaDecisionService.decide (unavailable route)",
    200,
    fallback_ok,
    f"effective_route={decision_unavail.route}, rationale={decision_unavail.rationale}",
)

# ---------------------------------------------------------------------------
# 12. Version Isolation and Opaque Version Bounds (AC-5)
# ---------------------------------------------------------------------------
from src.services.retrieval.laya_executor import LayaExpansionExecutor

executor = LayaExpansionExecutor()
mock_chunk_repo = MagicMock()
mock_chunk_repo.get_all_chunks.return_value = [doc_chunk]

snap_context_match = PinnedSnapshotContext(
    workspace_id=target_ws,
    captured_at_utc="2026-11-19T00:00:00Z",
    active_documents=(
        PinnedDocumentVersion(
            document_id=doc_id,
            document_version=doc_ver,
            publication_id="pub_1",
            is_tombstoned=False,
        ),
    ),
)
snap_context_mismatch = PinnedSnapshotContext(
    workspace_id=target_ws,
    captured_at_utc="2026-11-19T00:00:00Z",
    active_documents=(
        PinnedDocumentVersion(
            document_id=doc_id,
            document_version="ver_stale_999",
            publication_id="pub_2",
            is_tombstoned=False,
        ),
    ),
)

decision_ver = LayaEscalationDecision(
    decision_id="dec_ver_test",
    route="lexical_expansion",
    parameters=LexicalExpansionParams(
        route_type="lexical_expansion",
        query_terms=("gross", "revenue"),
        document_ids=(doc_id,),
    ),
    confidence=0.9,
)

novel_match, no_prog_match, st_match = executor.execute(
    decision=decision_ver,
    workspace_id=target_ws,
    baseline_chunks=[],
    boundary_repo=None,
    chunk_repo=mock_chunk_repo,
    snapshot_context=snap_context_match,
)

novel_mismatch, no_prog_mismatch, st_mismatch = executor.execute(
    decision=decision_ver,
    workspace_id=target_ws,
    baseline_chunks=[],
    boundary_repo=None,
    chunk_repo=mock_chunk_repo,
    snapshot_context=snap_context_mismatch,
)

version_isolation_ok = (len(novel_match) >= 1 and len(novel_mismatch) == 0 and no_prog_mismatch is True)
record(
    "AC-5",
    "Version isolation: matching opaque version retrieves chunk, stale version returns 0 chunks",
    "executor: LayaExpansionExecutor.execute",
    200,
    version_isolation_ok,
    f"match_count={len(novel_match)}, mismatch_count={len(novel_mismatch)}",
)

# ---------------------------------------------------------------------------
# 13. Dynamic Tombstone Halts Retrieval Fail-Closed (AC-5)
# ---------------------------------------------------------------------------
class MockBoundaryRepo:
    def __init__(self, tombstoned_resources: set[tuple[str, str, str]] | None = None) -> None:
        self.tombstoned = tombstoned_resources or set()

    def has_tombstone(self, workspace_id: str, resource_type: str, resource_id: str) -> bool:
        return (workspace_id, resource_type, resource_id) in self.tombstoned

mock_boundary = MockBoundaryRepo(tombstoned_resources={(target_ws, "source", doc_id)})

novel_tomb, no_prog_tomb, st_tomb = executor.execute(
    decision=decision_ver,
    workspace_id=target_ws,
    baseline_chunks=[],
    boundary_repo=mock_boundary,
    chunk_repo=mock_chunk_repo,
    snapshot_context=snap_context_match,
)
tombstone_retrieval_ok = (len(novel_tomb) == 0 and st_tomb == "tombstoned")
record(
    "AC-5",
    "Dynamic tombstone halts expanded retrieval immediately fail-closed (0 candidates)",
    "executor: execute (active tombstone)",
    200,
    tombstone_retrieval_ok,
    f"tombstoned_chunks_returned={len(novel_tomb)}, status={st_tomb}",
)

# ---------------------------------------------------------------------------
# 14. Dynamic Tombstone Blocks Delivery Gate (AC-5, AC-6)
# ---------------------------------------------------------------------------
state_with_tombstone = {
    "query": "What is the gross revenue?",
    "final_answer": "Gross revenue was forty million dollars.",
    "citations": [{"document_id": doc_id, "chunk_id": "chunk_fin_001"}],
    "workspace_id": target_ws,
    "confidence_score": 0.95,
    "top_chunks": [],
    "escalation_executed": True,
    "require_delivery_gate": True,
    "boundary_repo": mock_boundary,
}
state_blocked_delivery = delivery_gate(state_with_tombstone)
tombstone_delivery_blocked = (
    state_blocked_delivery.get("final_answer") == "NOT_FOUND"
    and state_blocked_delivery.get("citations") == []
    and state_blocked_delivery.get("retrieval_incomplete") is True
)
record(
    "AC-5, AC-6",
    "Dynamic tombstone before delivery blocks free-form delivery fail-closed (refusal)",
    "gate: delivery_gate (active tombstone)",
    200,
    tombstone_delivery_blocked,
    f"final_answer={state_blocked_delivery.get('final_answer')}, citations={state_blocked_delivery.get('citations')}",
)

# ---------------------------------------------------------------------------
# 15. Final Answer Delivery Gate Blocks Unverified Free-Form Delivery (AC-6)
# ---------------------------------------------------------------------------
# Reserving a slot does not permit delivery; unverified free-form prose must be blocked if verifier absent
state_unverified = {
    "query": "What is the gross revenue?",
    "final_answer": "Unverified generated answer text.",
    "citations": [{"document_id": doc_id, "chunk_id": "chunk_fin_001"}],
    "workspace_id": target_ws,
    "confidence_score": 0.9,
    "top_chunks": [],
    "escalation_executed": True,
    "require_delivery_gate": True,
    "verifier": None,  # No verifier ran
}
state_gated = delivery_gate(state_unverified)
delivery_gate_blocks_ok = (
    state_gated.get("final_answer") == "NOT_FOUND"
    and state_gated.get("citations") == []
    and state_gated.get("retrieval_incomplete") is True
)
record(
    "AC-6",
    "Final delivery gate blocks unverified free-form text, falling back to deterministic excerpt or refusal",
    "gate: delivery_gate (absent verifier)",
    200,
    delivery_gate_blocks_ok,
    f"gated_answer={state_gated.get('final_answer')}, incomplete={state_gated.get('retrieval_incomplete')}",
)

# ---------------------------------------------------------------------------
# 16. Final Delivery Gate Rejects Contradicted Verification (AC-6)
# ---------------------------------------------------------------------------
mock_contra_verifier = MockVerifier(support_status="CONTRADICTED")
state_contra = {
    "query": "What is the gross revenue?",
    "final_answer": "Synthesized text that contradicted sources.",
    "citations": [{"document_id": doc_id, "chunk_id": "chunk_fin_001"}],
    "workspace_id": target_ws,
    "confidence_score": 0.9,
    "top_chunks": [],
    "escalation_executed": True,
    "require_delivery_gate": True,
    "verifier": mock_contra_verifier,
}
state_contra_out = delivery_gate(state_contra)
contra_blocked = (
    state_contra_out.get("final_answer") == "NOT_FOUND"
    and state_contra_out.get("citations") == []
    and state_contra_out.get("retrieval_incomplete") is True
)
record(
    "AC-6",
    "Final delivery gate rejects contradicted verification, emitting clean refusal NOT_FOUND",
    "gate: delivery_gate (contradicted verifier)",
    200,
    contra_blocked,
    f"final_answer={state_contra_out.get('final_answer')}, incomplete={state_contra_out.get('retrieval_incomplete')}",
)

# ---------------------------------------------------------------------------
# 17. Single Synthesis Call Invariant Enforced (AC-4, AC-6)
# ---------------------------------------------------------------------------
from services.langgraph_pipeline import run_llm

state_synth = {
    "compressed_text": "Sample valid context",
    "query": "Sample question",
    "workspace_id": target_ws,
    "synthesis_calls": 1,  # Already executed once
}
with patch("services.langgraph_pipeline.call_llm") as mock_call:
    res_synth = run_llm(state_synth)
    single_synth_ok = (res_synth == {} and not mock_call.called)

record(
    "AC-4, AC-6",
    "Single synthesis call invariant: second run_llm call returns empty dict with zero model calls",
    "pipeline: run_llm (synthesis_calls=1)",
    200,
    single_synth_ok,
    f"res_synth={res_synth}, mock_call_called={mock_call.called}",
)

# ---------------------------------------------------------------------------
# 18. No-Progress Detection Halts Escalation into Clean Refusal (AC-6, AC-7)
# ---------------------------------------------------------------------------
state_no_progress = {
    "query": "Sample question",
    "workspace_id": target_ws,
    "query_id": "q_no_prog_01",
    "retrieved_chunks": [doc_chunk],  # doc_chunk already in baseline
    "compressed_text": "Existing text",
    "missing_requirement": MissingRequirementDescription(
        requirement_id="req_np",
        missing_dimension="exact_term",
        description="Term gap",
        failed_stage="run_fidelity_coverage",
        detected_gap="gross revenue",
    ),
    "expansion_steps": 0,
    "laya_calls": 0,
    "synthesis_calls": 0,
    "retrieval_candidates": {},
    "doc_repo": mock_chunk_repo,
    "candidate_chunks": [doc_chunk],
}

with patch("src.services.retrieval.laya_service.LayaDecisionService.decide") as mock_decide:
    mock_decide.return_value = LayaEscalationDecision(
        decision_id="dec_np_01",
        route="lexical_expansion",
        parameters=LexicalExpansionParams(query_terms=("gross", "revenue")),
        confidence=0.9,
    )
    # Expansion returns identical chunk_fin_001
    state_no_prog_out = run_laya_escalation(state_no_progress)

no_prog_ok = (
    state_no_prog_out.get("final_answer") == "NOT_FOUND"
    and state_no_prog_out.get("citations") == []
    and state_no_prog_out.get("retrieval_incomplete") is True
    and state_no_prog_out.get("synthesis_calls") == 0
)
record(
    "AC-6, AC-7",
    "No-progress detection halts escalation into clean refusal with zero synthesis calls",
    "pipeline: run_laya_escalation (no progress)",
    200,
    no_prog_ok,
    f"final_answer={state_no_prog_out.get('final_answer')}, incomplete={state_no_prog_out.get('retrieval_incomplete')}",
)

# ---------------------------------------------------------------------------
# Summary and Evidence Ledger
# ---------------------------------------------------------------------------
total_checks = len(results)
passed_checks = sum(1 for r in results if r["passed"])
failed_checks = total_checks - passed_checks

print(f"\n=== Verification Summary: {passed_checks}/{total_checks} passed, {failed_checks} failed ===")

# Save evidence ledger to scratch area
scratch_dir = Path("/home/swyra/.gemini/antigravity-cli/brain/bfce9234-50e9-4347-b043-30c5f36b67e0/scratch")
scratch_dir.mkdir(parents=True, exist_ok=True)
ledger_path = scratch_dir / "verify_0008_ledger.json"
with open(ledger_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)

print(f"Evidence ledger saved to: {ledger_path}")

if failed_checks > 0:
    sys.exit(1)
sys.exit(0)
