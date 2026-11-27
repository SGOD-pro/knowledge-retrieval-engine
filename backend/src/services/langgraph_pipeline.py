import dataclasses
import json
import logging
import re
import time
from typing import Any, TypedDict
import uuid

from langgraph.graph import END, START, StateGraph

from config import settings
from schemas.models import Chunk, RerankerMode
from services.llm.llm_service import call as call_llm
from services.retrieval.bm25_retriever import BM25Retriever
from services.retrieval.compressor import compress_chunks
from services.retrieval.fidelity_check import CoverageError, check_fidelity
from services.retrieval.graph_retriever import GraphRetriever
from services.retrieval.okf_retriever import OKFRetriever, OkfRetriever, get_okf_retriever
from services.retrieval.page_index_retriever import PageIndexRetriever
from services.retrieval.planner import Plan, planner
from services.retrieval.reranker import rerank
from services.retrieval.vector_retriever import VectorRetriever
try:
    from schemas.contracts.laya import (
        BaselineEvidenceSummary,
        LayaEscalationDecision,
        LayaEscalationRequest,
        MissingRequirementDescription,
    )
    from services.retrieval.laya_executor import LayaExpansionExecutor, validate_chunk_provenance
    from services.retrieval.laya_gate import (
        LayaBudgetLedger,
        calculate_phase_deadline,
    )
    from services.retrieval.laya_service import LayaDecisionService
except ImportError:
    from src.schemas.contracts.laya import (
        BaselineEvidenceSummary,
        LayaEscalationDecision,
        LayaEscalationRequest,
        MissingRequirementDescription,
    )
    from src.services.retrieval.laya_executor import LayaExpansionExecutor, validate_chunk_provenance
    from src.services.retrieval.laya_gate import (
        LayaBudgetLedger,
        calculate_phase_deadline,
    )
    from src.services.retrieval.laya_service import LayaDecisionService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton repository to avoid re-instantiating DynamoDB/Qdrant clients
# ---------------------------------------------------------------------------
_shared_repo = None


def _get_repo():
    global _shared_repo
    if _shared_repo is None:
        from db.database import CloudRepository
        _shared_repo = CloudRepository()
    return _shared_repo


# ---------------------------------------------------------------------------
# OKF soft-boost constant — arbitrary starting value, must be tuned against
# Recall@5 benchmark before treating as settled. See Phase 2 Rev 3 C1/C2.
# ---------------------------------------------------------------------------
OKF_BOOST = 2.0


class PipelineState(TypedDict):
    query: str
    workspace_id: str
    query_embedding: list[float] | None
    query_embedding_provenance: str | None
    document_ids: list[str] | None
    plan: Plan | None
    bm25_candidates: list[Chunk]  # preserved BM25 results for RRF
    candidate_page_ids: list[int]  # pages narrowed by PageIndex
    candidate_chunk_ids: list[
        str
    ]  # chunk IDs without pages narrowed by PageIndex (DOCX, PPTX, CSV)
    candidate_chunks: list[Chunk]  # vector results (also used for fast-path)
    okf_properties: list[dict[str, Any]]
    okf_seed_chunk_ids: list[str]  # chunk IDs from OKF lookup for BM25 boost
    graph_results: list[dict[str, Any]]
    top_chunks: list[Chunk]
    compressed_text: str
    context_snippet: str  # first 500 chars of compressed_text for faithfulness judge
    final_answer: str
    confidence_score: float
    citations: list[str]
    error: str | None
    stage_timings: dict[str, float]  # real per-stage measurements (Component 4)
    force_full_path: bool
    verified_answer: str | None
    verified_chunk: Chunk | None
    execution_result: Any | None
    usage: dict[str, int]
    faithfulness: float | None
    citation_utilization_rate: float | None
    planned_path: str
    executed_path: str
    candidate_page_scopes: list[tuple[str, int]] | None
    graph_chunks: list[Chunk]
    graph_candidate_ids: list[str]
    graph_match_count: int
    graph_latency_ms: float
    graph_traversed_nodes: int
    graph_matched_edges: int
    graph_was_truncated: bool
    graph_truncation_reason: str | None
    graph_entity_state: str
    graph_relationship_state: str
    graph_execution_status: str
    retrieval_candidates: dict[str, list[str]]
    reranker_mode: str
    reranker_telemetry: dict[str, Any]
    status: str | None
    error_code: str | None
    selected_strategies: dict[str, Any] | None
    retained_evidence_items: list[Any]
    structural_candidates: list[Chunk]
    structural_candidate_ids: list[str]
    structural_match_count: int
    page_index_latency_ms: float
    structural_target_state: str
    okf_candidates: list[Chunk]
    okf_candidate_ids: list[str]
    okf_match_count: int
    okf_latency_ms: float
    okf_entity_state: str
    okf_property_state: str
    okf_execution_status: str
    table_store: Any | None
    knowledge_graph_repo: Any | None
    boundary_repo: Any | None
    doc_repo: Any | None
    kg_retriever: Any | None
    retriever: Any | None
    laya_enabled: bool
    laya_calls: int
    expansion_steps: int
    synthesis_calls: int
    laya_decision: dict[str, Any] | None
    laya_status: str
    laya_latency_ms: float
    laya_tokens: dict[str, int]
    require_delivery_gate: bool
    escalation_executed: bool
    retrieval_incomplete: bool
    verifier: Any | None
    semantic_verifier: Any | None
    laya_client: Any | None
    laya_model_id: str | None
    laya_endpoint_url: str | None
    laya_api_key: str | None
    laya_decision_service: Any | None
    laya_expansion_executor: Any | None
    laya_budget_ledger: Any | None
    laya_concurrency_controller: Any | None
    caller_deadline_ms: float | None
    phase_deadline_ms: float | None
    requirement_ids: list[str]
    subgoal_specs: list[dict[str, Any]]
    missing_required_sources: list[str]
    snapshot_sources: dict[str, str]
    snapshot_context: Any | None
    _pipeline_start_time: float
    _escalation_start_time: float
    _escalation_deadline_t: float | None
    escalation_deadline_t: float | None
    financial_binding: Any | None


# ---------------------------------------------------------------------------
# RRF merge (Component 1)
# ---------------------------------------------------------------------------


def _rrf_merge(
    bm25_chunks: list,
    vector_chunks: list,
    graph_chunks: list | None = None,
    structural_chunks: list | None = None,
    okf_chunks: list | None = None,
    k: int = 60,
    graph_weight: float | None = None,
    structural_weight: float = 1.1,
    okf_weight: float = 1.15,
) -> list:
    """Reciprocal Rank Fusion with graph, structural PageIndex, and OKF factual signals.
    k=60 per Cormack et al. 2009.
    Returns deduplicated list of Chunk objects ordered by descending RRF score."""
    g_weight = graph_weight if graph_weight is not None else float(getattr(settings, "GRAPH_FUSION_WEIGHT", 1.10))
    scores: dict[str, float] = {}
    by_id: dict[str, object] = {}
    for rank, chunk in enumerate(bm25_chunks):
        scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (k + rank + 1)
        by_id[chunk.id] = chunk
    for rank, chunk in enumerate(vector_chunks):
        scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (k + rank + 1)
        by_id[chunk.id] = chunk
    if graph_chunks:
        for rank, chunk in enumerate(graph_chunks):
            chunk_w = getattr(chunk, "graph_weight", None)
            w = float(chunk_w) if chunk_w is not None else g_weight
            scores[chunk.id] = scores.get(chunk.id, 0.0) + (w / (k + rank + 1))
            by_id[chunk.id] = chunk
    if structural_chunks:
        for rank, chunk in enumerate(structural_chunks):
            chunk_w = getattr(chunk, "structural_weight", None)
            w = float(chunk_w) if chunk_w is not None else structural_weight
            scores[chunk.id] = scores.get(chunk.id, 0.0) + (w / (k + rank + 1))
            by_id[chunk.id] = chunk
    if okf_chunks:
        for rank, chunk in enumerate(okf_chunks):
            chunk_w = getattr(chunk, "okf_weight", None)
            w = float(chunk_w) if chunk_w is not None else okf_weight
            scores[chunk.id] = scores.get(chunk.id, 0.0) + (w / (k + rank + 1))
            by_id[chunk.id] = chunk
    return [by_id[cid] for cid in sorted(scores, key=scores.__getitem__, reverse=True)]


# ---------------------------------------------------------------------------
# Pipeline nodes
# ---------------------------------------------------------------------------


def route_query(state: PipelineState):
    t0 = time.perf_counter()
    from services.retrieval.planner import planner
    from services.retrieval.strategy_router import StrategyRouter

    # 1. Route query deterministically without remote embedding calls
    plan = planner.route(state["query"])
    if state.get("force_full_path", False):
        from dataclasses import replace
        plan = replace(plan, fast_path=False, use_structured_aggregate=False)

    router = StrategyRouter()
    selected_strategies = router.route(state["query"], plan=plan)

    is_structured = getattr(plan, "use_structured_aggregate", False) or ("structured_table" in selected_strategies.primary)

    # 2. Re-use existing query_embedding if provided by query_service;
    # only embed via Titan if full path is chosen (not structured and not fast path)
    # and embedding not yet computed
    query_embedding = state.get("query_embedding")
    provenance = state.get("query_embedding_provenance")
    if not plan.fast_path and not is_structured and query_embedding is None:
        from providers.embedding_provider import embed_text
        from providers.bedrock_models import get_embedding_model
        query_embedding = embed_text(state["query"])
        provenance = get_embedding_model()

    latency_ms = (time.perf_counter() - t0) * 1000.0
    if is_structured:
        planned = "structured_aggregate"
    else:
        planned = "fast" if plan.fast_path else "full"

    fin_binding = state.get("financial_binding")
    if fin_binding is None:
        try:
            from services.retrieval.table_metric_binding import bind_financial_query
            fin_binding = bind_financial_query(state["query"])
        except Exception as fbe:
            logger.warning("route_query.bind_financial_query_failed: %s", fbe)
            fin_binding = None

    logger.info("route_query.latency_ms=%.2f fast_path=%s planned=%s primary_strategies=%s", latency_ms, plan.fast_path, planned, selected_strategies.primary)
    return {
        "query_embedding": query_embedding,
        "query_embedding_provenance": provenance,
        "plan": plan,
        "planned_path": planned,
        "executed_path": planned,
        "selected_strategies": selected_strategies.to_dict(),
        "financial_binding": fin_binding,
        "stage_timings": {"route_query_ms": latency_ms},
    }


def run_okf_router(state: PipelineState):
    """Bounded OKF factual candidate retrieval (Spec 0006 Slice 2 / AC-9).
    Queries workspace-partitioned OKF facts repository with caller deadline (50ms).
    Yields candidate chunks for soft pooling into reciprocal rank fusion with
    calibrated weight (1.15), dividing weight by 2 for ambiguous or conflicting facts.
    Bypasses OKF lookups completely on fast-path queries (0 tokens, 0ms)."""
    t0 = time.perf_counter()
    existing = state.get("stage_timings", {})

    # 1. Check feature toggle
    if not getattr(settings, "OKF_ENABLED", True):
        return {
            "okf_candidates": [],
            "okf_candidate_ids": [],
            "okf_match_count": 0,
            "okf_latency_ms": 0.0,
            "okf_entity_state": "none",
            "okf_property_state": "none",
            "okf_execution_status": "disabled",
            "okf_properties": [],
            "okf_seed_chunk_ids": [],
            "stage_timings": {**existing, "okf_router_ms": 0.0},
        }

    # 2. Fast-path query bypass (AC-9, Key Invariants)
    plan = state.get("plan")
    if plan and getattr(plan, "fast_path", False):
        logger.info("okf_router.fast_path_bypass query=%s", state.get("query", "")[:50])
        return {
            "okf_candidates": [],
            "okf_candidate_ids": [],
            "okf_match_count": 0,
            "okf_latency_ms": 0.0,
            "okf_entity_state": "none",
            "okf_property_state": "none",
            "okf_execution_status": "fast_path_bypassed",
            "okf_properties": [],
            "okf_seed_chunk_ids": [],
            "stage_timings": {**existing, "okf_router_ms": 0.0},
        }

    ws_id = state.get("workspace_id", "")
    query = state.get("query", "")
    if not ws_id or not query:
        return {
            "okf_candidates": [],
            "okf_candidate_ids": [],
            "okf_match_count": 0,
            "okf_latency_ms": 0.0,
            "okf_entity_state": "none",
            "okf_property_state": "none",
            "okf_execution_status": "empty_input",
            "okf_properties": [],
            "okf_seed_chunk_ids": [],
            "stage_timings": {**existing, "okf_router_ms": 0.0},
        }

    # 3. Execute authoritative OKF lookup with deadline & concurrency bounds
    from db.okf_repo import get_okf_repo
    from db.workspace_boundary_repo import get_workspace_boundary_repo
    from services.retrieval.okf_lookup import get_okf_controller

    okf_repo = get_okf_repo()
    boundary_repo = get_workspace_boundary_repo()
    chunk_repo = _get_repo()
    controller = get_okf_controller()

    retriever = OkfRetriever(
        repository=okf_repo,
        boundary_repo=boundary_repo,
        chunk_repo=chunk_repo,
        controller=controller,
    )

    target_docs = state.get("document_ids")
    timeout_ms = int(getattr(settings, "OKF_TIMEOUT_MS", 50))
    lookup_res = retriever.lookup_sync(
        workspace_id=ws_id,
        query=query,
        deadline_ms=timeout_ms,
        document_ids=target_docs,
    )

    # 4. Calibrate RRF weight (AC-4, AC-5, AC-9)
    # Default calibrated weight is 1.15; divided by 2 if ambiguous or conflicting
    base_weight = float(getattr(settings, "OKF_FUSION_WEIGHT", 1.15))
    is_divided = (lookup_res.entity_state == "ambiguous" or lookup_res.property_state == "conflicting")
    calibrated_weight = (base_weight / 2.0) if is_divided else base_weight

    hydrated_chunks = [
        dataclasses.replace(c, okf_weight=calibrated_weight) if hasattr(c, "okf_weight") else c
        for c in lookup_res.candidate_chunks
    ]

    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "okf_router.latency_ms=%.2f match_count=%d entity_state=%s prop_state=%s weight=%.3f status=%s",
        latency_ms,
        len(hydrated_chunks),
        lookup_res.entity_state,
        lookup_res.property_state,
        calibrated_weight,
        lookup_res.execution_status,
    )

    props_data = [
        ass.model_dump() if hasattr(ass, "model_dump") else ass.__dict__
        for ass in lookup_res.matched_assertions
    ]
    candidate_ids = [str(c.id) for c in hydrated_chunks] or list(lookup_res.candidate_chunk_ids)

    return {
        "okf_candidates": hydrated_chunks,
        "okf_candidate_ids": candidate_ids,
        "okf_match_count": len(hydrated_chunks),
        "okf_latency_ms": lookup_res.latency_ms,
        "okf_entity_state": lookup_res.entity_state,
        "okf_property_state": lookup_res.property_state,
        "okf_execution_status": lookup_res.execution_status,
        "okf_properties": props_data,
        "okf_seed_chunk_ids": candidate_ids,
        "stage_timings": {**existing, "okf_router_ms": latency_ms},
    }


_WORKBOOK_GENERIC_TOKENS = frozenset({
    "data", "file", "sheet", "workbook", "xls", "xlsx", "factsheet",
    "table", "report", "the", "and", "for", "are", "in", "with", "from",
})


def _discover_workbook_candidates(
    query: str,
    all_chunks: list[Any],
    existing_ids: set[str],
    snapshot_sources: dict[str, str] | None = None,
    max_docs: int = 2,
    max_chunks_per_doc: int = 2,
) -> list[Any]:
    """Bounded workbook discovery when query asks about sheets/workbooks by identifying filename."""
    q_lower = query.lower()
    if not re.search(r"\b(?:sheet\s*\d+|workbook)\b|\.(?:xls|xlsx)\b", q_lower):
        return []
    q_tokens = set(re.findall(r"[a-z0-9]{3,}", q_lower))
    if not q_tokens:
        return []

    matched_by_doc: dict[str, list[Any]] = {}
    for c in all_chunks:
        cid = str(getattr(c, "id", ""))
        if cid in existing_ids:
            continue
        did = str(getattr(c, "document_id", ""))
        fmt = str(getattr(c, "source_format", "") or "").lower()
        meta = getattr(c, "metadata", None) or {}
        fname = str(
            meta.get("document_filename")
            or getattr(c, "document_filename", "")
            or (snapshot_sources or {}).get(did, "")
            or ""
        )
        if fmt not in ("xls", "xlsx") and not fname.lower().endswith((".xls", ".xlsx")):
            continue
        fname_tokens = set(re.findall(r"[a-z0-9]{3,}", fname.lower())) - _WORKBOOK_GENERIC_TOKENS
        if not (fname_tokens & q_tokens):
            continue
        bucket = matched_by_doc.setdefault(did, [])
        if len(bucket) < max_chunks_per_doc:
            bucket.append(c)
            if len(matched_by_doc) >= max_docs and len(bucket) == max_chunks_per_doc:
                continue

    added: list[Any] = []
    for did in list(matched_by_doc.keys())[:max_docs]:
        added.extend(matched_by_doc[did])
    return added


