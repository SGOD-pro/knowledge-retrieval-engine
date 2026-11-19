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
    from services.retrieval.laya_executor import LayaExpansionExecutor
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
    from src.services.retrieval.laya_executor import LayaExpansionExecutor
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
    laya_decision_service: Any | None
    laya_expansion_executor: Any | None
    laya_budget_ledger: Any | None
    snapshot_context: Any | None
    _pipeline_start_time: float


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

    logger.info("route_query.latency_ms=%.2f fast_path=%s planned=%s primary_strategies=%s", latency_ms, plan.fast_path, planned, selected_strategies.primary)
    return {
        "query_embedding": query_embedding,
        "query_embedding_provenance": provenance,
        "plan": plan,
        "planned_path": planned,
        "executed_path": planned,
        "selected_strategies": selected_strategies.to_dict(),
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
        overall_res = retriever.search(state["query"], all_chunks, top_k=25)
        for c, s in overall_res:
            if c.id not in seen_ids:
                seen_ids.add(c.id)
                combined_results.append((c, s))
        results = combined_results
    else:
        results = retriever.search(state["query"], all_chunks, top_k=40)

    chunks = [c for c, _ in results]
    avg_score = sum(s for _, s in results) / max(1, len(results))
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
        return {"top_chunks": [], "retrieval_candidates": cands, "reranker_mode": RerankerMode.BYPASSED.value}

    rerank_res = rerank(state["query"], merged, top_k=6, return_mode=True)
    if isinstance(rerank_res, tuple) and len(rerank_res) == 2:
        top_chunks, reranker_mode = rerank_res
    else:
        # Accommodate test doubles in legacy phase tests returning bare chunk list
        top_chunks = rerank_res
        reranker_mode = getattr(rerank_res, "reranker_mode", RerankerMode.BYPASSED.value)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "reranker.latency_ms=%.2f reranker.input_count=%d reranker.output_count=%d mode=%s",
        latency_ms,
        len(merged),
        len(top_chunks),
        reranker_mode,
    )
    existing = state.get("stage_timings", {})

    retrieval_candidates = {
        "bm25": [str(c.id) for c in bm25],
        "vector": [str(c.id) for c in vector],
        "graph": [str(c.id) for c in graph],
        "structural": [str(c.id) for c in structural],
        "okf": [str(c.id) for c in okf_candidates],
        "rrf": [str(c.id) for c in merged],
        "reranked": [str(c.id) for c in top_chunks],
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
        )
        retained_evidence_items.append(ev)

    return {
        "top_chunks": top_chunks,
        "retained_evidence_items": retained_evidence_items,
        "retrieval_candidates": retrieval_candidates,
        "reranker_mode": reranker_mode,
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
        return {
            "stage_timings": {**existing, "deterministic_math_ms": latency_ms},
        }

    ans_str = res.format_answer(state["query"])

    # Map provenance citations to response builder citations from chunks
    citations = []
    chunk_by_id = {getattr(c, "id", ""): c for c in chunks}
    for prov in res.provenance_citations:
        cid = prov.get("chunk_id") if isinstance(prov, dict) else getattr(prov, "chunk_id", "")
        if cid and cid in chunk_by_id:
            citations.append(build_citation(chunk_by_id[cid]).to_dict())
    if not citations and chunks:
        citations = [build_citation(chunks[0]).to_dict()]

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

    response = call_llm(state["query"], compressed)

    # Real confidence score — avg reranker score
    top_chunks = state.get("top_chunks", [])
    avg_reranker = (
        sum((getattr(c, "reranker_score", 0.0) or 0.0) for c in top_chunks) / len(top_chunks)
        if top_chunks
        else 0.0
    )
    confidence = avg_reranker

    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info("llm.latency_ms=%.2f llm.confidence=%.4f", latency_ms, confidence)
    existing = state.get("stage_timings", {})

    from services.retrieval.response_builder import build_citation
    from services.evaluation.benchmark_scorer import compute_faithfulness

    ans = response.get("answer", "NOT_FOUND")
    usage = response.get("usage", {"input_tokens": 0, "output_tokens": 0})

    cands = dict(state.get("retrieval_candidates", {}))
    compressed_ids = set(cands.get("compressed_context", []))
    if not compressed_ids and compressed:
        compressed_ids = {str(c.id) for c in top_chunks if f"[{c.id}]" in compressed}
        cands["compressed_context"] = list(compressed_ids)

    # Parse and validate citations directly from the LLM's JSON contract: {"answer": "...", "citations": ["chunk_id"]}
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

    if not final_cited_chunks and ans != "NOT_FOUND":
        final_cited_chunks = [c for c in top_chunks if str(c.id) in compressed_ids]

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

    return {
        "final_answer": ans,
        "citations": [build_citation(c).to_dict() for c in final_cited_chunks],
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
        if laya_enabled and expansion_steps < 1 and laya_calls < 1 and remaining_ms >= 500.0:
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
    - Atomic pre reservation in request local budget ledger.
    - Token preflight with deterministic evidence truncation (<= 1000 tokens).
    - Single pass BM25 lexical expansion (other routes explicitly unavailable).
    - Merging novel chunks with baseline candidates.
    - Re reranking and recompression.
    - Clearing stale refusal state before secondary fidelity recheck.
    - Single synthesis call enforcement.
    - Delivery gate evaluation before free form prose delivery.
    """
    t0 = time.perf_counter()
    existing_timings = state.get("stage_timings", {})
    cands = dict(state.get("retrieval_candidates", {}))
    ledger = state.get("laya_budget_ledger") or LayaBudgetLedger()

    pipe_start = state.get("_pipeline_start_time", time.perf_counter())
    elapsed_ms = (time.perf_counter() - pipe_start) * 1000.0
    remaining_ms = max(0.0, 15000.0 - elapsed_ms)

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
    ws_id = state.get("workspace_id", "ws_default")
    for c in baseline_chunks[:10]:
        c_ver = str(getattr(c, "document_version", "") or getattr(c, "version", "") or "v1")
        c_score = float(min(100.0, max(0.0, getattr(c, "similarity_score", 0.0) or getattr(c, "reranker_score", 0.0) or 1.0)))
        summaries.append(
            BaselineEvidenceSummary(
                chunk_id=str(c.id)[:64],
                document_id=str(getattr(c, "document_id", "doc_unknown"))[:128],
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
        caller_deadline_ms=remaining_ms,
    )

    service = state.get("laya_decision_service") or LayaDecisionService(
        client=state.get("laya_client"),
        model_id=state.get("laya_model_id"),
    )
    decision = service.decide(escalation_req, remaining_ms, ledger)
    l_lat_ms = (time.perf_counter() - t0) * 1000.0

    cands["laya_decision_id"] = decision.decision_id
    cands["laya_route"] = decision.route
    cands["laya_confidence"] = decision.confidence

    novel_chunks: list[Chunk] = []
    no_progress = True
    status = decision.execution_status

    if decision.route == "lexical_expansion":
        executor = state.get("laya_expansion_executor") or LayaExpansionExecutor()
        novel_chunks, no_progress, status = executor.execute(
            decision=decision,
            workspace_id=ws_id,
            baseline_chunks=state.get("candidate_chunks", []),
            boundary_repo=state.get("boundary_repo"),
            chunk_repo=state.get("doc_repo"),
            snapshot_context=state.get("snapshot_context"),
        )

    cands["laya_novel_chunks"] = [str(c.id) for c in novel_chunks]
    cands["laya_no_progress"] = no_progress

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
    }

    if novel_chunks and not no_progress:
        # Note on design choice for Slice 5.1:
        # Post expansion reranking, recompression, fidelity rechecking, and single synthesis
        # are executed as an inline sequence of pure state transformer calls within this node.
        # This matches the bounded specification requirement for immediate deterministic resolution.
        # Slice 5.2 may restructure this flow as an explicit LangGraph subgraph.

        # 1. Pool novel chunks with baseline candidates
        merged_candidates = list(state.get("candidate_chunks", [])) + novel_chunks
        current_state = {**state, **updated_state, "candidate_chunks": merged_candidates}

        # 2. Rerank updated candidate pool (consuming second allowed rerank batch under ADR-004)
        rerank_res = run_reranker(current_state)
        current_state.update(rerank_res)

        # 3. Recompress top chunks
        comp_res = run_compressor(current_state)
        current_state.update(comp_res)

        # 4. Clear stale refusal state before secondary check
        current_state["error"] = None
        current_state["final_answer"] = None
        current_state["citations"] = []

        # 5. Secondary fidelity check
        fid_res = run_fidelity(current_state)
        current_state.update(fid_res)

        updated_state.update(rerank_res)
        updated_state.update(comp_res)
        updated_state["candidate_chunks"] = merged_candidates
        updated_state["escalation_executed"] = True

        if current_state.get("error") is None:
            # Secondary fidelity succeeded!
            updated_state["error"] = None
            verifier = state.get("verifier") or state.get("semantic_verifier")
            if verifier is not None and (not hasattr(verifier, "is_ready") or verifier.is_ready()):
                # Run single synthesis call
                ledger.synthesis_calls += 1
                updated_state["synthesis_calls"] = ledger.synthesis_calls
                llm_res = run_llm(current_state)
                updated_state.update(llm_res)
            else:
                # Verifier is unconfigured / unavailable -> free form delivery blocked!
                from services.retrieval.extractor import extract_verified_fact
                v_ans, v_chunk = extract_verified_fact(current_state["query"], current_state.get("top_chunks", []))
                if v_ans and v_chunk:
                    updated_state["final_answer"] = v_ans
                    updated_state["citations"] = [build_citation(v_chunk).to_dict()]
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
    """
    ws_id = state.get("workspace_id", "")
    boundary_repo = state.get("boundary_repo")

    # 1. Dynamic tombstone check before delivery
    if boundary_repo is not None and hasattr(boundary_repo, "has_tombstone"):
        if boundary_repo.has_tombstone(ws_id, "workspace", ws_id):
            logger.warning("delivery_gate.workspace_tombstoned ws_id=%s (delivery blocked)", ws_id)
            return {
                "final_answer": "NOT_FOUND",
                "citations": [],
                "confidence_score": 0.0,
                "retrieval_incomplete": True,
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
                }

    # 2. Delivery gate blocking rule under ADR-004
    if state.get("escalation_executed") or state.get("require_delivery_gate"):
        verifier = state.get("verifier") or state.get("semantic_verifier")
        ans = state.get("final_answer", "")
        if ans and ans != "NOT_FOUND":
            if verifier is None:
                # No compatible verifier exists -> block free form delivery
                logger.warning("delivery_gate.blocked_no_verifier (falling back to deterministic output)")
                from services.retrieval.extractor import extract_verified_fact
                from services.retrieval.response_builder import build_citation

                v_ans, v_chunk = extract_verified_fact(state["query"], state.get("top_chunks", []))
                if v_ans and v_chunk:
                    return {
                        "final_answer": v_ans,
                        "citations": [build_citation(v_chunk).to_dict()],
                        "executed_path": "deterministic_excerpt",
                        "confidence_score": 0.8,
                    }
                return {
                    "final_answer": "NOT_FOUND",
                    "citations": [],
                    "confidence_score": 0.0,
                    "retrieval_incomplete": True,
                }
            else:
                # Verifier is present -> verify actual synthesized text
                comp_text = state.get("compressed_text", "")
                cits = state.get("citations", [])
                try:
                    v_rec = verifier.verify(text=ans, context=comp_text, citations=cits)
                    status = getattr(v_rec, "support_status", None)
                    if status != "ASSERTED":
                        logger.warning("delivery_gate.verifier_rejected status=%s", status)
                        from services.retrieval.extractor import extract_verified_fact
                        from services.retrieval.response_builder import build_citation

                        v_ans, v_chunk = extract_verified_fact(state["query"], state.get("top_chunks", []))
                        if v_ans and v_chunk:
                            return {
                                "final_answer": v_ans,
                                "citations": [build_citation(v_chunk).to_dict()],
                                "executed_path": "deterministic_excerpt",
                                "confidence_score": 0.8,
                            }
                        return {
                            "final_answer": "NOT_FOUND",
                            "citations": [],
                            "confidence_score": 0.0,
                            "retrieval_incomplete": True,
                        }
                except Exception as e:
                    logger.error("delivery_gate.verifier_exception error=%s", e)
                    return {
                        "final_answer": "NOT_FOUND",
                        "citations": [],
                        "confidence_score": 0.0,
                        "retrieval_incomplete": True,
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
        answer = verified_ans
        top_chunks = [verified_chunk]
        citation = build_citation(verified_chunk).to_dict()
        util_rate = round(1.0 / max(1, len(all_top)), 4) if all_top else 1.0
        conf = getattr(verified_chunk, "similarity_score", None)
        if conf is None:
            conf = 1.0

        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.info("fast_path.verified_factual answer='%s' latency_ms=%.2f", answer[:50], latency_ms)
        from services.retrieval.evidence_contract import evidence_item_from_chunk
        retained_evidence_items = [
            evidence_item_from_chunk(c, workspace_id=state.get("workspace_id", ""), strategy="vector_rerank")
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

    # Sentence-level extractive fallback for fast-path prose queries
    _STOPS = {
        "what", "is", "the", "a", "an", "in", "on", "at", "to", "for", "of", "and",
        "or", "with", "by", "from", "as", "are", "how", "many", "does", "do", "did",
    }
    q_words = {w.lower().strip(".,?!\"'") for w in query.split() if w.lower().strip(".,?!\"'") not in _STOPS and len(w) > 2}
    if not q_words:
        q_words = {w.lower().strip(".,?!\"'") for w in query.split() if len(w) > 1}

    selected_sentences = []
    contributing_chunks = []

    for c in non_heading_chunks:
        sentences = re.split(r"(?<=[.!?])\s+", c.text.strip())
        chunk_matched = False
        for s in sentences:
            s_words = {w.lower().strip(".,?!\"'") for w in s.split()}
            if q_words & s_words:
                selected_sentences.append(s.strip())
                chunk_matched = True
        if chunk_matched:
            contributing_chunks.append(c)

    if selected_sentences and contributing_chunks:
        answer = " ".join(selected_sentences)[:800]
        top_chunks = contributing_chunks
        citations = [build_citation(c).to_dict() for c in contributing_chunks]
        util_rate = round(len(contributing_chunks) / len(all_top), 4) if all_top else 1.0
        conf = getattr(contributing_chunks[0], "similarity_score", None)
        if conf is None:
            conf = 1.0

        latency_ms = (time.perf_counter() - t0) * 1000.0
        from services.retrieval.evidence_contract import evidence_item_from_chunk
        retained_evidence_items = [
            evidence_item_from_chunk(c, workspace_id=state.get("workspace_id", ""), strategy="vector_rerank")
            for c in top_chunks
        ]
        return {
            "final_answer": answer,
            "confidence_score": float(conf),
            "faithfulness": 1.0,
            "citation_utilization_rate": util_rate,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "top_chunks": top_chunks,
            "citations": citations,
            "retained_evidence_items": retained_evidence_items,
            "executed_path": "fast",
            "status": "success",
            "retrieval_candidates": {
                **dict(state.get("retrieval_candidates", {})),
                "reranked": [str(c.id) for c in top_chunks],
                "compressed_context": [str(c.id) for c in top_chunks],
                "llm_citations": [str(c.get("chunk_id", "")) for c in citations if c.get("chunk_id")],
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
    If high confidence top chunk exists (similarity >= 0.40), end fast path via extractive fallback.
    Otherwise escalate to full path."""
    if state.get("verified_answer") and state.get("verified_chunk"):
        return "end_fast_path"

    candidates = state.get("candidate_chunks", [])
    if candidates:
        top_c = candidates[0]
        top_sim = getattr(top_c, "similarity_score", 0.0) or 0.0
        threshold = 0.40
        if top_sim >= threshold:
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
# Pipeline entry point
# ---------------------------------------------------------------------------


class Pipeline:
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
            "laya_decision_service": kwargs.get("laya_decision_service"),
            "laya_expansion_executor": kwargs.get("laya_expansion_executor"),
            "laya_budget_ledger": kwargs.get("laya_budget_ledger"),
            "snapshot_context": kwargs.get("snapshot_context"),
            "_pipeline_start_time": time.perf_counter(),
        }

        if table_store is not None:
            initial_state["table_store"] = table_store
        if knowledge_graph_repo is not None:
            initial_state["knowledge_graph_repo"] = knowledge_graph_repo
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

        class ResponseObject:
            def __init__(self, state):
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

                plan = state.get("plan")
                self.executed_path = state.get("executed_path", "full")
                self.fast_path = (self.executed_path == "fast")
                self.stages = plan.stages if plan else []
                self.planned_path = state.get("planned_path", "fast" if (plan and plan.fast_path) else "full")
                self.structured_aggregate = (self.executed_path == "structured_aggregate")
                self.reranker_mode = state.get("reranker_mode", RerankerMode.BYPASSED.value)
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
                self.retrieval_incomplete = state.get("retrieval_incomplete", False)
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

                self.top_chunks = state.get("top_chunks", [])
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

        return ResponseObject(final_state)


pipeline = Pipeline()