def run_bm25(state: PipelineState):
    """BM25 retrieval (Component 1 + 2). top_k=40 for union design."""
    t0 = time.perf_counter()
    repo = _get_repo()
    from services.retrieval.bm25_retriever import get_cached_chunks

    ws_id = state.get("workspace_id", "")
    doc_ids = state.get("document_ids")
    cached_chunks = get_cached_chunks(
        ws_id,
        lambda: repo.get_all_chunks(document_ids=None, workspace_id=ws_id),
    )
    if doc_ids:
        doc_set = set(str(d) for d in doc_ids)
        all_chunks = [c for c in cached_chunks if str(c.document_id) in doc_set]
    else:
        all_chunks = cached_chunks

    b_repo = state.get("boundary_repo")
    if b_repo is None:
        try:
            from db.workspace_boundary_repo import get_workspace_boundary_repo
            b_repo = get_workspace_boundary_repo()
        except Exception:
            b_repo = None
    snapshot_sources: dict[str, str] = {}
    snapshots_by_doc: dict[str, Any] = {}
    if b_repo is not None and ws_id:
        active_versions: dict[str, str] = {}
        try:
            for snap in b_repo.list_document_snapshots(ws_id):
                did = str(getattr(snap, "document_id", "") or "")
                if not did:
                    continue
                snapshots_by_doc[did] = snap
                if getattr(snap, "version", None):
                    active_versions[did] = str(snap.version)
                if getattr(snap, "source", None):
                    snapshot_sources[did] = str(snap.source)
        except Exception:
            active_versions = {}
            snapshot_sources = {}
            snapshots_by_doc = {}
        if active_versions:
            filtered_chunks = []
            for c in all_chunks:
                did = str(getattr(c, "document_id", ""))
                exp_ver = active_versions.get(did)
                if exp_ver:
                    meta = getattr(c, "metadata", None) or {}
                    c_ver = getattr(c, "document_version", None) or meta.get("document_version")
                    if c_ver and str(c_ver) != exp_ver:
                        continue
                filtered_chunks.append(c)
            all_chunks = filtered_chunks

    retriever = BM25Retriever()
    from services.retrieval.subgoal_decomposer import decompose_query
    q_plan = decompose_query(state["query"])
    if len(q_plan.subgoals) > 1:
        per_subgoal_k = max(15, 40 // len(q_plan.subgoals))
        seen_ids = set()
        combined_results = []
        for sg in q_plan.subgoals:
            sg_res = retriever.search(sg.query_text, all_chunks, top_k=per_subgoal_k)
            for c, s in sg_res:
                if c.id not in seen_ids:
                    seen_ids.add(c.id)
                    combined_results.append((c, s))
        overall_res = retriever.search(state["query"], all_chunks, top_k=40)
        for c, s in overall_res:
            if c.id not in seen_ids:
                seen_ids.add(c.id)
                combined_results.append((c, s))
        results = combined_results
    else:
        results = retriever.search(state["query"], all_chunks, top_k=40)

    chunks = [c for c, _ in results]
    wb_added = _discover_workbook_candidates(
        state["query"],
        all_chunks,
        {str(c.id) for c in chunks},
        snapshot_sources=snapshot_sources,
    )
    if wb_added:
        chunks.extend(wb_added)
    if b_repo is not None and ws_id and snapshots_by_doc and re.search(r"\b(?:sheet\s*\d+|workbook)\b|\.(?:xls|xlsx)\b", state["query"].lower()):
        from services.retrieval.sheet_inventory import build_sheet_inventory_chunk_and_evidence
        allowed_set = {str(d) for d in doc_ids} if doc_ids else None
        existing_ids = {str(c.id) for c in chunks}
        matched_dids = {str(getattr(c, "document_id", "")) for c in wb_added} | {
            str(getattr(c, "document_id", ""))
            for c in chunks
            if str(getattr(c, "source_format", "") or "").lower() in ("xls", "xlsx")
        }
        for did in matched_dids:
            snap = snapshots_by_doc.get(did)
            if snap is None:
                continue
            pair = build_sheet_inventory_chunk_and_evidence(
                snap,
                workspace_id=ws_id,
                boundary_repo=b_repo,
                allowed_document_ids=allowed_set,
            )
            if pair is not None and pair[0].id not in existing_ids:
                chunks.append(pair[0])
                existing_ids.add(pair[0].id)
    avg_score = sum(s for _, s in results) / max(1, len(results))

    # Check explicit source filenames against in-scope active workspace documents
    allowed_doc_set = {str(d) for d in doc_ids} if doc_ids else None
    active_source_names: list[str] = []
    for did, src_name in snapshot_sources.items():
        if allowed_doc_set is not None and did not in allowed_doc_set:
            continue
        if src_name:
            active_source_names.append(str(src_name).lower())
    if not active_source_names and hasattr(repo, "get_workspace_documents"):
        try:
            ws_docs = (repo.get_workspace_documents(ws_id, page=1, limit=1000) or {}).get("documents", [])
            for d in ws_docs:
                did = str(d.get("id") or d.get("document_id") or "")
                if allowed_doc_set is not None and did not in allowed_doc_set:
                    continue
                fn = d.get("filename")
                if fn:
                    active_source_names.append(str(fn).lower())
        except Exception:
            pass
    active_stems = {
        re.sub(r"\.(?:csv|xlsx?|tsv|pdf)$", "", nm, flags=re.IGNORECASE)
        for nm in active_source_names
    }
    explicit_source_files = re.findall(
        r"\b([a-z0-9_.\-]+\.(?:csv|xlsx?|tsv|pdf))\b",
        state["query"].lower(),
    )
    missing_required_sources: list[str] = []
    if active_source_names and explicit_source_files:
        for req_file in explicit_source_files:
            req_stem = re.sub(r"\.(?:csv|xlsx?|tsv|pdf)$", "", req_file, flags=re.IGNORECASE)
            if req_file not in active_source_names and req_stem not in active_stems:
                if req_file not in missing_required_sources:
                    missing_required_sources.append(req_file)

    requirement_ids = [sg.subgoal_id for sg in q_plan.subgoals]
    subgoal_specs = [
        {
            "id": sg.subgoal_id,
            "query_text": sg.query_text,
            "source_hint": sg.source_hint,
            "eligible_sources": list(getattr(sg, "eligible_sources", ()) or ()),
            "operator": sg.target_operator.value if getattr(sg, "target_operator", None) else "count",
        }
        for sg in q_plan.subgoals
    ]

    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "bm25.latency_ms=%.2f bm25.result_count=%d bm25.avg_score=%.4f",
        latency_ms,
        len(chunks),
        avg_score,
    )
    existing = state.get("stage_timings", {})
    return {
        "bm25_candidates": chunks,
        "candidate_chunks": chunks,  # initial write; vector node will overwrite
        "requirement_ids": requirement_ids,
        "subgoal_specs": subgoal_specs,
        "missing_required_sources": missing_required_sources,
        "snapshot_sources": snapshot_sources,
        "stage_timings": {**existing, "bm25_ms": latency_ms},
    }


def _sync_list_snapshots(
    boundary_repo: Any,
    workspace_id: str,
    remaining_deadline_s: float,
) -> list[Any]:
    return boundary_repo.list_document_snapshots(workspace_id)


def run_page_index(state: PipelineState):
    """PageIndex structural candidate extraction (Slice 2 / AC-3).
    Evaluates query for structural targets (Table N, Figure N), resolves candidate
    pages via isolated lookup against durable PageIndex summaries, and returns
    structural candidates for reciprocal rank fusion without hard page gating."""
    t0 = time.perf_counter()
    existing = state.get("stage_timings", {})

    if not getattr(settings, "PAGEINDEX_ENABLED", True):
        retrieval_cands = dict(state.get("retrieval_candidates", {}))
        retrieval_cands.update({
            "structural_candidate_ids": [],
            "structural_match_count": 0,
            "page_index_latency_ms": 0.0,
            "structural_target_state": "none",
        })
        return {
            "structural_candidates": [],
            "structural_candidate_ids": [],
            "structural_match_count": 0,
            "page_index_latency_ms": 0.0,
            "structural_target_state": "none",
            "okf_candidates": [],
            "okf_candidate_ids": [],
            "okf_match_count": 0,
            "okf_latency_ms": 0.0,
            "okf_entity_state": "none",
            "okf_property_state": "none",
            "okf_execution_status": "none",
            "candidate_page_ids": None,
            "candidate_chunk_ids": None,
            "candidate_page_scopes": None,
            "retrieval_candidates": retrieval_cands,
            "stage_timings": {**existing, "page_index_ms": 0.0},
        }

    ws_id = state.get("workspace_id", "")
    query = state.get("query", "")
    target_docs = state.get("document_ids")

    retriever = PageIndexRetriever()
    target_labels = retriever.lookup_service.extract_target_labels(query)

    structural_chunks = []
    matched_pages: list[int] = []
    structural_target_state = "none"

    if target_labels and ws_id:
        repo = _get_repo()
        from src.db.workspace_boundary_repo import get_workspace_boundary_repo
        from src.services.retrieval.page_index_lookup import get_page_index_controller
        b_repo = get_workspace_boundary_repo()
        controller = get_page_index_controller()
        try:
            # Bound total PageIndex stage work across documents and labels
            stage_limit_ms = float(
                getattr(settings, "PAGEINDEX_STAGE_TIMEOUT_MS", getattr(settings, "PAGEINDEX_TIMEOUT_MS", 50))
            )
            per_lookup_limit_ms = float(getattr(settings, "PAGEINDEX_TIMEOUT_MS", 50))
            stage_t0 = time.perf_counter()
            stage_exhausted = False

            # Stage deadline covers snapshot discovery under bounded admission and caller deadline
            elapsed_stage_ms = (time.perf_counter() - stage_t0) * 1000.0
            remaining_stage_ms = stage_limit_ms - elapsed_stage_ms
            if remaining_stage_ms <= 1.0:
                logger.warning(
                    "page_index.stage_budget_exhausted elapsed_ms=%.1f limit_ms=%.1f fallback=baseline",
                    elapsed_stage_ms,
                    stage_limit_ms,
                )
                stage_exhausted = True
                snaps = []
            else:
                snaps_res, snap_status = controller.run_storage_work_sync(
                    _sync_list_snapshots,
                    b_repo,
                    ws_id,
                    deadline_s=min(per_lookup_limit_ms, remaining_stage_ms) / 1000.0,
                )
                if snap_status != "completed" or snaps_res is None:
                    logger.warning(
                        "page_index.snapshot_discovery_failed ws=%s status=%s (fallback=baseline)",
                        ws_id,
                        snap_status,
                    )
                    snaps = []
                else:
                    snaps = snaps_res

            if target_docs:
                target_set = set(target_docs)
                snaps = [s for s in snaps if s.document_id in target_set]

            pdf_snaps = [s for s in snaps if s.format.lower() == "pdf"]

            for snap in pdf_snaps:
                if stage_exhausted:
                    break
                doc_id = snap.document_id
                doc_ver = snap.version
                for t_label in target_labels:
                    elapsed_stage_ms = (time.perf_counter() - stage_t0) * 1000.0
                    remaining_stage_ms = stage_limit_ms - elapsed_stage_ms
                    if remaining_stage_ms <= 1.0:
                        logger.warning(
                            "page_index.stage_budget_exhausted elapsed_ms=%.1f limit_ms=%.1f fallback=baseline",
                            elapsed_stage_ms,
                            stage_limit_ms,
                        )
                        stage_exhausted = True
                        break

                    effective_deadline_ms = min(per_lookup_limit_ms, remaining_stage_ms)
                    res, status = retriever.lookup_sync(
                        workspace_id=ws_id,
                        document_id=doc_id,
                        document_version=doc_ver,
                        target_label=t_label,
                        deadline_ms=effective_deadline_ms,
                        chunk_repo=repo,
                    )
                    if status == "caller_timeout":
                        logger.warning(
                            "page_index.lookup_timeout doc=%s ver=%s label=%s deadline_ms=%.1f (fallback=baseline)",
                            doc_id,
                            doc_ver,
                            t_label,
                            effective_deadline_ms,
                        )
                    elif status == "admission_rejected":
                        logger.warning(
                            "page_index.lookup_admission_rejected doc=%s ver=%s label=%s (queue saturated, fallback=baseline)",
                            doc_id,
                            doc_ver,
                            t_label,
                        )
                    elif status == "tombstoned":
                        logger.info(
                            "page_index.lookup_tombstoned doc=%s ver=%s label=%s (fail-closed, fallback=baseline)",
                            doc_id,
                            doc_ver,
                            t_label,
                        )

                    if res and (res.candidate_pages or res.cross_reference_pages):
                        pages_to_fetch = res.candidate_pages if res.candidate_pages else res.cross_reference_pages
                        matched_pages.extend(pages_to_fetch)
                        structural_target_state = res.state

                        doc_chunks = res.candidate_chunks or ()
                        for c in doc_chunks:
                            if c.page_number in pages_to_fetch:
                                from dataclasses import replace
                                weight_div = 0.5 if res.state == "ambiguous" else 1.0
                                s_weight = float(getattr(settings, "PAGEINDEX_STRUCTURAL_WEIGHT", 1.1)) * weight_div
                                c_meta = dict(c.metadata) if isinstance(c.metadata, dict) else {}
                                c_meta["strategy"] = "page_index"
                                c_struct = replace(
                                    c,
                                    structural_weight=s_weight,
                                    metadata=c_meta,
                                )
                                structural_chunks.append(c_struct)

                # Check if total stage limit was reached across document iterations
                if (time.perf_counter() - stage_t0) * 1000.0 >= stage_limit_ms:
                    break
        except Exception as exc:
            logger.warning("page_index.lookup_failed error=%s (preserving baseline)", exc)

    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "page_index.latency_ms=%.2f targets=%s match_count=%d state=%s",
        latency_ms,
        target_labels,
        len(structural_chunks),
        structural_target_state,
    )
    retrieval_cands = dict(state.get("retrieval_candidates", {}))
    retrieval_cands.update({
        "structural_candidate_ids": [str(c.id) for c in structural_chunks],
        "structural_match_count": len(structural_chunks),
        "page_index_latency_ms": latency_ms,
        "structural_target_state": structural_target_state,
    })
    return {
        "structural_candidates": structural_chunks,
        "structural_candidate_ids": [str(c.id) for c in structural_chunks],
        "structural_match_count": len(structural_chunks),
        "page_index_latency_ms": latency_ms,
        "structural_target_state": structural_target_state,
        "candidate_page_ids": None,
        "candidate_chunk_ids": None,
        "candidate_page_scopes": None,
        "retrieval_candidates": retrieval_cands,
        "stage_timings": {**existing, "page_index_ms": latency_ms},
    }


def run_vector(state: PipelineState):
    """Vector retrieval (Component 1). Searches within document-scoped PageIndex candidates."""
    t0 = time.perf_counter()
    repo = _get_repo()
    retriever = VectorRetriever(repository=repo)

    plan = state["plan"]
    is_fast_path = plan.fast_path if plan else False

    chunks = retriever.search(
        query=state["query"],
        query_embedding=state.get("query_embedding"),
        fast_path=is_fast_path,
        document_ids=state.get("document_ids"),
        candidate_page_ids=None if is_fast_path else state.get("candidate_page_ids"),
        candidate_chunk_ids=None if is_fast_path else state.get("candidate_chunk_ids"),
        candidate_page_scopes=None if is_fast_path else state.get("candidate_page_scopes"),
        workspace_id=state.get("workspace_id", ""),
        top_k=40 if not is_fast_path else 10,
    )

    from dataclasses import replace

    vector_chunks = [replace(c, similarity_score=float(s)) for c, s in chunks]
    avg_sim = sum(s for _, s in chunks) / max(1, len(chunks)) if chunks else 0.0
    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "vector.latency_ms=%.2f vector.result_count=%d vector.avg_sim=%.4f",
        latency_ms,
        len(vector_chunks),
        avg_sim,
    )
    existing = state.get("stage_timings", {})

    if is_fast_path:
        # Fast path: expose top-5 vector results as top_chunks for end_fast_path
        return {
            "candidate_chunks": vector_chunks,
            "top_chunks": vector_chunks[:5],
            "stage_timings": {**existing, "vector_ms": latency_ms},
        }
    return {
        "candidate_chunks": vector_chunks,
        "stage_timings": {**existing, "vector_ms": latency_ms},
    }


def run_graph(state: PipelineState):
    """Bounded Knowledge Graph candidate retrieval (Spec 0007 Slice 3 / AC-10).
    Traverses workspace-partitioned graph with caller deadline (75ms).
    Yields candidate chunks for soft pooling into reciprocal rank fusion with
    provisional initial weight (1.10), dividing weight by 2 (0.55) for ambiguous or
    conflicting relationships.
    Bypasses graph lookups completely on fast-path queries (0 tokens, 0ms)."""
    t0 = time.perf_counter()
    existing = state.get("stage_timings", {})

    # 1. Check feature toggle
    if not getattr(settings, "GRAPH_ENABLED", False):
        return {
            "graph_results": [],
            "graph_chunks": [],
            "graph_candidate_ids": [],
            "graph_match_count": 0,
            "graph_latency_ms": 0.0,
            "graph_traversed_nodes": 0,
            "graph_matched_edges": 0,
            "graph_was_truncated": False,
            "graph_truncation_reason": None,
            "graph_entity_state": "none",
            "graph_relationship_state": "none",
            "graph_execution_status": "disabled",
            "stage_timings": {**existing, "graph_ms": 0.0},
        }

    # 2. Fast-path query bypass (AC-10)
    plan = state.get("plan")
    if plan and getattr(plan, "fast_path", False):
        logger.info("run_graph.fast_path_bypass query=%s", state.get("query", "")[:50])
        return {
            "graph_results": [],
            "graph_chunks": [],
            "graph_candidate_ids": [],
            "graph_match_count": 0,
            "graph_latency_ms": 0.0,
            "graph_traversed_nodes": 0,
            "graph_matched_edges": 0,
            "graph_was_truncated": False,
            "graph_truncation_reason": None,
            "graph_entity_state": "none",
            "graph_relationship_state": "none",
            "graph_execution_status": "fast_path_bypassed",
            "stage_timings": {**existing, "graph_ms": 0.0},
        }

    ws_id = state.get("workspace_id", "")
    query = state.get("query", "")
    if not ws_id or not query:
        return {
            "graph_results": [],
            "graph_chunks": [],
            "graph_candidate_ids": [],
            "graph_match_count": 0,
            "graph_latency_ms": 0.0,
            "graph_traversed_nodes": 0,
            "graph_matched_edges": 0,
            "graph_was_truncated": False,
            "graph_truncation_reason": None,
            "graph_entity_state": "none",
            "graph_relationship_state": "none",
            "graph_execution_status": "empty_input",
            "stage_timings": {**existing, "graph_ms": 0.0},
        }

    # 3. Execute authoritative KnowledgeGraphRetriever expansion
    try:
        from db.knowledge_graph_repo import get_shared_knowledge_graph_repository
        from db.workspace_boundary_repo import get_workspace_boundary_repo
        from modules.documents.documents_repository import DocumentsRepository
        from services.retrieval.knowledge_graph_retriever import (
            KnowledgeGraphLookupResult,
            KnowledgeGraphRetriever,
        )
    except ImportError:
        from src.db.knowledge_graph_repo import get_shared_knowledge_graph_repository
        from src.db.workspace_boundary_repo import get_workspace_boundary_repo
        from src.modules.documents.documents_repository import DocumentsRepository
        from src.services.retrieval.knowledge_graph_retriever import (
            KnowledgeGraphLookupResult,
            KnowledgeGraphRetriever,
        )

    try:
        from db.table_store import get_shared_table_store
    except ImportError:
        try:
            from src.db.table_store import get_shared_table_store
        except ImportError:
            get_shared_table_store = None

    kg_repo = state.get("knowledge_graph_repo") or get_shared_knowledge_graph_repository()
    boundary_repo = state.get("boundary_repo") or get_workspace_boundary_repo()
    doc_repo = state.get("doc_repo") or _get_repo()

    table_store = state.get("table_store")
    if table_store is None and "table_store" not in state and get_shared_table_store is not None:
        try:
            table_store = get_shared_table_store()
        except Exception as e:
            logger.warning("Failed to resolve shared table store for run_graph: %s", e)
            table_store = None

    retriever = state.get("kg_retriever") or state.get("retriever")
    if retriever is None:
        retriever = KnowledgeGraphRetriever(
            repository=kg_repo,
            boundary_repo=boundary_repo,
            documents_repo=doc_repo,
            table_store=table_store,
        )
    elif getattr(retriever, "table_store", None) is None and table_store is not None:
        retriever.table_store = table_store

    timeout_ms = float(getattr(settings, "GRAPH_TIMEOUT_MS", 75))
    max_hops = int(getattr(settings, "GRAPH_MAX_HOPS", 2))

    try:
        lookup_res = retriever.expand(
            workspace_id=ws_id,
            query=query,
            max_hops=max_hops,
            caller_deadline_ms=timeout_ms,
            snapshot_context=state.get("snapshot_context"),
        )
    except Exception as e:
        logger.warning("run_graph: retriever.expand failed fail-closed: %s", e)
        lookup_res = KnowledgeGraphLookupResult(
            execution_status="storage_failed",
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    # 4. Calibrate RRF weight (AC-10)
    base_weight = float(getattr(settings, "GRAPH_FUSION_WEIGHT", 1.10))
    is_ambiguous_or_conflicting = (
        lookup_res.entity_state == "ambiguous"
        or lookup_res.relationship_state == "conflicting"
    )
    ambiguous_weight = float(getattr(settings, "GRAPH_AMBIGUOUS_FUSION_WEIGHT", 0.55))
    calibrated_weight = ambiguous_weight if is_ambiguous_or_conflicting else base_weight

    graph_chunks = [
        dataclasses.replace(c, graph_weight=calibrated_weight)
        if dataclasses.is_dataclass(c) and hasattr(c, "graph_weight")
        else c
        for c in lookup_res.candidate_chunks
    ]

    candidate_ids = [str(c.id) for c in graph_chunks] or list(lookup_res.matched_chunk_ids)

    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "run_graph.latency_ms=%.2f match_count=%d entity_state=%s rel_state=%s weight=%.3f status=%s",
        latency_ms,
        len(graph_chunks),
        lookup_res.entity_state,
        lookup_res.relationship_state,
        calibrated_weight,
        lookup_res.execution_status,
    )

    return {
        "graph_results": list(lookup_res.candidate_chunks),
        "graph_chunks": graph_chunks,
        "graph_candidate_ids": candidate_ids,
        "graph_match_count": len(graph_chunks),
        "graph_latency_ms": lookup_res.latency_ms,
        "graph_traversed_nodes": lookup_res.traversed_node_count,
        "graph_matched_edges": lookup_res.edge_match_count,
        "graph_was_truncated": lookup_res.was_truncated,
        "graph_truncation_reason": lookup_res.truncation_reason,
        "graph_entity_state": lookup_res.entity_state,
        "graph_relationship_state": lookup_res.relationship_state,
        "graph_execution_status": lookup_res.execution_status,
        "stage_timings": {**existing, "graph_ms": latency_ms},
    }


def run_reranker(state: PipelineState):
    """RRF merge of bm25_candidates + candidate_chunks (vector) + graph_chunks + structural_candidates + okf_candidates, then neural rerank.
    Tracks raw retrieval candidates per stage for honest evaluation."""
    t0 = time.perf_counter()
    bm25 = state.get("bm25_candidates", [])
    vector = state.get("candidate_chunks", [])
    graph = state.get("graph_chunks", [])
    structural = state.get("structural_candidates", [])
    okf_candidates = state.get("okf_candidates", [])

    graph_weight = float(getattr(settings, "GRAPH_FUSION_WEIGHT", 1.10))
    structural_weight = float(getattr(settings, "PAGEINDEX_STRUCTURAL_WEIGHT", 1.1))
    okf_weight = float(getattr(settings, "OKF_FUSION_WEIGHT", 1.15))
    merged = _rrf_merge(
        bm25,
        vector,
        graph_chunks=graph,
        structural_chunks=structural,
        okf_chunks=okf_candidates,
        k=60,
        graph_weight=graph_weight,
        structural_weight=structural_weight,
        okf_weight=okf_weight,
    )
    if not merged:
        logger.warning("run_reranker: no chunks to rerank — returning empty")
        cands = dict(state.get("retrieval_candidates", {}))
        cands.update({
            "bm25": [],
            "vector": [],
            "graph": [],
            "structural": [],
            "okf": [],
            "rrf": [],
            "pre_rerank": [],
            "reranked": [],
            "structural_candidate_ids": [],
            "structural_match_count": 0,
            "page_index_latency_ms": 0.0,
            "structural_target_state": "none",
            "okf_candidate_ids": [],
            "okf_match_count": 0,
            "okf_latency_ms": 0.0,
            "okf_entity_state": "none",
            "okf_property_state": "none",
            "graph_candidate_ids": [],
            "graph_match_count": 0,
            "graph_latency_ms": 0.0,
            "graph_traversed_nodes": 0,
            "graph_matched_edges": 0,
            "graph_was_truncated": False,
            "graph_truncation_reason": None,
            "graph_entity_state": "none",
            "graph_relationship_state": "none",
        })
        from providers.reranker_provider import get_bypassed_reranker_telemetry

        return {
            "top_chunks": [],
            "retrieval_candidates": cands,
            "reranker_mode": RerankerMode.BYPASSED.value,
            "reranker_telemetry": get_bypassed_reranker_telemetry(candidate_count=0),
        }

    fin_binding = state.get("financial_binding")
    if fin_binding and getattr(fin_binding, "is_active", False) and merged:
        try:
            from services.retrieval.table_metric_binding import score_table_chunk_metric_affinity
            merged = sorted(
                merged,
                key=lambda c: score_table_chunk_metric_affinity(getattr(c, "text", ""), fin_binding),
                reverse=True,
            )
        except Exception as sae:
            logger.warning("run_reranker.score_merged_chunks_failed: %s", sae)

    try:
        rerank_res = rerank(
            state["query"],
            merged,
            top_k=6,
            return_mode=True,
            financial_binding=fin_binding,
            return_telemetry=True,
        )
    except TypeError:
        try:
            rerank_res = rerank(state["query"], merged, top_k=6, return_mode=True, financial_binding=fin_binding)
        except TypeError:
            rerank_res = rerank(state["query"], merged, top_k=6, return_mode=True)

    from providers.reranker_provider import get_bypassed_reranker_telemetry, get_last_reranker_telemetry

    if isinstance(rerank_res, tuple) and len(rerank_res) == 3:
        top_chunks, reranker_mode, reranker_telemetry = rerank_res
    elif isinstance(rerank_res, tuple) and len(rerank_res) == 2:
        top_chunks, reranker_mode = rerank_res
        reranker_telemetry = dict(get_last_reranker_telemetry() or get_bypassed_reranker_telemetry(mode=reranker_mode, candidate_count=len(merged)))
    else:
        # Accommodate test doubles in legacy phase tests returning bare chunk list
        top_chunks = rerank_res
        reranker_mode = getattr(rerank_res, "reranker_mode", RerankerMode.BYPASSED.value)
        reranker_telemetry = dict(getattr(rerank_res, "reranker_telemetry", None) or get_bypassed_reranker_telemetry(mode=reranker_mode, candidate_count=len(merged)))

    # Ensure each retained sheet_inventory chunk keeps a supporting row/content chunk
    # from the same authorized workbook document and active version in top_chunks.
    if isinstance(top_chunks, list) and top_chunks:
        ws_id_rr = str(state.get("workspace_id", "") or "")
        for inv_c in list(top_chunks):
            if getattr(inv_c, "element_type", "") != "sheet_inventory":
                continue
            inv_did = str(getattr(inv_c, "document_id", "") or "")
            inv_ver = getattr(inv_c, "document_version", None) or getattr(inv_c, "version", None)
            has_row = any(
                str(getattr(c, "document_id", "") or "") == inv_did
                and getattr(c, "element_type", "") != "sheet_inventory"
                for c in top_chunks
            )
            if has_row:
                continue
            row_cand = None
            for cand in merged:
                if str(getattr(cand, "document_id", "") or "") != inv_did:
                    continue
                if getattr(cand, "element_type", "") == "sheet_inventory":
                    continue
                c_ws = str(getattr(cand, "workspace_id", "") or "")
                if ws_id_rr and c_ws and c_ws != ws_id_rr:
                    continue
                c_meta = getattr(cand, "metadata", None) or {}
                c_ver = (
                    getattr(cand, "document_version", None)
                    or getattr(cand, "version", None)
                    or c_meta.get("document_version")
                    or c_meta.get("version")
                )
                if c_ver and inv_ver and str(c_ver) != str(inv_ver):
                    continue
                row_cand = cand
                break
            if row_cand is not None:
                if len(top_chunks) >= 6:
                    for idx in range(len(top_chunks) - 1, -1, -1):
                        if str(getattr(top_chunks[idx], "document_id", "") or "") != inv_did:
                            top_chunks[idx] = row_cand
                            break
                else:
                    top_chunks.append(row_cand)

    # Ensure multi-requirement queries preserve at least one supporting chunk per requirement within top_k=6
    subgoal_specs = state.get("subgoal_specs") or []
    snapshot_sources = state.get("snapshot_sources") or {}
    requirement_evidence: dict[str, list[str]] = {}
    if isinstance(top_chunks, list) and top_chunks and len(subgoal_specs) > 1 and merged:
        _req_stop = {
            "the", "and", "for", "with", "from", "that", "this", "what", "how", "many",
            "compare", "value", "values", "using", "across", "between", "same", "scale",
        }
        from services.evaluation.benchmark_scorer import extract_numbers_normalized

        def _src_matches_name(did: str, ref_name: str) -> bool:
            src_name = str(snapshot_sources.get(did, "") or "").lower()
            if not src_name or not ref_name:
                return False
            src_stem = re.sub(r"\.(?:csv|xlsx?|tsv|pdf)$", "", src_name, flags=re.IGNORECASE)
            ref_low = ref_name.strip().lower()
            ref_stem = re.sub(r"\.(?:csv|xlsx?|tsv|pdf)$", "", ref_low, flags=re.IGNORECASE)
            return ref_low == src_name or (bool(ref_stem) and ref_stem == src_stem)

        def _sg_score(cand: Any, sg: dict[str, Any], used_docs: set[str] | None = None) -> tuple[float, float, float, float, float, float]:
            did = str(getattr(cand, "document_id", "") or "")
            hint = str(sg.get("source_hint") or "").strip().lower()
            elig = [str(e).strip().lower() for e in (sg.get("eligible_sources") or []) if str(e).strip()]
            if hint:
                src_match = 1.0 if _src_matches_name(did, hint) else 0.0
            elif elig:
                src_match = 1.0 if any(_src_matches_name(did, e) for e in elig) else 0.0
            else:
                src_match = 0.0
            distinct_bonus = 1.0 if (len(elig) > 1 and src_match > 0.0 and used_docs is not None and did not in used_docs) else 0.0

            q_txt = str(sg.get("query_text") or "")
            q_low = q_txt.lower()
            txt = str(getattr(cand, "text", "") or "")
            txt_low = txt.lower()

            # Period/year match
            req_years = {y for y in re.findall(r"\b(19\d{2}|20\d{2})\b", q_low)}
            cand_years = {y for y in re.findall(r"\b(19\d{2}|20\d{2})\b", txt_low)}
            if req_years:
                if req_years <= cand_years:
                    period_match = 1.0
                elif cand_years:
                    period_match = -1.0
                else:
                    period_match = 0.0
            else:
                period_match = 0.0

            # Operand & metric/code match
            cand_nums = extract_numbers_normalized(txt)
            non_year_nums = {n for n in cand_nums if not (len(n) == 4 and n.isdigit() and 1900 <= int(n) <= 2099)}
            operand_match = 0.0
            if "current ratio" in q_low:
                if re.search(r"\bnot\s+current\s+ratio\b", txt_low):
                    operand_match = -1.0
                elif "current assets" in txt_low and "current liabilities" in txt_low and len(non_year_nums) >= 2:
                    operand_match = 2.0
                elif "current ratio" in txt_low and non_year_nums:
                    operand_match = 1.5
            elif non_year_nums and sg.get("operator") in ("compare", "ratio", "difference", "percentage_difference", "percentage_point_difference", "sum", "average", "count"):
                operand_match = 1.0

            req_codes = re.findall(r"\b([A-Za-z]\d{1,4})\b", q_txt)
            if req_codes:
                if all(re.search(rf"\b{re.escape(code.lower())}\b", txt_low) for code in req_codes) and non_year_nums and operand_match >= 0.0:
                    operand_match += 1.0
                elif not any(re.search(rf"\b{re.escape(code.lower())}\b", txt_low) for code in req_codes):
                    operand_match = min(operand_match, 0.0)

            toks = [
                t for t in re.findall(r"[a-z0-9]{2,}", q_low)
                if t not in _req_stop
            ]
            overlap = (sum(1 for t in toks if t in txt_low) / max(1, len(toks))) if toks else 0.0
            return (src_match, distinct_bonus, operand_match, period_match, overlap, float(getattr(cand, "reranker_score", 0.0) or 0.0))

        protected_ids: set[str] = set()
        used_source_docs: set[str] = set()
        for sg in subgoal_specs:
            sg_id = str(sg.get("id") or "")
            scored_pool = sorted(merged, key=lambda c: _sg_score(c, sg, used_source_docs), reverse=True)
            best_cand = scored_pool[0] if scored_pool else None
            if best_cand is None:
                continue
            best_src, _, best_op, best_per, best_ov, _ = _sg_score(best_cand, sg, used_source_docs)
            if best_src == 0.0 and best_ov == 0.0 and best_op <= 0.0:
                continue

            # Check if top_chunks already contains a chunk satisfying this requirement (including operand & period match)
            covered = None
            for c in top_chunks:
                c_src, _, c_op, c_per, c_ov, _ = _sg_score(c, sg, used_source_docs)
                if best_src > 0.0 and c_src == 0.0:
                    continue
                if best_op > 0.0 and c_op < best_op:
                    continue
                if best_per > 0.0 and c_per < best_per:
                    continue
                if c_ov >= best_ov * 0.8 and (c_ov > 0.0 or c_op > 0.0):
                    covered = c
                    break

            if covered is None:
                if len(top_chunks) >= 6:
                    for idx in range(len(top_chunks) - 1, -1, -1):
                        if str(top_chunks[idx].id) not in protected_ids:
                            top_chunks[idx] = best_cand
                            covered = best_cand
                            break
                else:
                    top_chunks.append(best_cand)
                    covered = best_cand

            if covered is not None:
                protected_ids.add(str(covered.id))
                cov_did = str(getattr(covered, "document_id", "") or "")
                if cov_did:
                    used_source_docs.add(cov_did)
                requirement_evidence[sg_id] = [str(covered.id)]
                meta = dict(getattr(covered, "metadata", None) or {})
                existing_rids = list(meta.get("requirement_ids") or [])
                if sg_id not in existing_rids:
                    existing_rids.append(sg_id)
                meta["requirement_ids"] = existing_rids
                try:
                    covered.metadata = meta
                except Exception:
                    pass

    if fin_binding and getattr(fin_binding, "is_active", False) and top_chunks:
        try:
            from services.retrieval.table_metric_binding import score_table_chunk_metric_affinity
            top_chunks = sorted(
                top_chunks,
                key=lambda c: score_table_chunk_metric_affinity(getattr(c, "text", ""), fin_binding),
                reverse=True,
            )
        except Exception as sae:
            logger.warning("run_reranker.score_top_chunks_failed: %s", sae)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "reranker.latency_ms=%.2f reranker.input_count=%d reranker.output_count=%d mode=%s",
        latency_ms,
        len(merged),
        len(top_chunks),
        reranker_mode,
    )
    existing = state.get("stage_timings", {})
    existing_cands = dict(state.get("retrieval_candidates", {}))

    retrieval_candidates = {
        **existing_cands,
        "bm25": [str(c.id) for c in bm25],
        "vector": [str(c.id) for c in vector],
        "graph": [str(c.id) for c in graph],
        "structural": [str(c.id) for c in structural],
        "okf": [str(c.id) for c in okf_candidates],
        "rrf": [str(c.id) for c in merged],
        "pre_rerank": [str(c.id) for c in merged],
        "reranked": [str(c.id) for c in top_chunks],
        "requirement_ids": state.get("requirement_ids", []),
        "requirement_evidence": requirement_evidence,
        "missing_required_sources": state.get("missing_required_sources", []),
        "structural_candidate_ids": [str(c.id) for c in structural],
        "structural_match_count": len(structural),
        "page_index_latency_ms": state.get("page_index_latency_ms", 0.0),
        "structural_target_state": state.get("structural_target_state", "none"),
        "okf_candidate_ids": [str(c.id) for c in okf_candidates],
        "okf_match_count": len(okf_candidates),
        "okf_latency_ms": state.get("okf_latency_ms", 0.0),
        "okf_entity_state": state.get("okf_entity_state", "none"),
        "okf_property_state": state.get("okf_property_state", "none"),
        "graph_candidate_ids": state.get("graph_candidate_ids", [str(c.id) for c in graph]),
        "graph_match_count": state.get("graph_match_count", len(graph)),
        "graph_latency_ms": state.get("graph_latency_ms", 0.0),
        "graph_traversed_nodes": state.get("graph_traversed_nodes", 0),
        "graph_matched_edges": state.get("graph_matched_edges", 0),
        "graph_was_truncated": state.get("graph_was_truncated", False),
        "graph_truncation_reason": state.get("graph_truncation_reason", None),
        "graph_entity_state": state.get("graph_entity_state", "none"),
        "graph_relationship_state": state.get("graph_relationship_state", "none"),
    }

    from services.retrieval.evidence_contract import evidence_item_from_chunk
    retained_evidence_items = []
    ws_id = state.get("workspace_id", "")
    b_repo = state.get("boundary_repo")
    if b_repo is None:
        try:
            from db.workspace_boundary_repo import get_workspace_boundary_repo
            b_repo = get_workspace_boundary_repo()
        except Exception:
            b_repo = None
    okf_ids = {str(getattr(x, "id", "")) for x in okf_candidates}
    structural_ids = {str(getattr(x, "id", "")) for x in structural}
    graph_ids = {str(getattr(x, "id", "")) for x in graph}
    vector_ids = {str(getattr(x, "id", "")) for x in vector}
    bm25_ids = {str(getattr(x, "id", "")) for x in bm25}

    for c in top_chunks:
        strat = getattr(c, "strategy", None) or (c.metadata.get("strategy") if isinstance(getattr(c, "metadata", None), dict) else None)
        if not strat:
            cid = str(getattr(c, "id", ""))
            if cid in okf_ids:
                strat = "okf"
            elif cid in structural_ids:
                strat = "page_index"
            elif cid in graph_ids:
                strat = "knowledge_graph"
            elif cid in vector_ids:
                strat = "vector_rerank"
            elif cid in bm25_ids:
                strat = "bm25"
            else:
                strat = "vector_rerank"
        if isinstance(getattr(c, "metadata", None), dict):
            c.metadata["strategy"] = strat
        ev = evidence_item_from_chunk(
            chunk=c,
            workspace_id=ws_id,
            strategy=strat,
            score=getattr(c, "reranker_score", 0.0),
            boundary_repo=b_repo,
        )
        retained_evidence_items.append(ev)

    return {
        "top_chunks": top_chunks,
        "retained_evidence_items": retained_evidence_items,
        "retrieval_candidates": retrieval_candidates,
        "reranker_mode": reranker_mode,
        "reranker_telemetry": reranker_telemetry,
        "executed_path": "full",
        "stage_timings": {**existing, "reranker_ms": latency_ms},
    }


def run_deterministic_math(state: PipelineState):
    """Deterministic arithmetic execution node.
    Runs immediately after run_reranker on top_chunks.
    Decoupled from compressor and fidelity gating so exact math calculations
    are never blocked by semantic-similarity thresholds.
    """
    import os

    if os.getenv("ENABLE_DETERMINISTIC_MATH", "1") != "1" or state.get("force_full_path", False):
        return {}

    t0 = time.perf_counter()
    from services.retrieval.deterministic_executor import DeterministicExecutor
    from services.retrieval.response_builder import build_citation

    executor = DeterministicExecutor()
    chunks = state.get("top_chunks", [])
    if not chunks:
        chunks = state.get("candidate_chunks", [])

    res = executor.resolve_and_execute(state["query"], chunks)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    existing = state.get("stage_timings", {})

    if not res:
        from services.retrieval.sheet_inventory import resolve_workbook_sheet_status_answer
        ws_id = state.get("workspace_id", "")
        b_repo = state.get("boundary_repo")
        sheet_res = resolve_workbook_sheet_status_answer(
            state["query"], chunks, workspace_id=ws_id, boundary_repo=b_repo
        )
        if sheet_res:
            det_cits = [
                build_citation(c, workspace_id=ws_id, boundary_repo=b_repo).to_dict()
                for c in sheet_res["cited_chunks"]
            ]
            cands = dict(state.get("retrieval_candidates", {}))
            cands["compressed_context"] = [str(c.id) for c in chunks]
            cands["llm_citations"] = [str(c.get("chunk_id")) for c in det_cits if c.get("chunk_id")]
            return {
                "final_answer": sheet_res["answer"],
                "citations": det_cits,
                "confidence_score": 1.0,
                "faithfulness": 1.0,
                "citation_utilization_rate": round(len(det_cits) / max(1, len(chunks)), 4),
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "executed_path": "deterministic_sheet_inventory",
                "retrieval_candidates": cands,
                "stage_timings": {**existing, "deterministic_math_ms": (time.perf_counter() - t0) * 1000.0},
            }
        return {
            "stage_timings": {**existing, "deterministic_math_ms": latency_ms},
        }

    ans_str = res.format_answer(state["query"])

    # Map provenance citations to response builder citations from chunks (deduplicated)
    citations = []
    ws_id = state.get("workspace_id", "")
    b_repo = state.get("boundary_repo")
    chunk_by_id = {getattr(c, "id", ""): c for c in chunks}
    seen_cids: set[str] = set()
    for prov in res.provenance_citations:
        cid = prov.get("chunk_id") if isinstance(prov, dict) else getattr(prov, "chunk_id", "")
        if cid and cid in chunk_by_id and cid not in seen_cids:
            seen_cids.add(cid)
            citations.append(build_citation(chunk_by_id[cid], workspace_id=ws_id, boundary_repo=b_repo).to_dict())
    if not citations and chunks:
        citations = [build_citation(chunks[0], workspace_id=ws_id, boundary_repo=b_repo).to_dict()]

    logger.info(
        "deterministic_math.resolved query='%s' answer='%s' latency_ms=%.2f",
        state["query"][:50],
        ans_str[:50],
        latency_ms,
    )

    cands = dict(state.get("retrieval_candidates", {}))
    cands["compressed_context"] = [str(c.id) for c in chunks]
    cands["llm_citations"] = [str(c.get("chunk_id")) for c in citations if c.get("chunk_id")]

    return {
        "final_answer": ans_str,
        "citations": citations,
        "confidence_score": float(res.overall_confidence),
        "faithfulness": 1.0,
        "citation_utilization_rate": 1.0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "execution_result": res,
        "executed_path": "deterministic_math",
        "retrieval_candidates": cands,
        "stage_timings": {**existing, "deterministic_math_ms": latency_ms},
    }


def route_after_math(state: PipelineState):
    """If deterministic math produced a valid final answer, proceed directly to END.
    Otherwise fall back to the full generation path (run_compressor -> run_fidelity -> run_llm).
    """
    if state.get("final_answer") and state.get("final_answer") != "NOT_FOUND":
        return END
    return "run_compressor"


def run_compressor(state: PipelineState):
    t0 = time.perf_counter()
    chunks = state.get("top_chunks", [])
    compressed = compress_chunks(state["query"], chunks)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "compressor.latency_ms=%.2f compressor.output_len=%d",
        latency_ms,
        len(compressed),
    )
    existing = state.get("stage_timings", {})
    compressed_chunk_ids = [str(c.id) for c in chunks if f"[{c.id}]" in compressed]
    cands = dict(state.get("retrieval_candidates", {}))
    cands["compressed_context"] = compressed_chunk_ids
    return {
        "compressed_text": compressed,
        "context_snippet": compressed[:500],  # expose for LLM faithfulness judge
        "retrieval_candidates": cands,
        "stage_timings": {**existing, "compressor_ms": latency_ms},
    }


def run_fidelity(state: PipelineState):
    t0 = time.perf_counter()
    query = state.get("query", "")
    compressed = state.get("compressed_text", "")
    if not compressed:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        existing = state.get("stage_timings", {})
        return {
            "error": "No context available",
            "final_answer": "NOT_FOUND",
            "citations": [],
            "confidence_score": 0.0,
            "stage_timings": {**existing, "fidelity_ms": latency_ms},
        }
    try:
        check_fidelity(
            query,
            [compressed],
            query_embedding=state.get("query_embedding"),
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        existing = state.get("stage_timings", {})
        return {
            "error": None,
            "stage_timings": {**existing, "fidelity_ms": latency_ms},
        }
    except CoverageError as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.error("fidelity.failed reason=%s latency_ms=%.2f", str(e), latency_ms)
        existing = state.get("stage_timings", {})
        return {
            "error": str(e),
            "final_answer": "NOT_FOUND",
            "citations": [],
            "stage_timings": {**existing, "fidelity_ms": latency_ms},
        }


def run_llm(state: PipelineState):
    if state.get("error"):
        return {}

    calls = state.get("synthesis_calls", 0)
    if calls >= 1:
        logger.warning("run_llm.single_synthesis_limit_reached calls=%d, skipping call", calls)
        return {}

    t0 = time.perf_counter()
    compressed = state.get("compressed_text", "").strip()

    # Refuse safely when an explicitly requested source file is absent from the active workspace
    missing_sources = state.get("missing_required_sources") or []
    if missing_sources:
        return {
            "final_answer": "NOT_FOUND",
            "citations": [],
            "confidence_score": 0.0,
            "faithfulness": None,
            "citation_utilization_rate": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "executed_path": "full",
            "stage_timings": {**state.get("stage_timings", {}), "llm_ms": 0.0},
        }

    # M1 / L2: Early exit if context is empty
    if not compressed:
        return {
            "final_answer": "NOT_FOUND",
            "citations": [],
            "confidence_score": 0.0,
            "faithfulness": None,
            "citation_utilization_rate": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "executed_path": "full",
            "stage_timings": {**state.get("stage_timings", {}), "llm_ms": 0.0},
        }

    top_chunks = state.get("top_chunks", [])
    cands = dict(state.get("retrieval_candidates", {}))
    compressed_ids = set(cands.get("compressed_context", []))
    if not compressed_ids and compressed:
        compressed_ids = {str(c.id) for c in top_chunks if f"[{c.id}]" in compressed}
        cands["compressed_context"] = list(compressed_ids)
    retained_pool = [c for c in top_chunks if str(c.id) in compressed_ids] if compressed_ids else list(top_chunks)
    subgoal_specs_llm = state.get("subgoal_specs") or []
    snapshot_sources_llm = state.get("snapshot_sources") or {}

    avg_reranker = (
        sum((getattr(c, "reranker_score", 0.0) or 0.0) for c in top_chunks) / len(top_chunks)
        if top_chunks
        else 0.0
    )

    from services.retrieval.deterministic_executor import evaluate_two_operand_comparison
    det_eval = evaluate_two_operand_comparison(
        state.get("query", ""),
        subgoal_specs_llm,
        retained_pool,
        snapshot_sources_llm,
    )
    if det_eval.get("status") == "verified":
        det_ans = det_eval["answer"]
        det_chunks = det_eval["cited_chunks"]
        from services.retrieval.response_builder import build_citation
        ws_id = state.get("workspace_id", "")
        b_repo = state.get("boundary_repo")
        det_citations = [build_citation(c, workspace_id=ws_id, boundary_repo=b_repo).to_dict() for c in det_chunks]
        cands["llm_citations"] = [str(c.id) for c in det_chunks]
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "final_answer": det_ans,
            "citations": det_citations,
            "confidence_score": avg_reranker,
            "faithfulness": 1.0,
            "citation_utilization_rate": round(len(det_chunks) / len(top_chunks), 4) if top_chunks else None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "executed_path": "full",
            "status": "success",
            "retrieval_candidates": cands,
            "synthesis_calls": 0,
            "stage_timings": {**state.get("stage_timings", {}), "llm_ms": latency_ms},
        }
    if det_eval.get("status") == "unverified":
        cands["unverified_operands"] = det_eval.get("unverified_operands", [])
        logger.info("run_llm.unverified_comparison_refused unverified=%s", cands["unverified_operands"])
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "final_answer": "NOT_FOUND",
            "citations": [],
            "confidence_score": 0.0,
            "faithfulness": None,
            "citation_utilization_rate": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "executed_path": "full",
            "status": "insufficient_evidence",
            "retrieval_candidates": cands,
            "synthesis_calls": 0,
            "stage_timings": {**state.get("stage_timings", {}), "llm_ms": latency_ms},
        }

    response = call_llm(state["query"], compressed)

    # Real confidence score — avg reranker score
    confidence = avg_reranker

    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info("llm.latency_ms=%.2f llm.confidence=%.4f", latency_ms, confidence)
    existing = state.get("stage_timings", {})

    from services.retrieval.response_builder import build_citation
    from services.evaluation.benchmark_scorer import (
        compute_faithfulness,
        detect_contradiction_against_evidence,
        extract_numbers_normalized,
    )
    from services.retrieval.table_metric_binding import (
        bind_financial_query,
        cell_matches_financial_claim,
        extract_financial_claims,
        parse_financial_table_cells,
    )

    ans = response.get("answer", "NOT_FOUND")
    if ans is None or (isinstance(ans, str) and not ans.strip()):
        ans = "NOT_FOUND"
    usage = response.get("usage", {"input_tokens": 0, "output_tokens": 0})
    stop_reason = usage.get("stop_reason") if isinstance(usage, dict) else None
    out_tokens = int(usage.get("output_tokens", 0) or 0) if isinstance(usage, dict) else 0

    cands = dict(state.get("retrieval_candidates", {}))
    cands["llm_stop_reason"] = stop_reason
    cands["llm_output_tokens"] = out_tokens
    if stop_reason in ("max_tokens", "length"):
        logger.warning(
            "run_llm.truncated_response stop_reason=%s output_tokens=%d (refusing incomplete output)",
            stop_reason,
            out_tokens,
        )
        ans = "NOT_FOUND"
        cands["delivery_rejection_reason"] = f"truncated_stop_reason:{stop_reason}"
    compressed_ids = set(cands.get("compressed_context", []))
    if not compressed_ids and compressed:
        compressed_ids = {str(c.id) for c in top_chunks if f"[{c.id}]" in compressed}
        cands["compressed_context"] = list(compressed_ids)

    # Parse and validate citations directly from the LLM's JSON contract: {"answer": "...", "citations": ["chunk_id"]}
    fin_binding = state.get("financial_binding")
    is_financial = bool(fin_binding and getattr(fin_binding, "is_active", False))

    raw_citations = response.get("citations", [])
    if isinstance(raw_citations, list):
        valid_llm_citations = [
            str(cid) for cid in raw_citations
            if str(cid) in compressed_ids
        ]
    else:
        valid_llm_citations = []
    cands["llm_citations"] = valid_llm_citations

    # Select final citations: preferred LLM cited chunks, else compressed context chunks
    final_cited_chunks = []
    chunk_by_id = {str(c.id): c for c in top_chunks}
    for cid in valid_llm_citations:
        if cid in chunk_by_id:
            final_cited_chunks.append(chunk_by_id[cid])

    def _derived_ratio_numbers(ctx_nums: set[str]) -> set[str]:
        floats: list[float] = []
        for cn in ctx_nums:
            if len(cn) == 4 and cn.isdigit() and 1900 <= int(cn) <= 2099:
                continue
            try:
                floats.append(float(cn))
            except ValueError:
                pass
        out: set[str] = set()
        for a in floats:
            for b in floats:
                if b != 0:
                    out.add(str(round(a / b * 100.0, 2)).rstrip("0").rstrip("."))
                    out.add(str(round(a / b, 2)).rstrip("0").rstrip("."))
                out.add(str(round(a - b, 2)).rstrip("0").rstrip("."))
        return out

    has_sheet_inventory = any(getattr(c, "element_type", "") == "sheet_inventory" for c in top_chunks)
    subgoal_specs_llm = state.get("subgoal_specs") or []
    snapshot_sources_llm = state.get("snapshot_sources") or {}
    def _run_verified_claim_fallback(ans_val: Any) -> tuple[Any, list[Any]]:
        if is_financial or has_sheet_inventory:
            return ans_val, []
        ans_for_verify = json.dumps(ans_val) if isinstance(ans_val, (dict, list)) else str(ans_val)
        ans_for_verify = re.sub(r"\[[^\]]+\]", " ", ans_for_verify).strip()
        retained_pool_fb = [c for c in top_chunks if str(c.id) in compressed_ids]
        doc_nums_by_id: dict[str, set[str]] = {}
        for rc in retained_pool_fb:
            did = str(getattr(rc, "document_id", "") or "")
            doc_nums_by_id.setdefault(did, set()).update(extract_numbers_normalized(getattr(rc, "text", "") or ""))
        from services.retrieval.deterministic_executor import verify_two_operand_comparison
        _fb_cmp_ok, _, _fb_cmp_derived = verify_two_operand_comparison(
            state.get("query", ""),
            subgoal_specs_llm,
            retained_pool_fb,
            ans_for_verify,
        )
        sentences = [
            seg.strip()
            for seg in re.split(r"(?<=[.!?])\s+|\n+", ans_for_verify)
            if seg.strip()
        ] or [ans_for_verify]
        _stop = {
            "that", "this", "with", "from", "have", "been", "were", "only",
            "about", "does", "mean", "there", "their", "which", "when", "what",
            "where", "while", "into", "over", "under", "between", "because",
            "information", "context", "provided", "indicates", "data", "absence",
            "necessarily", "specific", "meanwhile",
        }
        claims: list[tuple[str, str | None]] = []
        prev_regime: str | None = None
        for sent in sentences:
            s_lower = sent.lower()
            has_inf = bool(re.search(r"\binference\b", s_lower))
            has_trn = bool(re.search(r"\b(?:training|pretraining)\b", s_lower))
            if has_inf and not has_trn:
                sent_regime: str | None = "inference"
            elif has_trn and not has_inf:
                sent_regime = "training"
            elif re.match(r"^(?:this|it|these|such)\b", s_lower):
                sent_regime = prev_regime
            else:
                sent_regime = None
            prev_regime = sent_regime

            parts = [
                p.strip()
                for p in re.split(
                    r";\s+|,\s+(?:and|while|whereas|but)\s+|\s+(?:and|while|whereas)\s+",
                    sent,
                )
                if p.strip()
            ]
            if len(parts) > 1 and all(
                len({w for w in re.findall(r"[a-z0-9]{4,}", p.lower()) if w not in _stop}) >= 2
                or bool(extract_numbers_normalized(p))
                for p in parts
            ):
                for p in parts:
                    claims.append((p, sent_regime))
            else:
                claims.append((sent, sent_regime))

        selected_by_id: dict[str, Any] = {}
        all_claims_supported = bool(retained_pool_fb)
        for claim, claim_regime in claims:
            claim_nums = extract_numbers_normalized(claim)
            claim_terms = {
                w for w in re.findall(r"[a-z0-9]{4,}", claim.lower())
                if w not in _stop
            }
            best_chunk = None
            best_key = (0, 0.0, 0.0)
            for cand in retained_pool_fb:
                cand_text = getattr(cand, "text", "") or ""
                cand_nums = extract_numbers_normalized(cand_text)
                if claim_nums and not (claim_nums <= cand_nums):
                    from services.evaluation.benchmark_scorer import _verify_cited_derived_calculations
                    same_doc_years = {
                        n for n in doc_nums_by_id.get(str(getattr(cand, "document_id", "") or ""), set())
                        if len(n) == 4 and n.isdigit() and 1900 <= int(n) <= 2099
                    }
                    derived_claim = _verify_cited_derived_calculations(
                        answer=claim,
                        ans_nums=claim_nums,
                        context_nums=cand_nums,
                        joined_context=cand_text,
                    ) | _derived_ratio_numbers(cand_nums) | (_fb_cmp_derived if _fb_cmp_ok else set()) | same_doc_years
                    if not ((claim_nums - derived_claim) <= cand_nums):
                        continue
                if detect_contradiction_against_evidence(answer=claim, context=cand_text):
                    continue
                c_lower = cand_text.lower()
                claim_lower = claim.lower()
                if not re.search(r"\b(?:not|never|no)\b", claim_lower) and any(
                    re.search(rf"\b{m.group(1)}\s+{re.escape(m.group(2))}\b", claim_lower)
                    for m in re.finditer(r"\b(is|are|was|were)\s+not\s+([a-z]{3,})\b", c_lower)
                ):
                    continue
                q_bind = bind_financial_query(state.get("query", ""))
                fin_claims = extract_financial_claims(claim)
                table_cells = (
                    parse_financial_table_cells(cand_text)
                    if fin_claims and getattr(cand, "element_type", "") != "TableRow"
                    else []
                )
                if fin_claims and table_cells:
                    derived_tbl = _derived_ratio_numbers(cand_nums)
                    all_cells_ok = all(
                        (str(fc.get("value") or "").rstrip("0").rstrip(".") in derived_tbl)
                        or any(
                            cell_matches_financial_claim(
                                cell,
                                fc,
                                target_metric=(q_bind.metric if q_bind and q_bind.is_active else None) or fc.get("metric"),
                                target_entity=(q_bind.entity if q_bind and q_bind.is_active else None) or fc.get("entity"),
                                required_duration=fc.get("duration_norm") or (q_bind.duration_norm if q_bind and q_bind.is_active else None),
                                required_date=fc.get("date_parsed") or (q_bind.date_parsed if q_bind and q_bind.is_active else None),
                                chunk_context=cand_text,
                            )
                            for cell in table_cells
                        )
                        for fc in fin_claims
                    )
                    if not all_cells_ok:
                        continue
                cand_lines = [ln.strip() for ln in cand_text.splitlines() if ln.strip()]
                support_text = cand_text
                if (
                    len(cand_lines) >= 2
                    and len(cand_lines[0]) <= 120
                    and not cand_lines[0].endswith((".", "!", "?"))
                ):
                    support_text = "\n".join(cand_lines[1:])
                support_lower = support_text.lower()
                if claim_regime in ("inference", "training") and claim_terms:
                    ev_sents = [
                        es.strip().lower()
                        for es in re.split(r"(?<=[.!?])\s+|\n+", support_text)
                        if es.strip()
                    ]
                    sent_scores = [
                        (sum(1 for t in claim_terms if re.search(rf"\b{re.escape(t)}\b", es)), es)
                        for es in ev_sents
                    ]
                    max_sent_hits = max((sc for sc, _ in sent_scores), default=0)
                    top_sents = [es for sc, es in sent_scores if sc == max_sent_hits and sc > 0]
                    if top_sents:
                        req_pat = (
                            r"\binference\b"
                            if claim_regime == "inference"
                            else r"\b(?:training|pretraining)\b"
                        )
                        opp_pat = (
                            r"\b(?:training|pretraining)\b"
                            if claim_regime == "inference"
                            else r"\binference\b"
                        )
                        if all(
                            re.search(opp_pat, es) and not re.search(req_pat, es)
                            for es in top_sents
                        ):
                            continue
                claim_faith = compute_faithfulness(claim, support_text) or 0.0
                content_hits = sum(1 for t in claim_terms if t in support_lower)
                stem_hits = sum(1 for t in claim_terms if len(t) >= 5 and t[:5] in support_lower)
                whole_faith = compute_faithfulness(ans_for_verify, support_text) or 0.0
                multi_non_num_ok = (
                    not claim_nums
                    and len(claims) > 1
                    and (content_hits >= 1 or stem_hits >= 1)
                )
                if ((claim_faith >= 0.25 or content_hits >= 2) and (content_hits >= 1 or claim_nums)) or multi_non_num_ok:
                    cand_key = (content_hits if (claim_faith >= 0.25 or content_hits >= 2) else 0, whole_faith, claim_faith)
                    if cand_key > best_key:
                        best_key = cand_key
                        best_chunk = cand
            if best_chunk is None:
                if (
                    not claim_nums
                    and len(claims) > 1
                    and re.match(r"^(?:no|yes)\b", claim.lower())
                    and len(claim_terms) <= 2
                ):
                    continue
                all_claims_supported = False
                break
            selected_by_id[str(best_chunk.id)] = best_chunk
        if all_claims_supported and selected_by_id:
            query_text = state.get("query", "") or ""
            query_entities = {
                w.lower()
                for w in re.findall(r"\b[A-Z][A-Za-z0-9-]{3,}\b", query_text)
                if w.lower() not in _stop and w.lower() not in {"does", "what", "when", "where", "which", "explain", "using", "both", "papers"}
            }
            answered_entities = {
                w.lower()
                for w in re.findall(r"\b[A-Z][A-Za-z0-9-]{3,}\b", ans_for_verify)
                if w.lower() in query_entities
            }
            selected_corpus = "\n".join(
                (getattr(c, "text", "") or "").lower()
                for c in selected_by_id.values()
            )
            if answered_entities and not all(ent in selected_corpus for ent in answered_entities):
                all_claims_supported = False
        if all_claims_supported and selected_by_id:
            return ans_val, [c for c in retained_pool_fb if str(c.id) in selected_by_id]
        return "NOT_FOUND", []

    if not final_cited_chunks and ans != "NOT_FOUND":
        if not is_financial and not has_sheet_inventory:
            ans, final_cited_chunks = _run_verified_claim_fallback(ans)
    elif final_cited_chunks and ans != "NOT_FOUND" and not is_financial and not has_sheet_inventory:
        # Recover missing supporting chunks from retained_pool when model citations omit a claim's evidence
        ans_check_str = json.dumps(ans) if isinstance(ans, (dict, list)) else str(ans)
        retained_pool_aug = [c for c in top_chunks if str(c.id) in compressed_ids]
        cited_ids_set = {str(c.id) for c in final_cited_chunks}
        _aug_stop = {
            "that", "this", "with", "from", "have", "been", "were", "only",
            "about", "does", "mean", "there", "their", "which", "when", "what",
            "where", "while", "into", "over", "under", "between", "because",
            "however", "specifically", "during", "using", "results", "cannot",
        }
        aug_sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+|;\s+|,\s+(?:and|while|whereas|but)\s+", ans_check_str) if s.strip()]
        for aug_s in aug_sents:
            aug_s_clean = re.sub(r"\[[^\]]+\]", " ", aug_s).strip()
            s_terms = {w for w in re.findall(r"[a-z0-9]{4,}", aug_s_clean.lower()) if w not in _aug_stop}
            if len(s_terms) < 2:
                continue
            max_cited_hits = max(
                (sum(1 for t in s_terms if t in (getattr(cc, "text", "") or "").lower()) for cc in final_cited_chunks),
                default=0,
            )
            if max_cited_hits >= len(s_terms):
                continue
            s_nums = extract_numbers_normalized(aug_s_clean)
            s_entities = {w.lower() for w in re.findall(r"\b[A-Za-z0-9]+-[A-Za-z0-9]+\b", aug_s_clean)}
            s_low = aug_s_clean.lower()
            best_extra = None
            best_extra_hits = max_cited_hits
            for cand in retained_pool_aug:
                if str(cand.id) in cited_ids_set:
                    continue
                c_txt = getattr(cand, "text", "") or ""
                c_low = c_txt.lower()
                if s_entities and not all(ent in c_low for ent in s_entities):
                    continue
                c_nums = extract_numbers_normalized(c_txt)
                if s_nums and not (s_nums <= c_nums):
                    continue
                if detect_contradiction_against_evidence(answer=aug_s_clean, context=c_txt):
                    continue
                if any(
                    re.search(rf"\b{m.group(1)}\s+{re.escape(m.group(2))}\b", s_low)
                    for m in re.finditer(r"\b(is|are|was|were)\s+not\s+([a-z]{3,})\b", c_low)
                ):
                    continue
                if re.search(r"\b(?:is|are|was|were)\s+not\s+([a-z]{3,})\b", s_low):
                    neg_words = [m.group(1) for m in re.finditer(r"\b(?:is|are|was|were)\s+not\s+([a-z]{3,})\b", s_low)]
                    if any(re.search(rf"\b(?:is|are|was|were)\s+{re.escape(nw)}\b", c_low) for nw in neg_words):
                        continue
                hits = sum(1 for t in s_terms if t in c_low)
                if hits > best_extra_hits and hits >= min(3, len(s_terms)):
                    best_extra_hits = hits
                    best_extra = cand
            if best_extra is not None:
                final_cited_chunks.append(best_extra)
                cited_ids_set.add(str(best_extra.id))

    if ans != "NOT_FOUND" and final_cited_chunks and len(subgoal_specs_llm) > 1:
        from services.evaluation.benchmark_scorer import _verify_cited_derived_calculations
        from services.retrieval.deterministic_executor import verify_two_operand_comparison

        ans_str_check = json.dumps(ans) if isinstance(ans, (dict, list)) else str(ans)
        for c in final_cited_chunks:
            ans_str_check = ans_str_check.replace(f"[{c.id}]", " ")
        cmp_ok, cmp_reason, cmp_derived = verify_two_operand_comparison(
            state.get("query", ""),
            subgoal_specs_llm,
            final_cited_chunks,
            ans_str_check,
        )
        joined_cited = "\n".join(getattr(c, "text", "") or "" for c in final_cited_chunks)
        ans_nums = extract_numbers_normalized(ans_str_check)
        cited_nums = extract_numbers_normalized(joined_cited)
        derived_nums = _verify_cited_derived_calculations(
            answer=ans_str_check,
            ans_nums=ans_nums,
            context_nums=cited_nums,
            joined_context=joined_cited,
        )
        ans_years = {n for n in ans_nums if len(n) == 4 and n.isdigit() and 1900 <= int(n) <= 2099}
        ans_amounts = ans_nums - ans_years
        ungrounded_nums = ans_amounts - cited_nums - derived_nums
        if ungrounded_nums:
            ungrounded_nums = ungrounded_nums - _derived_ratio_numbers(cited_nums) - cmp_derived

        reqs_ok = cmp_ok and (not ungrounded_nums)
        if reqs_ok:
            for sg in subgoal_specs_llm:
                q_low = str(sg.get("query_text") or "").lower()
                req_years = {y for y in re.findall(r"\b(19\d{2}|20\d{2})\b", q_low)}
                req_codes = re.findall(r"\b([A-Za-z]\d{1,4})\b", str(sg.get("query_text") or ""))
                matched_sg = False
                for c in final_cited_chunks:
                    c_txt = str(getattr(c, "text", "") or "")
                    c_low = c_txt.lower()
                    if req_years and not (req_years <= set(re.findall(r"\b(19\d{2}|20\d{2})\b", c_low))):
                        continue
                    if req_codes and not all(re.search(rf"\b{re.escape(code.lower())}\b", c_low) for code in req_codes):
                        continue
                    if "current ratio" in q_low and re.search(r"\bnot\s+current\s+ratio\b", c_low):
                        continue
                    if not (req_years or req_codes):
                        if re.match(
                            r"^(?:is\s+(?:that|this)\s+(?:correct|true|accurate|right|valid)|confirm\b|verify\b|explain\s+(?:whether|why|using)\b)",
                            q_low,
                        ):
                            matched_sg = True
                            break
                        # Require >=2 non-generic tokens from subgoal when >=2 exist
                        _sg_gen = {"that", "this", "with", "from", "what", "when", "where", "which", "while", "models", "model", "paper", "papers", "shows", "wants", "using", "both", "explain"}
                        sg_toks = {t for t in re.findall(r"[a-z0-9]{4,}", q_low) if t not in _sg_gen}
                        need_hits = min(2, len(sg_toks))
                        if sg_toks and sum(1 for t in sg_toks if t in c_low) < need_hits:
                            continue
                    matched_sg = True
                    break
                if not matched_sg:
                    reqs_ok = False
                    break

        if reqs_ok and snapshot_sources_llm:
            all_elig = {
                str(e).strip().lower()
                for sg in subgoal_specs_llm
                for e in (sg.get("eligible_sources") or [])
                if str(e).strip()
            }
            if len(all_elig) >= 2:
                cited_srcs = {
                    str(snapshot_sources_llm.get(str(getattr(c, "document_id", "") or ""), "") or "").lower()
                    for c in final_cited_chunks
                } - {""}
                if cited_srcs and not all(e in cited_srcs for e in all_elig):
                    reqs_ok = False

        if not reqs_ok:
            if cmp_ok and not is_financial and not has_sheet_inventory:
                fb_ans, fb_chunks = _run_verified_claim_fallback(ans)
                if fb_ans != "NOT_FOUND" and fb_chunks:
                    # Merge verified claim citations with valid model citations and re-check subgoal coverage
                    merged_by_id = {str(c.id): c for c in final_cited_chunks}
                    for fc in fb_chunks:
                        merged_by_id[str(fc.id)] = fc
                    final_cited_chunks = list(merged_by_id.values())
                    recheck_ok = True
                    for sg in subgoal_specs_llm:
                        q_low = str(sg.get("query_text") or "").lower()
                        if re.match(r"^(?:is\s+(?:that|this)\s+(?:correct|true|accurate|right|valid)|confirm\b|verify\b|explain\s+(?:whether|why|using)\b)", q_low):
                            continue
                        _sg_gen = {"that", "this", "with", "from", "what", "when", "where", "which", "while", "models", "model", "paper", "papers", "shows", "wants", "using", "both", "explain"}
                        sg_toks = {t for t in re.findall(r"[a-z0-9]{4,}", q_low) if t not in _sg_gen}
                        need_hits = min(2, len(sg_toks))
                        if sg_toks and not any(
                            sum(1 for t in sg_toks if t in (getattr(c, "text", "") or "").lower()) >= need_hits
                            for c in final_cited_chunks
                        ):
                            recheck_ok = False
                            break
                    reqs_ok = recheck_ok
            if not reqs_ok:
                ans = "NOT_FOUND"
                final_cited_chunks = []

    if ans != "NOT_FOUND" and final_cited_chunks:
        q_str = state.get("query", "") or ""
        ans_clean = re.sub(r"\[[^\]]+\]", " ", str(ans)).strip()
        if (
            re.search(r"\b(?:is\s+(?:it|that|this)\s+(?:correct|true|accurate)|correct\s*\?)", q_str, re.IGNORECASE)
            and re.search(r"\b\d+(?:\.\d+)?\b", q_str)
            and len(ans_clean.split()) <= 8
            and not extract_numbers_normalized(ans_clean)
        ):
            src_txt = (getattr(final_cited_chunks[0], "text", "") or "").strip()
            if src_txt:
                ans = f"{ans_clean.rstrip('.')}. {src_txt}"

    if ans == "NOT_FOUND" or not top_chunks:
        citation_utilization_rate = None
        faithfulness = None
        final_cited_chunks = []
    else:
        citation_utilization_rate = (
            round(len(final_cited_chunks) / len(top_chunks), 4) if top_chunks else None
        )
        ans_str = json.dumps(ans) if isinstance(ans, (dict, list)) else str(ans)
        faithfulness = compute_faithfulness(ans_str, compressed)

    ws_id = state.get("workspace_id", "")
    b_repo = state.get("boundary_repo")

    final_citations = [build_citation(c, workspace_id=ws_id, boundary_repo=b_repo).to_dict() for c in final_cited_chunks]
    if is_financial and raw_citations and not final_citations:
        for r_cid in raw_citations:
            if isinstance(r_cid, dict):
                final_citations.append(r_cid)
            else:
                final_citations.append({"chunk_id": str(r_cid), "document_id": None})

    return {
        "final_answer": ans,
        "citations": final_citations,
        "confidence_score": confidence,
        "faithfulness": faithfulness,
        "citation_utilization_rate": citation_utilization_rate,
        "usage": usage,
        "executed_path": "full",
        "status": "success",
        "retrieval_candidates": cands,
        "synthesis_calls": calls + 1,
        "stage_timings": {**existing, "llm_ms": latency_ms},
    }


def route_after_fidelity(state: PipelineState) -> str:
    """Conditional routing after evidence fidelity evaluation.

    - If fidelity failed (error or empty context) and escalation budget is available:
      routes to run_laya_escalation.
    - If fidelity failed and escalation is unavailable/exhausted:
      routes to delivery_gate (refusal).
    - If fidelity passed:
      evaluates verifier readiness if delivery gate is required. If unready, routes to delivery_gate;
      otherwise routes to run_llm for single answer synthesis.
    """
    err = state.get("error")
    if err:
        laya_enabled = state.get("laya_enabled", getattr(settings, "LAYA_ENABLED", False))
        expansion_steps = state.get("expansion_steps", 0)
        laya_calls = state.get("laya_calls", 0)
        pipe_start = state.get("_pipeline_start_time", time.perf_counter())
        elapsed_ms = (time.perf_counter() - pipe_start) * 1000.0
        remaining_ms = max(0.0, 15000.0 - elapsed_ms)
        caller_spec = state.get("caller_deadline_ms") or state.get("phase_deadline_ms")
        effective_rem_ms = min(remaining_ms, 2500.0)
        if caller_spec is not None:
            try:
                c_val = float(caller_spec)
                if c_val > 0.0:
                    effective_rem_ms = min(effective_rem_ms, c_val)
            except (ValueError, TypeError):
                pass
        existing_deadline_t = state.get("_escalation_deadline_t") or state.get("escalation_deadline_t")
        if existing_deadline_t is not None:
            effective_rem_ms = min(effective_rem_ms, max(0.0, (float(existing_deadline_t) - time.perf_counter()) * 1000.0))
        if laya_enabled and expansion_steps < 1 and laya_calls < 1 and effective_rem_ms >= 500.0:
            return "run_laya_escalation"
        return "delivery_gate"

    if state.get("escalation_executed") or state.get("require_delivery_gate"):
        verifier = state.get("verifier") or state.get("semantic_verifier")
        if verifier is None:
            return "delivery_gate"
        if hasattr(verifier, "is_ready") and not verifier.is_ready():
            return "delivery_gate"

    return "run_llm"


def run_laya_escalation(state: PipelineState) -> dict[str, Any]:
    """Execute bounded LAYA decision model and targeted lexical expansion.

    Enforces:
    - Single absolute escalation deadline covering CountTokens, admission, retry backoff,
      inference, expansion, and postprocessing.
    - Production resolution of boundary authority and pinned snapshot context (fails closed).
    - Strict chunk metadata provenance (rejects unverified chunks, missing versions, or version conflicts).
    - Single pass BM25 lexical expansion (other routes explicitly unavailable).
    - Merging novel chunks with baseline candidates.
    - Re reranking and recompression.
    - Clearing stale refusal state before secondary fidelity recheck.
    - Single synthesis call enforcement.
    - Delivery gate evaluation and fallback replacement validation before delivery.
    """
    t0 = time.perf_counter()
    existing_timings = state.get("stage_timings", {})
    cands = dict(state.get("retrieval_candidates", {}))
    ledger = state.get("laya_budget_ledger") or LayaBudgetLedger()

    pipe_start = state.get("_pipeline_start_time", t0)
    elapsed_query_ms = (t0 - pipe_start) * 1000.0
    remaining_query_ms = max(0.0, 15000.0 - elapsed_query_ms)

    # Persist one absolute monotonic escalation deadline in pipeline state.
    # Derive it from the minimum of remaining query time, the 2500ms phase ceiling,
    # and any valid tighter caller limit.
    existing_deadline_t = state.get("_escalation_deadline_t") or state.get("escalation_deadline_t")
    if existing_deadline_t is not None:
        escalation_deadline_t = float(existing_deadline_t)
        phase_deadline_ms = max(0.0, (escalation_deadline_t - t0) * 1000.0)
    else:
        limits = [remaining_query_ms, 2500.0]
        caller_spec = state.get("caller_deadline_ms") or state.get("phase_deadline_ms")
        if caller_spec is not None:
            try:
                c_val = float(caller_spec)
                if c_val > 0.0:
                    limits.append(c_val)
            except (ValueError, TypeError):
                pass
        phase_deadline_ms = min(limits)
        escalation_deadline_t = t0 + (phase_deadline_ms / 1000.0)

    try:
        from services.retrieval.laya_concurrency import get_laya_concurrency_controller
    except ImportError:
        from src.services.retrieval.laya_concurrency import get_laya_concurrency_controller
    ctrl = state.get("laya_concurrency_controller") or get_laya_concurrency_controller()

    ws_id = state.get("workspace_id", "ws_default")

    # Resolve boundary repository through production path
    boundary_repo = state.get("boundary_repo")
    if boundary_repo is None:
        try:
            from db.workspace_boundary_repo import get_workspace_boundary_repo
            boundary_repo = get_workspace_boundary_repo()
        except Exception as bre:
            logger.error("laya.boundary_repo_resolution_failed: %s (failing closed)", bre)
            boundary_repo = None

    if boundary_repo is None:
        logger.error("laya.missing_boundary_authority: failing closed")
        return {
            "final_answer": "NOT_FOUND",
            "citations": [],
            "confidence_score": 0.0,
            "retrieval_incomplete": True,
            "error": "Missing boundary authority",
            "_escalation_deadline_t": escalation_deadline_t,
            "escalation_deadline_t": escalation_deadline_t,
            "escalation_executed": True,
        }

    # Evaluate dynamic workspace tombstone early
    try:
        if hasattr(boundary_repo, "has_tombstone") and boundary_repo.has_tombstone(ws_id, "workspace", ws_id):
            logger.warning("laya.workspace_tombstoned ws_id=%s (failing closed)", ws_id)
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "error": "Workspace tombstoned",
                "_escalation_deadline_t": escalation_deadline_t,
                "escalation_deadline_t": escalation_deadline_t,
                "escalation_executed": True,
            }
    except Exception as te:
        logger.error("laya.workspace_tombstone_check_error: %s (failing closed)", te)
        return {
            "final_answer": "NOT_FOUND",
            "citations": [],
            "confidence_score": 0.0,
            "retrieval_incomplete": True,
            "error": f"Tombstone check error: {te}",
            "_escalation_deadline_t": escalation_deadline_t,
            "escalation_deadline_t": escalation_deadline_t,
            "escalation_executed": True,
        }

    # Resolve pinned snapshot context through production path
    snapshot_context = state.get("snapshot_context")
    if snapshot_context is None:
        try:
            from db.knowledge_graph_repo import get_knowledge_graph_repository, build_pinned_snapshot_context
            kg_repo = state.get("knowledge_graph_repo")
            if kg_repo is None and getattr(settings, "GRAPH_ENABLED", False):
                try:
                    kg_repo = get_knowledge_graph_repository()
                except Exception:
                    kg_repo = None
            snapshot_context = build_pinned_snapshot_context(
                workspace_id=ws_id,
                boundary_repo=boundary_repo,
                kg_repo=kg_repo,
                require_kg_manifest=False,
            )
        except Exception as ske:
            logger.error("laya.snapshot_context_build_failed: %s (failing closed)", ske)
            snapshot_context = None

    if snapshot_context is None or not hasattr(snapshot_context, "get_document"):
        logger.error("laya.missing_snapshot_authority: failing closed")
        return {
            "final_answer": "NOT_FOUND",
            "citations": [],
            "confidence_score": 0.0,
            "retrieval_incomplete": True,
            "error": "Missing or failed snapshot authority",
            "_escalation_deadline_t": escalation_deadline_t,
            "escalation_deadline_t": escalation_deadline_t,
            "escalation_executed": True,
        }

    err = state.get("error") or "Context insufficient"
    compressed = state.get("compressed_text", "")

    # Derive failed stage from state or context failure mode
    failed_stage_recorded = state.get("failed_stage")
    if failed_stage_recorded in (
        "run_fidelity_coverage",
        "run_fidelity_empty_context",
        "run_deterministic_math_operands",
    ):
        failed_stage = failed_stage_recorded
    elif not compressed:
        failed_stage = "run_fidelity_empty_context"
    elif "math" in str(err).lower() or "operand" in str(err).lower():
        failed_stage = "run_deterministic_math_operands"
    else:
        failed_stage = "run_fidelity_coverage"

    # Derive missing dimension from failed stage and error indicators
    err_str = str(err).lower()
    if failed_stage == "run_fidelity_empty_context":
        missing_dimension = "semantic_concept"
    elif failed_stage == "run_deterministic_math_operands":
        missing_dimension = "tabular_operand"
    elif "location" in err_str or "page" in err_str or "section" in err_str:
        missing_dimension = "location"
    elif "relationship" in err_str or "entity" in err_str or "connect" in err_str:
        missing_dimension = "relationship"
    elif "table" in err_str or "operand" in err_str or "calc" in err_str:
        missing_dimension = "tabular_operand"
    else:
        # Slice 5.1 default fallback; Slice 5.2 thickens routing across dense, structural, structured, and graph gap dimensions
        missing_dimension = "exact_term"

    from services.retrieval.response_builder import build_citation

    req_desc = MissingRequirementDescription(
        requirement_id=f"req_{uuid.uuid4().hex[:8]}",
        missing_dimension=missing_dimension,
        description=f"Evidence gap detected during {failed_stage}",
        failed_stage=failed_stage,
        detected_gap=str(err)[:300],
    )

    baseline_chunks = list(state.get("top_chunks") or state.get("candidate_chunks", []))
    summaries: list[BaselineEvidenceSummary] = []
    for c in baseline_chunks[:10]:
        is_prov_valid, c_ws, c_doc, c_ver, prov_reason = validate_chunk_provenance(
            c,
            expected_workspace_id=ws_id,
            snapshot_context=snapshot_context,
        )
        if not is_prov_valid:
            logger.info("laya.baseline_chunk_provenance_rejected chunk_id=%s reason=%s", getattr(c, "id", None), prov_reason)
            continue

        c_score = float(min(100.0, max(0.0, getattr(c, "similarity_score", 0.0) or getattr(c, "reranker_score", 0.0) or 1.0)))
        summaries.append(
            BaselineEvidenceSummary(
                chunk_id=str(c.id)[:64],
                document_id=c_doc[:128],
                document_version=c_ver[:64],
                workspace_id=ws_id,
                snippet=str(getattr(c, "text", ""))[:300],
                score=c_score,
                source_kind="text",
                page_number=getattr(c, "page_number", None) if (isinstance(getattr(c, "page_number", None), int) and 1 <= getattr(c, "page_number", None) <= 10000) else None,
                bbox=getattr(c, "bounding_box", None) if (getattr(c, "bounding_box", None) and len(getattr(c, "bounding_box", None)) == 4) else None,
            )
        )

    escalation_req = LayaEscalationRequest(
        query_id=f"q_{uuid.uuid4().hex[:12]}",
        workspace_id=ws_id,
        query=state.get("query", ""),
        missing_requirement=req_desc,
        available_routes=("lexical_expansion", "none"),
        retrieved_summary=tuple(summaries),
        caller_deadline_ms=max(0.0, (escalation_deadline_t - time.perf_counter()) * 1000.0),
    )

    rem_ms_decide = max(0.0, (escalation_deadline_t - time.perf_counter()) * 1000.0)
    service = state.get("laya_decision_service") or LayaDecisionService(
        client=state.get("laya_client"),
        model_id=state.get("laya_model_id"),
        concurrency_controller=ctrl,
        endpoint_url=state.get("laya_endpoint_url"),
        api_key=state.get("laya_api_key"),
    )
    decision = service.decide(escalation_req, rem_ms_decide, ledger)
    l_lat_ms = (time.perf_counter() - t0) * 1000.0

    cands["laya_decision_id"] = decision.decision_id
    cands["laya_route"] = decision.route
    cands["laya_confidence"] = decision.confidence

    novel_chunks: list[Chunk] = []
    no_progress = True
    status = decision.execution_status

    rem_s_expansion = max(0.0, escalation_deadline_t - time.perf_counter())
    if rem_s_expansion < 0.050:
        no_progress = True
        status = "timeout"
    elif decision.route == "lexical_expansion":
        executor = state.get("laya_expansion_executor") or LayaExpansionExecutor()

        def _do_expansion() -> tuple[list[Chunk], bool, str]:
            return executor.execute(
                decision=decision,
                workspace_id=ws_id,
                baseline_chunks=state.get("candidate_chunks", []),
                boundary_repo=boundary_repo,
                chunk_repo=state.get("doc_repo"),
                snapshot_context=snapshot_context,
                timeout_s=rem_s_expansion,
            )

        exp_res, exp_status = ctrl.run_provider_call(_do_expansion, timeout_s=rem_s_expansion)
        if exp_status == "completed" and exp_res is not None:
            novel_chunks, no_progress, status = exp_res
        elif exp_status == "caller_timeout":
            novel_chunks, no_progress, status = [], True, "timeout"
        elif exp_status == "admission_rejected":
            novel_chunks, no_progress, status = [], True, "admission_rejected"
        else:
            novel_chunks, no_progress, status = [], True, "error"

    exp_executed = bool(novel_chunks and not no_progress)
    cands["laya_novel_chunks"] = [str(c.id) for c in novel_chunks]
    cands["laya_no_progress"] = no_progress
    cands["laya_configured"] = bool(state.get("laya_enabled", getattr(settings, "LAYA_ENABLED", False)))
    cands["laya_enabled"] = bool(state.get("laya_enabled", getattr(settings, "LAYA_ENABLED", False)))
    cands["laya_dispatched"] = bool(getattr(service, "last_dispatched", False))
    cands["laya_invoked"] = bool(getattr(service, "last_dispatched", False))
    cands["laya_valid_response"] = bool(getattr(service, "last_valid_response", False))
    cands["laya_provider_success"] = bool(getattr(service, "last_valid_response", False))
    cands["laya_route_accepted"] = bool(getattr(service, "last_route_accepted", False) and decision.execution_status == "approved")
    cands["laya_expansion_executed"] = exp_executed
    cands["laya_fallback"] = bool(getattr(service, "last_fallback", False) or decision.execution_status != "approved")
    cands["laya_status"] = status
    cands["laya_provider"] = "endpoint" if getattr(service, "endpoint_url", None) else ("bedrock" if getattr(service, "client", None) else "mock")

    updated_state: dict[str, Any] = {
        "laya_calls": ledger.laya_calls,
        "expansion_steps": ledger.expansion_steps,
        "synthesis_calls": ledger.synthesis_calls,
        "laya_decision": decision.model_dump(),
        "laya_status": status,
        "laya_latency_ms": l_lat_ms,
        "laya_tokens": {
            "input_tokens": ledger.total_input_tokens,
            "output_tokens": ledger.total_output_tokens,
        },
        "retrieval_candidates": cands,
        "stage_timings": {**existing_timings, "laya_ms": l_lat_ms},
        "_escalation_start_time": t0,
        "_escalation_deadline_t": escalation_deadline_t,
        "escalation_deadline_t": escalation_deadline_t,
        "escalation_executed": True,
    }

    rem_s_post = max(0.0, escalation_deadline_t - time.perf_counter())
    if rem_s_post < 0.050 and novel_chunks:
        no_progress = True
        status = "timeout"
        novel_chunks = []
        updated_state["laya_status"] = "timeout"

    if novel_chunks and not no_progress:
        # Note on design choice for Slice 5.1:
        # Post expansion reranking, recompression, fidelity rechecking, and single synthesis
        # are executed as an inline sequence of pure state transformer calls within this node.
        # This matches the bounded specification requirement for immediate deterministic resolution.
        # Slice 5.2 may restructure this flow as an explicit LangGraph subgraph.

        # 1. Pool novel chunks with baseline candidates
        merged_candidates = list(state.get("candidate_chunks", [])) + novel_chunks
        current_state = {**state, **updated_state, "candidate_chunks": merged_candidates}

        # 2. Rerank updated candidate pool under deadline protection
        rem_s_rerank = max(0.0, escalation_deadline_t - time.perf_counter())
        if rem_s_rerank < 0.050:
            logger.warning("laya.rerank_deadline_insufficient failing closed with timeout refusal")
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "laya_status": "timeout",
                "error": "Caller deadline exceeded before post-expansion reranking",
                "stage_timings": {**existing_timings, "laya_ms": (time.perf_counter() - t0) * 1000.0},
                "_escalation_deadline_t": escalation_deadline_t,
                "escalation_deadline_t": escalation_deadline_t,
                "escalation_executed": True,
            }

        def _do_rerank() -> dict[str, Any]:
            return run_reranker(current_state)

        rerank_res, rerank_status = ctrl.run_provider_call(_do_rerank, timeout_s=rem_s_rerank)
        if rerank_status == "caller_timeout":
            logger.warning("laya.rerank_caller_timeout failing closed with timeout refusal")
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "laya_status": "timeout",
                "error": "Caller deadline exceeded during post-expansion reranking",
                "stage_timings": {**existing_timings, "laya_ms": (time.perf_counter() - t0) * 1000.0},
                "_escalation_deadline_t": escalation_deadline_t,
                "escalation_deadline_t": escalation_deadline_t,
                "escalation_executed": True,
            }
        elif rerank_status != "completed" or rerank_res is None:
            logger.warning("laya.rerank_failed failing closed with error refusal")
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "laya_status": "error",
                "error": "Post-expansion reranking failed",
                "stage_timings": {**existing_timings, "laya_ms": (time.perf_counter() - t0) * 1000.0},
                "_escalation_deadline_t": escalation_deadline_t,
                "escalation_deadline_t": escalation_deadline_t,
                "escalation_executed": True,
            }

        current_state.update(rerank_res)

        # 3. Recompress top chunks under deadline protection
        rem_s_comp = max(0.0, escalation_deadline_t - time.perf_counter())
        if rem_s_comp < 0.050:
            logger.warning("laya.compression_deadline_insufficient failing closed with timeout refusal")
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "laya_status": "timeout",
                "error": "Caller deadline exceeded before context compression",
                "stage_timings": {**existing_timings, "laya_ms": (time.perf_counter() - t0) * 1000.0},
                "_escalation_deadline_t": escalation_deadline_t,
                "escalation_deadline_t": escalation_deadline_t,
                "escalation_executed": True,
            }

        def _do_compress() -> dict[str, Any]:
            return run_compressor(current_state)

        comp_res, comp_status = ctrl.run_provider_call(_do_compress, timeout_s=rem_s_comp)
        if comp_status == "caller_timeout":
            logger.warning("laya.compression_caller_timeout failing closed with timeout refusal")
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "laya_status": "timeout",
                "error": "Caller deadline exceeded during context compression",
                "stage_timings": {**existing_timings, "laya_ms": (time.perf_counter() - t0) * 1000.0},
                "_escalation_deadline_t": escalation_deadline_t,
                "escalation_deadline_t": escalation_deadline_t,
                "escalation_executed": True,
            }
        elif comp_status != "completed" or comp_res is None:
            logger.warning("laya.compression_failed failing closed with error refusal")
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "laya_status": "error",
                "error": "Post-expansion context compression failed",
                "stage_timings": {**existing_timings, "laya_ms": (time.perf_counter() - t0) * 1000.0},
                "_escalation_deadline_t": escalation_deadline_t,
                "escalation_deadline_t": escalation_deadline_t,
                "escalation_executed": True,
            }

        current_state.update(comp_res)

        # 4. Clear stale refusal state before secondary check
        current_state["error"] = None
        current_state["final_answer"] = None
        current_state["citations"] = []

        # 5. Secondary fidelity check under deadline protection
        rem_s_fid = max(0.0, escalation_deadline_t - time.perf_counter())
        if rem_s_fid < 0.050:
            logger.warning("laya.fidelity_deadline_insufficient failing closed with timeout refusal")
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "laya_status": "timeout",
                "error": "Caller deadline exceeded before secondary fidelity check",
                "stage_timings": {**existing_timings, "laya_ms": (time.perf_counter() - t0) * 1000.0},
                "_escalation_deadline_t": escalation_deadline_t,
                "escalation_deadline_t": escalation_deadline_t,
                "escalation_executed": True,
            }

        def _do_fidelity() -> dict[str, Any]:
            return run_fidelity(current_state)

        fid_res, fid_status = ctrl.run_provider_call(_do_fidelity, timeout_s=rem_s_fid)
        if fid_status == "caller_timeout":
            logger.warning("laya.fidelity_caller_timeout failing closed with timeout refusal")
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "laya_status": "timeout",
                "error": "Caller deadline exceeded during secondary fidelity check",
                "stage_timings": {**existing_timings, "laya_ms": (time.perf_counter() - t0) * 1000.0},
                "_escalation_deadline_t": escalation_deadline_t,
                "escalation_deadline_t": escalation_deadline_t,
                "escalation_executed": True,
            }
        elif fid_status != "completed" or fid_res is None:
            logger.warning("laya.fidelity_failed failing closed with error refusal")
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "laya_status": "error",
                "error": "Secondary fidelity check failed",
                "stage_timings": {**existing_timings, "laya_ms": (time.perf_counter() - t0) * 1000.0},
                "_escalation_deadline_t": escalation_deadline_t,
                "escalation_deadline_t": escalation_deadline_t,
                "escalation_executed": True,
            }

        current_state.update(fid_res)

        updated_state.update(rerank_res)
        updated_state.update(comp_res)
        updated_state["retrieval_candidates"] = {**updated_state.get("retrieval_candidates", {}), **cands}
        updated_state["candidate_chunks"] = merged_candidates
        updated_state["escalation_executed"] = True
        updated_state["_escalation_deadline_t"] = escalation_deadline_t
        updated_state["escalation_deadline_t"] = escalation_deadline_t

        if current_state.get("error") is None:
            # Secondary fidelity succeeded!
            updated_state["error"] = None
            verifier = state.get("verifier") or state.get("semantic_verifier")
            if verifier is not None and (not hasattr(verifier, "is_ready") or verifier.is_ready()):
                # Run single synthesis call under deadline protection
                rem_s_synthesis = max(0.0, escalation_deadline_t - time.perf_counter())
                if rem_s_synthesis < 0.050:
                    updated_state["final_answer"] = "NOT_FOUND"
                    updated_state["citations"] = []
                    updated_state["confidence_score"] = 0.0
                    updated_state["retrieval_incomplete"] = True
                    updated_state["error"] = "Caller deadline exceeded before synthesis"
                    updated_state["laya_status"] = "timeout"
                else:
                    synth_calls = ledger.record_synthesis_call() if hasattr(ledger, "record_synthesis_call") else (ledger.synthesis_calls + 1)
                    updated_state["synthesis_calls"] = synth_calls

                    def _spend_reconcile_synthesis(usage: dict[str, Any], latency_ms: float, status: str) -> None:
                        in_tok = (usage.get("input_tokens") or usage.get("inputTokens", 0)) if isinstance(usage, dict) else 0
                        out_tok = (usage.get("output_tokens") or usage.get("outputTokens", 0)) if isinstance(usage, dict) else 0
                        is_late = status != "completed"
                        ledger.settle_spend(in_tok, out_tok, latency_ms, is_late=is_late, status=status)

                    def _do_synthesis() -> dict[str, Any]:
                        return run_llm(current_state)

                    llm_res, llm_status = ctrl.run_provider_call(
                        _do_synthesis,
                        timeout_s=rem_s_synthesis,
                        on_spend_reconcile=_spend_reconcile_synthesis,
                    )
                    if llm_status == "caller_timeout":
                        logger.warning("laya.synthesis_caller_timeout failing closed with timeout refusal")
                        updated_state["final_answer"] = "NOT_FOUND"
                        updated_state["citations"] = []
                        updated_state["confidence_score"] = 0.0
                        updated_state["retrieval_incomplete"] = True
                        updated_state["error"] = "Caller deadline exceeded during synthesis"
                        updated_state["laya_status"] = "timeout"
                    elif llm_status != "completed" or llm_res is None:
                        logger.warning("laya.synthesis_failed failing closed with error refusal")
                        updated_state["final_answer"] = "NOT_FOUND"
                        updated_state["citations"] = []
                        updated_state["confidence_score"] = 0.0
                        updated_state["retrieval_incomplete"] = True
                        updated_state["error"] = "Answer synthesis failed"
                        updated_state["laya_status"] = "error"
                    else:
                        updated_state.update(llm_res)
            else:
                # Verifier is unconfigured / unavailable -> free form delivery blocked!
                from services.retrieval.extractor import extract_verified_fact
                v_ans, v_chunk = extract_verified_fact(current_state["query"], current_state.get("top_chunks", []))
                tombstoned = False
                if v_ans and v_chunk:
                    is_prov_valid, p_ws, p_doc, p_ver, p_reason = validate_chunk_provenance(
                        v_chunk,
                        expected_workspace_id=ws_id,
                        snapshot_context=snapshot_context,
                    )
                    if not is_prov_valid:
                        tombstoned = True
                    elif not boundary_repo or not hasattr(boundary_repo, "has_tombstone"):
                        tombstoned = True
                    else:
                        try:
                            if boundary_repo.has_tombstone(ws_id, "workspace", ws_id):
                                tombstoned = True
                            elif p_doc and boundary_repo.has_tombstone(ws_id, "source", str(p_doc)):
                                tombstoned = True
                        except Exception as te:
                            logger.error("laya.fallback_tombstone_error: %s (failing closed)", te)
                            tombstoned = True

                if v_ans and v_chunk and not tombstoned:
                    updated_state["final_answer"] = v_ans
                    updated_state["citations"] = [build_citation(v_chunk, workspace_id=ws_id, boundary_repo=boundary_repo).to_dict()]
                    updated_state["executed_path"] = "deterministic_excerpt"
                    updated_state["confidence_score"] = 0.8
                else:
                    updated_state["final_answer"] = "NOT_FOUND"
                    updated_state["citations"] = []
                    updated_state["confidence_score"] = 0.0
                    updated_state["retrieval_incomplete"] = True
        else:
            # Secondary fidelity failed again
            updated_state["final_answer"] = "NOT_FOUND"
            updated_state["citations"] = []
            updated_state["confidence_score"] = 0.0
            updated_state["retrieval_incomplete"] = True
    else:
        # No progress, route unavailable, or none
        updated_state["final_answer"] = "NOT_FOUND"
        updated_state["citations"] = []
        updated_state["confidence_score"] = 0.0
        updated_state["retrieval_incomplete"] = True

    return updated_state


def delivery_gate(state: PipelineState) -> dict[str, Any]:
    """Authoritative final answer delivery gate.

    Enforces:
    - Re checks dynamic workspace and document tombstones before delivery (fail closed).
    - Free form synthesized prose is never delivered without passing an authoritative semantic verifier.
    - If verifier is unconfigured or unavailable, delivery is blocked in favor of
      approved deterministic output or structured refusal.
    - Validates final selected evidence and citations after any fallback replacement.
      Missing boundary authority or tombstone errors suppress delivery.
    """
    ws_id = state.get("workspace_id", "")
    boundary_repo = state.get("boundary_repo")
    if boundary_repo is None:
        try:
            from db.workspace_boundary_repo import get_workspace_boundary_repo
            boundary_repo = get_workspace_boundary_repo()
        except Exception as bre:
            logger.error("delivery_gate.boundary_repo_resolution_failed: %s", bre)
            boundary_repo = None

    def _validate_replacement_excerpt(v_chunk: Any, ws_id: str, b_repo: Any, snap_ctx: Any | None = None) -> bool:
        if v_chunk is None or b_repo is None or not hasattr(b_repo, "has_tombstone"):
            return False
        is_prov_valid, p_ws, p_doc, p_ver, _ = validate_chunk_provenance(v_chunk, ws_id, snap_ctx)
        if not is_prov_valid:
            return False
        try:
            if b_repo.has_tombstone(ws_id, "workspace", ws_id):
                return False
            if p_doc and b_repo.has_tombstone(ws_id, "source", str(p_doc)):
                return False
        except Exception:
            return False
        return True

    # 1. Dynamic tombstone check before delivery (fail closed on tombstone, lookup error, or missing/incompatible authority)
    if boundary_repo is None or not hasattr(boundary_repo, "has_tombstone") or not callable(getattr(boundary_repo, "has_tombstone", None)):
        logger.warning("delivery_gate.boundary_repo_unavailable_or_incompatible (delivery blocked)")
        return {
            "final_answer": "NOT_FOUND",
            "citations": [],
            "confidence_score": 0.0,
            "retrieval_incomplete": True,
            "retained_evidence_items": [],
            "top_chunks": [],
        }
    try:
        if boundary_repo.has_tombstone(ws_id, "workspace", ws_id):
            logger.warning("delivery_gate.workspace_tombstoned ws_id=%s (delivery blocked)", ws_id)
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "retained_evidence_items": [],
                "top_chunks": [],
            }
        for cit in state.get("citations", []):
            doc_id = cit.get("document_id") if isinstance(cit, dict) else getattr(cit, "document_id", None)
            if doc_id and boundary_repo.has_tombstone(ws_id, "source", str(doc_id)):
                logger.warning("delivery_gate.source_tombstoned doc_id=%s (delivery blocked)", doc_id)
                return {
                    "final_answer": "NOT_FOUND",
                    "citations": [],
                    "confidence_score": 0.0,
                    "retrieval_incomplete": True,
                    "retained_evidence_items": [],
                    "top_chunks": [],
                }
        for ev in state.get("retained_evidence_items", []):
            doc_id = getattr(ev, "document_id", None) or (getattr(ev, "locator", {}) or {}).get("document_id")
            if doc_id and boundary_repo.has_tombstone(ws_id, "source", str(doc_id)):
                logger.warning("delivery_gate.source_tombstoned doc_id=%s (delivery blocked)", doc_id)
                return {
                    "final_answer": "NOT_FOUND",
                    "citations": [],
                    "confidence_score": 0.0,
                    "retrieval_incomplete": True,
                    "retained_evidence_items": [],
                    "top_chunks": [],
                }
    except Exception as te:
        logger.error("delivery_gate.tombstone_check_error: %s (delivery blocked)", te)
        return {
            "final_answer": "NOT_FOUND",
            "citations": [],
            "confidence_score": 0.0,
            "retrieval_incomplete": True,
            "retained_evidence_items": [],
            "top_chunks": [],
        }

    # 2. Financial table metric binding verification (Spec 0004 / Q193)
    fin_binding = state.get("financial_binding")
    ans = state.get("final_answer", "")
    if fin_binding and getattr(fin_binding, "is_active", False) and ans and ans != "NOT_FOUND":
        from services.retrieval.table_metric_binding import verify_financial_cell_binding

        citations = state.get("citations", [])
        if not citations:
            logger.warning("delivery_gate.financial_metric_binding_rejected reason=empty_citations (delivery blocked)")
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "retained_evidence_items": [],
                "top_chunks": [],
            }

        top_chunks = state.get("top_chunks", [])
        retained_items = state.get("retained_evidence_items", [])
        auth_chunk_by_id = {str(getattr(c, "id", "")): c for c in top_chunks}
        for ev in retained_items:
            loc = getattr(ev, "locator", {}) or {}
            loc_cid = loc.get("chunk_id")
            if loc_cid and str(loc_cid) not in auth_chunk_by_id and hasattr(ev, "chunk") and getattr(ev, "chunk", None):
                auth_chunk_by_id[str(loc_cid)] = ev.chunk

        # Every citation MUST resolve to authoritative retained evidence.
        # Citation-supplied text must not establish evidence independently.
        cited_chunks = []
        for cit in citations:
            cid = cit.get("chunk_id") if isinstance(cit, dict) else (getattr(cit, "chunk_id", None) or getattr(cit, "id", None))
            if not cid or str(cid) not in auth_chunk_by_id:
                logger.warning(
                    "delivery_gate.financial_metric_binding_rejected reason=unresolved_citation_id cid=%s (delivery blocked)",
                    cid,
                )
                return {
                    "final_answer": "NOT_FOUND",
                    "citations": [],
                    "confidence_score": 0.0,
                    "retrieval_incomplete": True,
                    "retained_evidence_items": [],
                    "top_chunks": [],
                }
            cited_chunks.append(auth_chunk_by_id[str(cid)])

        verified, rejection_reason = verify_financial_cell_binding(cited_chunks, ans, fin_binding)

        if (
            not verified
            and (
                rejection_reason == "no_cells_found_in_chunk"
                or (state.get("executed_path") == "deterministic_excerpt" and rejection_reason in ("missing_period_date_in_table", "missing_period_duration_in_table"))
            )
            and not any(str(getattr(c, "element_type", "")).lower() in ("table", "tablerow") for c in cited_chunks)
            and not any("|" in (getattr(c, "text", "") or "") for c in cited_chunks)
        ):
            from services.evaluation.benchmark_scorer import extract_numbers_normalized
            from services.retrieval.table_metric_binding import bind_financial_query, classify_metric, extract_financial_claims

            cited_text = "\n".join((getattr(c, "text", "") or "") for c in cited_chunks)
            ans_nums = extract_numbers_normalized(ans)
            cited_nums = extract_numbers_normalized(cited_text)
            nums_ok = bool(ans_nums) and (ans_nums <= cited_nums)

            expected_metrics = set(getattr(fin_binding, "metrics", ()) or ())
            if getattr(fin_binding, "metric", None):
                expected_metrics.add(fin_binding.metric)
            ans_claims = extract_financial_claims(ans)
            metric_ok = True
            if expected_metrics:
                ans_metrics = {cl.get("metric") for cl in ans_claims if cl.get("metric")}
                cited_metric = classify_metric(cited_text)
                if ans_metrics and not (ans_metrics <= expected_metrics):
                    metric_ok = False
                elif cited_metric is not None and cited_metric not in expected_metrics:
                    metric_ok = False

            period_ok = True
            req_dp = getattr(fin_binding, "date_parsed", None)
            if req_dp:
                req_y, req_m, req_d = req_dp
                if req_y is not None and str(req_y) not in cited_text:
                    period_ok = False
                for txt in (ans, cited_text):
                    c_dp = getattr(bind_financial_query(txt), "date_parsed", None)
                    if c_dp:
                        c_y, c_m, c_d = c_dp
                        if (
                            (req_y is not None and c_y is not None and c_y != req_y)
                            or (req_m is not None and c_m is not None and c_m != req_m)
                            or (req_d is not None and c_d is not None and c_d != req_d)
                        ):
                            period_ok = False
                            break

            req_dur = getattr(fin_binding, "duration_norm", None)
            if period_ok and req_dur:
                for txt in (ans, cited_text):
                    c_dur = getattr(bind_financial_query(txt), "duration_norm", None)
                    if c_dur and c_dur != req_dur:
                        period_ok = False
                        break

            if nums_ok and metric_ok and period_ok:
                verified = True
                rejection_reason = None

        if not verified:
            logger.warning(
                "delivery_gate.financial_metric_binding_rejected reason=%s answer=%s (delivery blocked)",
                rejection_reason,
                ans,
            )
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "retained_evidence_items": [],
                "top_chunks": [],
            }

    # 2b. Workbook sheet-status claim verification against retained sheet_inventory evidence
    if ans and ans != "NOT_FOUND":
        from services.retrieval.sheet_inventory import verify_sheet_status_claims
        sheet_ok, sheet_reason = verify_sheet_status_claims(
            ans,
            state.get("retained_evidence_items", []),
            state.get("citations", []),
            query=state.get("query", ""),
        )
        if not sheet_ok:
            logger.warning(
                "delivery_gate.sheet_status_rejected reason=%s answer=%s (delivery blocked)",
                sheet_reason,
                ans,
            )
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "retained_evidence_items": [],
                "top_chunks": [],
            }

    # 3. Delivery gate blocking rule under ADR-004
    if state.get("escalation_executed") or state.get("require_delivery_gate"):
        # Missing boundary authority must fail closed under escalation
        if boundary_repo is None:
            logger.warning("delivery_gate.missing_boundary_authority (delivery blocked)")
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
                "retained_evidence_items": [],
                "top_chunks": [],
            }

        verifier = state.get("verifier") or state.get("semantic_verifier")
        ans = state.get("final_answer", "")
        if ans and ans != "NOT_FOUND":
            if verifier is None:
                # No compatible verifier exists -> block free form delivery
                logger.warning("delivery_gate.blocked_no_verifier (falling back to deterministic output)")
                from services.retrieval.extractor import extract_verified_fact
                from services.retrieval.response_builder import build_citation

                v_ans, v_chunk = extract_verified_fact(state["query"], state.get("top_chunks", []))
                if v_ans and v_chunk and _validate_replacement_excerpt(v_chunk, ws_id, boundary_repo, state.get("snapshot_context")):
                    return {
                        "final_answer": v_ans,
                        "citations": [build_citation(v_chunk, workspace_id=ws_id, boundary_repo=boundary_repo).to_dict()],
                        "executed_path": "deterministic_excerpt",
                        "confidence_score": 0.8,
                    }
                return {
                    "final_answer": "NOT_FOUND",
                    "citations": [],
                    "confidence_score": 0.0,
                    "retrieval_incomplete": True,
                    "retained_evidence_items": [],
                    "top_chunks": [],
                }
            else:
                # Verifier is present -> verify actual synthesized text under deadline protection
                comp_text = state.get("compressed_text", "")
                cits = state.get("citations", [])

                pipe_start = state.get("_pipeline_start_time", time.perf_counter())
                esc_deadline_t = state.get("_escalation_deadline_t") or state.get("escalation_deadline_t")
                if esc_deadline_t is not None:
                    rem_s_verify = max(0.0, float(esc_deadline_t) - time.perf_counter())
                elif state.get("escalation_executed"):
                    caller_spec_deadline = state.get("caller_deadline_ms") or state.get("phase_deadline_ms")
                    escalation_start = state.get("_escalation_start_time", pipe_start)
                    elapsed_esc_ms = (time.perf_counter() - escalation_start) * 1000.0
                    phase_budget_ms = float(caller_spec_deadline) if caller_spec_deadline is not None else 2500.0
                    rem_s_verify = max(0.0, (phase_budget_ms - elapsed_esc_ms) / 1000.0)
                else:
                    caller_spec_deadline = state.get("caller_deadline_ms") or state.get("phase_deadline_ms")
                    if caller_spec_deadline is not None:
                        elapsed_ms = (time.perf_counter() - pipe_start) * 1000.0
                        rem_s_verify = max(0.0, (float(caller_spec_deadline) - elapsed_ms) / 1000.0)
                    else:
                        elapsed_ms = (time.perf_counter() - pipe_start) * 1000.0
                        rem_s_verify = max(0.0, (15000.0 - elapsed_ms) / 1000.0)

                ctrl = state.get("laya_concurrency_controller")
                if ctrl is None:
                    try:
                        from services.retrieval.laya_concurrency import get_laya_concurrency_controller
                        ctrl = get_laya_concurrency_controller()
                    except ImportError:
                        from src.services.retrieval.laya_concurrency import get_laya_concurrency_controller
                        ctrl = get_laya_concurrency_controller()

                if rem_s_verify < 0.050:
                    logger.warning("delivery_gate.verifier_deadline_insufficient (delivery blocked)")
                    return {
                        "final_answer": "NOT_FOUND",
                        "citations": [],
                        "confidence_score": 0.0,
                        "retrieval_incomplete": True,
                        "retained_evidence_items": [],
                        "top_chunks": [],
                    }

                def _do_verify() -> Any:
                    return verifier.verify(text=ans, context=comp_text, citations=cits)

                v_rec, v_status = ctrl.run_provider_call(_do_verify, timeout_s=rem_s_verify)
                if v_status == "caller_timeout":
                    logger.warning("delivery_gate.verifier_caller_timeout (delivery blocked)")
                    return {
                        "final_answer": "NOT_FOUND",
                        "citations": [],
                        "confidence_score": 0.0,
                        "retrieval_incomplete": True,
                        "retained_evidence_items": [],
                        "top_chunks": [],
                    }
                elif v_status != "completed" or v_rec is None or isinstance(v_rec, Exception):
                    logger.error("delivery_gate.verifier_failed status=%s", v_status)
                    return {
                        "final_answer": "NOT_FOUND",
                        "citations": [],
                        "confidence_score": 0.0,
                        "retrieval_incomplete": True,
                        "retained_evidence_items": [],
                        "top_chunks": [],
                    }

                status = getattr(v_rec, "support_status", None)
                if status != "ASSERTED":
                    logger.warning("delivery_gate.verifier_rejected status=%s", status)
                    from services.retrieval.extractor import extract_verified_fact
                    from services.retrieval.response_builder import build_citation

                    v_ans, v_chunk = extract_verified_fact(state["query"], state.get("top_chunks", []))
                    if v_ans and v_chunk and _validate_replacement_excerpt(v_chunk, ws_id, boundary_repo, state.get("snapshot_context")):
                        return {
                            "final_answer": v_ans,
                            "citations": [build_citation(v_chunk, workspace_id=ws_id, boundary_repo=boundary_repo).to_dict()],
                            "executed_path": "deterministic_excerpt",
                            "confidence_score": 0.8,
                        }
                    return {
                        "final_answer": "NOT_FOUND",
                        "citations": [],
                        "confidence_score": 0.0,
                        "retrieval_incomplete": True,
                        "retained_evidence_items": [],
                        "top_chunks": [],
                    }

    return {}


def end_fast_path(state: PipelineState):
    """Verified factual fast path answer (Component 3). No LLM.
    Supports deterministic verified factual extraction as well as
    extractive sentence to chunk provenance selection.
    """
    from services.retrieval.response_builder import build_citation

    t0 = time.perf_counter()
    query = state["query"]
    existing = state.get("stage_timings", {})
    verified_ans = state.get("verified_answer")
    verified_chunk = state.get("verified_chunk")

    if not verified_ans or not verified_chunk:
        from services.retrieval.extractor import extract_verified_fact

        candidates = list(state.get("candidate_chunks", []))
        for c in state.get("bm25_candidates", []):
            if c not in candidates:
                candidates.append(c)
        if not candidates and state.get("top_chunks"):
            candidates = list(state.get("top_chunks", []))
        verified_ans, verified_chunk = extract_verified_fact(query, candidates)

    all_top = list(state.get("top_chunks", []))
    if not all_top:
        all_top = list(state.get("candidate_chunks", []))

    non_heading_chunks = [c for c in all_top if getattr(c, "element_type", "") != "heading"]
    if not non_heading_chunks and not (verified_ans and verified_chunk):
        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.info("fast_path.no_candidate_chunks escalating_to_full_path latency_ms=%.2f", latency_ms)
        return {
            "final_answer": "NOT_FOUND",
            "confidence_score": 0.0,
            "faithfulness": None,
            "citation_utilization_rate": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "top_chunks": [],
            "citations": [],
            "executed_path": "full",
            "stage_timings": {**existing, "fast_path_ms": latency_ms},
        }

    if verified_ans and verified_chunk:
        ws_id = state.get("workspace_id", "")
        b_repo = state.get("boundary_repo")
        if b_repo is None:
            try:
                from db.workspace_boundary_repo import get_workspace_boundary_repo
                b_repo = get_workspace_boundary_repo()
            except Exception:
                b_repo = None

        answer = verified_ans
        top_chunks = [verified_chunk]
        citation = build_citation(verified_chunk, workspace_id=ws_id, boundary_repo=b_repo).to_dict()
        util_rate = round(1.0 / max(1, len(all_top)), 4) if all_top else 1.0
        conf = getattr(verified_chunk, "similarity_score", None)
        if conf is None:
            conf = 1.0

        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.info("fast_path.verified_factual answer='%s' latency_ms=%.2f", answer[:50], latency_ms)
        from services.retrieval.evidence_contract import evidence_item_from_chunk
        retained_evidence_items = [
            evidence_item_from_chunk(c, workspace_id=ws_id, strategy="vector_rerank", boundary_repo=b_repo)
            for c in top_chunks
        ]
        return {
            "final_answer": answer,
            "confidence_score": float(conf),
            "faithfulness": 1.0,
            "citation_utilization_rate": util_rate,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "top_chunks": top_chunks,
            "citations": [citation],
            "retained_evidence_items": retained_evidence_items,
            "executed_path": "fast",
            "status": "success",
            "retrieval_candidates": {
                **dict(state.get("retrieval_candidates", {})),
                "reranked": [str(c.id) for c in top_chunks],
                "compressed_context": [str(c.id) for c in top_chunks],
                "llm_citations": [str(citation.get("chunk_id", ""))],
            },
            "stage_timings": {**existing, "fast_path_ms": latency_ms},
        }

    # If extraction finds no supported answer, escalate to full path without fabricating confidence or chunk
    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info("fast_path.no_supported_answer escalating_to_full_path latency_ms=%.2f", latency_ms)
    return {
        "final_answer": "NOT_FOUND",
        "confidence_score": 0.0,
        "faithfulness": None,
        "citation_utilization_rate": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "top_chunks": [],
        "citations": [],
        "executed_path": "full",
        "stage_timings": {**existing, "fast_path_ms": latency_ms},
    }


def escalate_to_full_retrieval(state: PipelineState):
    """Transition from failed fast path into full retrieval policy.
    Refreshes candidate dependent state, updates plan to fast_path=False,
    and executes genuine full vector retrieval (top_k=40, candidate scoping,
    full embeddings).
    """
    t0 = time.perf_counter()
    logger.info(
        "escalate_to_full_retrieval.started query='%s' workspace_id='%s'",
        state.get("query", "")[:50],
        state.get("workspace_id", ""),
    )

    plan = state.get("plan")
    full_plan = None
    if plan:
        from dataclasses import replace
        full_plan = replace(plan, fast_path=False)

    # Do not reuse an existing query vector solely because it has 1024 dimensions.
    # Use the established full retrieval embedding provider model matching the full index.
    # If the existing vector has no reliable model provenance, generate a fresh compatible query embedding.
    repo = _get_repo()
    from providers.bedrock_models import get_embedding_model
    from providers.embedding_provider import embed_text
    from providers.provider_client import get_active_provider

    active_provider = get_active_provider()
    expected_model = get_embedding_model()

    existing_embedding = state.get("query_embedding")
    provenance = state.get("query_embedding_provenance") or state.get("query_embedding_model")

    trusted_provenance = (
        provenance in (expected_model, f"{active_provider}:{expected_model}")
        if (provenance and isinstance(provenance, str))
        else False
    )

    if existing_embedding and len(existing_embedding) == 1024 and trusted_provenance:
        query_embedding = existing_embedding
    else:
        query_embedding = (
            repo.generate_query_embedding(state["query"])
            if hasattr(repo, "generate_query_embedding")
            else embed_text(state["query"], provider=active_provider)
        )
        provenance = expected_model

    retriever = VectorRetriever(repository=repo)
    chunks = retriever.search(
        query=state["query"],
        query_embedding=query_embedding,
        fast_path=False,
        document_ids=state.get("document_ids"),
        candidate_page_ids=state.get("candidate_page_ids"),
        candidate_chunk_ids=state.get("candidate_chunk_ids"),
        candidate_page_scopes=state.get("candidate_page_scopes"),
        workspace_id=state.get("workspace_id", ""),
        top_k=40,
    )

    vector_chunks = [replace(c, similarity_score=float(s)) for c, s in chunks]
    avg_sim = sum(s for _, s in chunks) / max(1, len(chunks)) if chunks else 0.0
    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "full_vector.latency_ms=%.2f full_vector.result_count=%d full_vector.avg_sim=%.4f",
        latency_ms,
        len(vector_chunks),
        avg_sim,
    )
    existing = state.get("stage_timings", {})

    return {
        "plan": full_plan,
        "executed_path": "full",
        "query_embedding": query_embedding,
        "query_embedding_provenance": provenance,
        "candidate_chunks": vector_chunks,
        "retrieval_candidates": {**dict(state.get("retrieval_candidates", {})), "vector": [c.id for c in vector_chunks]},
        "top_chunks": [],
        "verified_answer": None,
        "verified_chunk": None,
        "compressed_text": "",
        "context_snippet": "",
        "final_answer": "NOT_FOUND",
        "confidence_score": 0.0,
        "citations": [],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "faithfulness": None,
        "citation_utilization_rate": None,
        "stage_timings": {**existing, "full_vector_ms": latency_ms},
    }


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------


def run_verified_extraction(state: PipelineState):
    """Attempt deterministic verified fact extraction before deciding fast vs full path."""
    if state.get("force_full_path", False):
        return {}
    from services.retrieval.extractor import extract_verified_fact

    t0 = time.perf_counter()
    candidates = list(state.get("candidate_chunks", []))
    for c in state.get("bm25_candidates", []):
        if c not in candidates:
            candidates.append(c)

    verified_ans, verified_chunk = extract_verified_fact(state["query"], candidates)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    existing = state.get("stage_timings", {})
    return {
        "verified_answer": verified_ans,
        "verified_chunk": verified_chunk,
        "stage_timings": {**existing, "verified_extraction_ms": latency_ms},
    }


def route_after_vector(state: PipelineState):
    plan = state["plan"]
    if plan and plan.fast_path:
        return "run_verified_extraction"
    return "run_okf_router_post"


def route_after_extraction(state: PipelineState):
    """If verified extraction succeeded, end fast path.
    Otherwise escalate to full path via end_fast_path for telemetry and escalation."""
    if state.get("verified_answer") and state.get("verified_chunk"):
        return "end_fast_path"

    candidates = state.get("candidate_chunks", [])
    if candidates:
        return "end_fast_path"

    # Escalate to full generation path
    return "escalate_to_full_retrieval"


def route_after_fast_path(state: PipelineState) -> str:
    """If fast path found a supported answer, terminate at END.
    Otherwise escalate to full retrieval path via escalate_to_full_retrieval."""
    if state.get("executed_path") == "fast" and state.get("final_answer") and state.get("final_answer") != "NOT_FOUND":
        return END
    return "escalate_to_full_retrieval"


def route_after_reranker_or_graph(state: PipelineState):
    """After graph expansion, always proceed to reranker (graph added to state)."""
    return "run_reranker"


def route_after_okf_post(state: PipelineState):
    plan = state.get("plan")
    selected = state.get("selected_strategies", {}) or {}
    primary = selected.get("primary", [])
    graph_enabled = getattr(settings, "GRAPH_ENABLED", False)
    if graph_enabled or (plan and getattr(plan, "use_graph", False)) or ("knowledge_graph" in primary):
        return "run_graph"
    return "run_reranker"


# ---------------------------------------------------------------------------
# Dummy passthrough node — avoids duplicate conditional edge targets
# ---------------------------------------------------------------------------


def run_okf_post(state: PipelineState):
    """Passthrough: OKF already ran pre-BM25 (run_okf_router). This node exists
    only so the graph can branch to run_graph or run_reranker after vector."""
    return {}


def run_structured_aggregate(state: PipelineState):
    from services.retrieval.run_structured_aggregate import execute
    res = execute(state)
    if res.get("executed_path") == "structured_aggregate_fallback":
        q_emb = state.get("query_embedding")
        if q_emb is None:
            from providers.embedding_provider import embed_text
            from providers.bedrock_models import get_embedding_model
            res["query_embedding"] = embed_text(state["query"])
            res["query_embedding_provenance"] = get_embedding_model()
    return res


def route_after_route_query(state: PipelineState) -> str:
    plan = state.get("plan")
    selected = state.get("selected_strategies", {}) or {}
    primary = selected.get("primary", [])
    if (plan and getattr(plan, "use_structured_aggregate", False)) or ("structured_table" in primary):
        return "run_structured_aggregate"
    return "run_okf_router"


def route_after_structured(state: PipelineState) -> str:
    ea = state.get("executed_path")
    if ea == "structured_aggregate":
        return END
    if ea in (
        "structured_aggregate_incomplete",
        "structured_aggregate_storage_failure",
        "structured_aggregate_ambiguous",
        "structured_aggregate_unresolved_filter",
    ):
        return END
    return "run_okf_router"


# ---------------------------------------------------------------------------
# Build LangGraph
# ---------------------------------------------------------------------------

workflow = StateGraph(PipelineState)

workflow.add_node("route_query", route_query)
workflow.add_node("run_structured_aggregate", run_structured_aggregate)
workflow.add_node("run_okf_router", run_okf_router)  # pre-BM25 OKF (Component 2)
workflow.add_node("run_bm25", run_bm25)
workflow.add_node("run_page_index", run_page_index)
workflow.add_node("run_vector", run_vector)
workflow.add_node("run_verified_extraction", run_verified_extraction)
workflow.add_node("run_okf_router_post", run_okf_post)  # routing branch after vector
workflow.add_node("run_graph", run_graph)
workflow.add_node("run_reranker", run_reranker)
workflow.add_node("run_deterministic_math", run_deterministic_math)
workflow.add_node("run_compressor", run_compressor)
workflow.add_node("run_fidelity", run_fidelity)
workflow.add_node("run_llm", run_llm)
workflow.add_node("run_laya_escalation", run_laya_escalation)
workflow.add_node("delivery_gate", delivery_gate)
workflow.add_node("end_fast_path", end_fast_path)
workflow.add_node("escalate_to_full_retrieval", escalate_to_full_retrieval)

# Edge order: route_query → structured_aggregate / OKF → BM25 → PageIndex → vector → branch
workflow.add_edge(START, "route_query")
workflow.add_conditional_edges(
    "route_query",
    route_after_route_query,
    {
        "run_structured_aggregate": "run_structured_aggregate",
        "run_okf_router": "run_okf_router",
    },
)
workflow.add_conditional_edges(
    "run_structured_aggregate",
    route_after_structured,
    {
        END: END,
        "run_okf_router": "run_okf_router",
    },
)
workflow.add_edge("run_okf_router", "run_bm25")
workflow.add_edge("run_bm25", "run_page_index")
workflow.add_edge("run_page_index", "run_vector")

workflow.add_conditional_edges(
    "run_vector",
    route_after_vector,
    {
        "run_verified_extraction": "run_verified_extraction",
        "run_okf_router_post": "run_okf_router_post",
    },
)

workflow.add_conditional_edges(
    "run_verified_extraction",
    route_after_extraction,
    {
        "end_fast_path": "end_fast_path",
        "escalate_to_full_retrieval": "escalate_to_full_retrieval",
    },
)

workflow.add_conditional_edges(
    "run_okf_router_post",
    route_after_okf_post,
    {
        "run_graph": "run_graph",
        "run_reranker": "run_reranker",
    },
)

workflow.add_edge("run_graph", "run_reranker")
workflow.add_edge("run_reranker", "run_deterministic_math")
workflow.add_conditional_edges(
    "run_deterministic_math",
    route_after_math,
    {
        END: END,
        "run_compressor": "run_compressor",
    },
)
workflow.add_edge("run_compressor", "run_fidelity")
workflow.add_conditional_edges(
    "run_fidelity",
    route_after_fidelity,
    {
        "run_llm": "run_llm",
        "run_laya_escalation": "run_laya_escalation",
        "delivery_gate": "delivery_gate",
    },
)
workflow.add_edge("run_laya_escalation", "delivery_gate")
workflow.add_edge("run_llm", "delivery_gate")
workflow.add_edge("delivery_gate", END)
workflow.add_conditional_edges(
    "end_fast_path",
    route_after_fast_path,
    {
        END: END,
        "escalate_to_full_retrieval": "escalate_to_full_retrieval",
    },
)
workflow.add_edge("escalate_to_full_retrieval", "run_okf_router_post")

app = workflow.compile()


# ---------------------------------------------------------------------------
# Pipeline response object
# ---------------------------------------------------------------------------


class ResponseObject:
    def __init__(self, state: dict[str, Any]):
        self.answer = state.get("final_answer", "")
        self.citations = state.get("citations", [])
        self.confidence_score = state.get("confidence_score", 0.0)
        self.stage_timings = state.get("stage_timings", {})
        self.context_snippet = state.get(
            "context_snippet", ""
        )  # for faithfulness judge
        self.faithfulness = state.get("faithfulness", None)
        self.citation_utilization_rate = state.get("citation_utilization_rate", None)
        self.usage = state.get("usage", {"input_tokens": 0, "output_tokens": 0})
        self.execution_result = state.get("execution_result", None)
        self.status = state.get("status", "success")
        self.error_code = state.get("error_code")
        self.retained_evidence_items = state.get("retained_evidence_items", [])
        self.selected_strategies = state.get("selected_strategies", {})
        self.retrieval_incomplete = state.get("retrieval_incomplete", False)

        # Ensure every citation carries the strategy of its matched retained evidence item
        retained_map = {str(ev.evidence_id): ev.strategy for ev in self.retained_evidence_items}
        for ev in self.retained_evidence_items:
            loc_cid = ev.locator.get("chunk_id")
            if loc_cid:
                retained_map[str(loc_cid)] = ev.strategy
            tbl_id = ev.locator.get("table_id")
            if tbl_id:
                retained_map[str(tbl_id)] = ev.strategy
        for cit in self.citations:
            if isinstance(cit, dict) and not cit.get("strategy"):
                cid = str(cit.get("chunk_id", ""))
                tid = str(cit.get("table_id", ""))
                cit["strategy"] = retained_map.get(cid) or retained_map.get(tid) or "vector_rerank"

        ws_id = state.get("workspace_id", "")
        b_repo = state.get("boundary_repo")
        if b_repo is None:
            try:
                from src.db.workspace_boundary_repo import get_workspace_boundary_repo
                b_repo = get_workspace_boundary_repo()
            except Exception as e:
                logger.error("ResponseObject.boundary_repo_resolution_error: %s (failing closed)", e)
                b_repo = None

        # Final boundary check: verify workspace and document sources are not tombstoned
        # Any tombstone, lookup error, or missing/incompatible authority fails closed
        blocked = False
        if not b_repo or not hasattr(b_repo, "has_tombstone") or not callable(getattr(b_repo, "has_tombstone", None)):
            logger.warning("ResponseObject.boundary_repo_unavailable_or_incompatible (failing closed)")
            blocked = True
        elif ws_id:
            try:
                if b_repo.has_tombstone(ws_id, "workspace", ws_id):
                    blocked = True
                if not blocked:
                    for cit in self.citations:
                        did = cit.get("document_id") if isinstance(cit, dict) else getattr(cit, "document_id", None)
                        if did and b_repo.has_tombstone(ws_id, "source", str(did)):
                            blocked = True
                            break
                if not blocked:
                    for ev in self.retained_evidence_items:
                        did = getattr(ev, "document_id", None) or (getattr(ev, "locator", {}) or {}).get("document_id")
                        if did and b_repo.has_tombstone(ws_id, "source", str(did)):
                            blocked = True
                            break
            except Exception as e:
                logger.error("ResponseObject.final_boundary_check_error: %s (failing closed)", e)
                blocked = True

        if blocked or self.retrieval_incomplete or self.answer == "NOT_FOUND" or not self.answer:
            self.answer = "NOT_FOUND"
            self.citations = []
            self.retained_evidence_items = []
            self.top_chunks = []
            self.confidence_score = 0.0
            self.retrieval_incomplete = True
            self.faithfulness = None
            self.citation_utilization_rate = None
        else:
            # Ensure retained evidence and final citations carry identical canonical filename and version
            retained_by_cid = {}
            for ev in self.retained_evidence_items:
                loc = getattr(ev, "locator", {}) or {}
                cid = str(getattr(ev, "evidence_id", "") or loc.get("chunk_id", ""))
                if cid:
                    retained_by_cid[cid] = ev

            for cit in self.citations:
                if isinstance(cit, dict):
                    cid = str(cit.get("chunk_id", ""))
                    matched_ev = retained_by_cid.get(cid)
                    if matched_ev:
                        ev_fname = getattr(matched_ev, "document_filename", None) or (getattr(matched_ev, "locator", {}) or {}).get("document_filename")
                        ev_ver = getattr(matched_ev, "document_version", None) or (getattr(matched_ev, "locator", {}) or {}).get("document_version")
                        if ev_fname:
                            cit["document_filename"] = ev_fname
                        cit["document_version"] = ev_ver

        plan = state.get("plan")
        self.executed_path = state.get("executed_path", "full")
        self.fast_path = (self.executed_path == "fast")
        self.stages = plan.stages if plan else []
        self.planned_path = state.get("planned_path", "fast" if (plan and plan.fast_path) else "full")
        self.structured_aggregate = (self.executed_path == "structured_aggregate")
        self.reranker_mode = state.get("reranker_mode", RerankerMode.BYPASSED.value)
        self.reranker_telemetry = dict(state.get("reranker_telemetry") or {})
        self.retrieval_candidates = state.get("retrieval_candidates", {})
        self.structural_candidates = state.get("structural_candidates", [])
        self.structural_candidate_ids = state.get("structural_candidate_ids", [])
        self.structural_match_count = state.get("structural_match_count", 0)
        self.page_index_latency_ms = state.get("page_index_latency_ms", 0.0)
        self.structural_target_state = state.get("structural_target_state", "none")
        self.okf_candidates = state.get("okf_candidates", [])
        self.okf_candidate_ids = state.get("okf_candidate_ids", [])
        self.okf_match_count = state.get("okf_match_count", 0)
        self.okf_latency_ms = state.get("okf_latency_ms", 0.0)
        self.okf_entity_state = state.get("okf_entity_state", "none")
        self.okf_property_state = state.get("okf_property_state", "none")
        self.graph_candidates = state.get("graph_chunks", [])
        self.graph_candidate_ids = state.get("graph_candidate_ids", [])
        self.graph_match_count = state.get("graph_match_count", 0)
        self.graph_latency_ms = state.get("graph_latency_ms", 0.0)
        self.graph_traversed_nodes = state.get("graph_traversed_nodes", 0)
        self.graph_matched_edges = state.get("graph_matched_edges", 0)
        self.graph_was_truncated = state.get("graph_was_truncated", False)
        self.graph_truncation_reason = state.get("graph_truncation_reason", None)
        self.graph_entity_state = state.get("graph_entity_state", "none")
        self.graph_relationship_state = state.get("graph_relationship_state", "none")
        self.query_embedding = state.get("query_embedding")
        self.query_embedding_provenance = state.get("query_embedding_provenance")
        self.retrieval_incomplete = (
            True if (blocked or self.retrieval_incomplete or self.answer == "NOT_FOUND" or not self.answer)
            else state.get("retrieval_incomplete", False)
        )
        self.laya_calls = state.get("laya_calls", 0)
        self.expansion_steps = state.get("expansion_steps", 0)
        self.synthesis_calls = state.get("synthesis_calls", 0)
        self.laya_decision = state.get("laya_decision")
        self.laya_status = state.get("laya_status", "none")
        self.laya_latency_ms = state.get("laya_latency_ms", 0.0)
        self.laya_tokens = state.get("laya_tokens", {"input_tokens": 0, "output_tokens": 0})

        # Expose internal state for tests
        class LLMInput:
            def __init__(self, ctx):
                self.context = ctx

        self._llm_input = LLMInput(state.get("compressed_text", ""))

        self.top_chunks = [] if self.retrieval_incomplete else state.get("top_chunks", [])
        self._reranker_avg = (
            sum(
                (getattr(c, "reranker_score", 0.0) or 0.0)
                for c in self.top_chunks
            )
            / len(self.top_chunks)
            if self.top_chunks
            else 0.0
        )

        self._coverage = 1.0

    def __getitem__(self, item: str) -> Any:
        if item == "final_answer":
            return self.answer
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


class Pipeline:
    ResponseObject = ResponseObject
    def run(
        self,
        query: str,
        document_ids: list[str] | None = None,
        workspace_id: str = "",
        force_full_path: bool = False,
        query_embedding: list[float] | None = None,
        query_embedding_provenance: str | None = None,
        benchmark_mode: bool = False,
        table_store: Any | None = None,
        knowledge_graph_repo: Any | None = None,
        boundary_repo: Any | None = None,
        doc_repo: Any | None = None,
        retriever: Any | None = None,
        **kwargs,
    ):
        if not workspace_id:
            raise ValueError("workspace_id is required for pipeline execution")

        initial_state = {
            "query": query,
            "workspace_id": workspace_id,
            "query_embedding": query_embedding,
            "query_embedding_provenance": query_embedding_provenance,
            "document_ids": document_ids,
            "force_full_path": force_full_path,
            "plan": None,
            "planned_path": "full",
            "executed_path": "full",
            "bm25_candidates": [],
            "candidate_page_ids": [],
            "candidate_chunk_ids": [],
            "candidate_page_scopes": [],
            "candidate_chunks": [],
            "okf_properties": [],
            "okf_seed_chunk_ids": [],
            "graph_results": [],
            "graph_chunks": [],
            "top_chunks": [],
            "retrieval_candidates": {},
            "reranker_mode": RerankerMode.BYPASSED.value,
            "reranker_telemetry": {},
            "compressed_text": "",
            "context_snippet": "",
            "final_answer": "",
            "confidence_score": 0.0,
            "citations": [],
            "error": None,
            "stage_timings": {},
            "verified_answer": None,
            "verified_chunk": None,
            "execution_result": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "faithfulness": None,
            "citation_utilization_rate": None,
            "status": "pending",
            "error_code": None,
            "selected_strategies": None,
            "retained_evidence_items": [],
            "structural_candidates": [],
            "structural_candidate_ids": [],
            "structural_match_count": 0,
            "page_index_latency_ms": 0.0,
            "structural_target_state": "none",
            "okf_candidates": [],
            "okf_candidate_ids": [],
            "okf_match_count": 0,
            "okf_latency_ms": 0.0,
            "okf_entity_state": "none",
            "okf_property_state": "none",
            "okf_execution_status": "none",
            "graph_candidate_ids": [],
            "graph_match_count": 0,
            "graph_latency_ms": 0.0,
            "graph_traversed_nodes": 0,
            "graph_matched_edges": 0,
            "graph_was_truncated": False,
            "graph_truncation_reason": None,
            "graph_entity_state": "none",
            "graph_relationship_state": "none",
            "graph_execution_status": "none",
            "laya_enabled": kwargs.get("laya_enabled", getattr(settings, "LAYA_ENABLED", False)),
            "laya_calls": 0,
            "expansion_steps": 0,
            "synthesis_calls": 0,
            "laya_decision": None,
            "laya_status": "none",
            "laya_latency_ms": 0.0,
            "laya_tokens": {"input_tokens": 0, "output_tokens": 0},
            "require_delivery_gate": kwargs.get("require_delivery_gate", False),
            "escalation_executed": False,
            "retrieval_incomplete": False,
            "verifier": kwargs.get("verifier"),
            "semantic_verifier": kwargs.get("semantic_verifier"),
            "laya_client": kwargs.get("laya_client"),
            "laya_model_id": kwargs.get("laya_model_id"),
            "laya_endpoint_url": kwargs.get("laya_endpoint_url"),
            "laya_api_key": kwargs.get("laya_api_key"),
            "laya_decision_service": kwargs.get("laya_decision_service"),
            "laya_expansion_executor": kwargs.get("laya_expansion_executor"),
            "laya_budget_ledger": kwargs.get("laya_budget_ledger"),
            "laya_concurrency_controller": kwargs.get("laya_concurrency_controller"),
            "caller_deadline_ms": kwargs.get("caller_deadline_ms"),
            "phase_deadline_ms": kwargs.get("phase_deadline_ms"),
            "snapshot_context": kwargs.get("snapshot_context"),
            "_pipeline_start_time": time.perf_counter(),
            "_escalation_start_time": time.perf_counter(),
            "_escalation_deadline_t": kwargs.get("_escalation_deadline_t"),
            "escalation_deadline_t": kwargs.get("escalation_deadline_t"),
            "financial_binding": kwargs.get("financial_binding"),
        }

        if table_store is not None:
            initial_state["table_store"] = table_store
        if knowledge_graph_repo is not None:
            initial_state["knowledge_graph_repo"] = knowledge_graph_repo
        if boundary_repo is None:
            try:
                from db.workspace_boundary_repo import get_workspace_boundary_repo
                boundary_repo = get_workspace_boundary_repo()
            except Exception:
                boundary_repo = None
        if boundary_repo is not None:
            initial_state["boundary_repo"] = boundary_repo
        if doc_repo is not None:
            initial_state["doc_repo"] = doc_repo
        if retriever is not None:
            initial_state["retriever"] = retriever
        for k, v in kwargs.items():
            if k not in initial_state:
                initial_state[k] = v

        final_state = app.invoke(initial_state)

        return ResponseObject(final_state)


pipeline = Pipeline()
