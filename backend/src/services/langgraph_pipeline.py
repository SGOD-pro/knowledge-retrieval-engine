import json
import logging
import re
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from schemas.models import Chunk
from services.llm.llm_service import call as call_llm
from services.retrieval.bm25_retriever import BM25Retriever
from services.retrieval.compressor import compress_chunks
from services.retrieval.fidelity_check import CoverageError, check_fidelity
from services.retrieval.graph_retriever import GraphRetriever
from services.retrieval.okf_retriever import OKFRetriever
from services.retrieval.page_index_retriever import PageIndexRetriever
from services.retrieval.planner import Plan, planner
from services.retrieval.reranker import rerank
from services.retrieval.vector_retriever import VectorRetriever

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
    retrieval_candidates: dict[str, list[str]]
    reranker_mode: str
    status: str | None
    error_code: str | None
    selected_strategies: dict[str, Any] | None
    retained_evidence_items: list[Any]


# ---------------------------------------------------------------------------
# RRF merge (Component 1)
# ---------------------------------------------------------------------------


def _rrf_merge(
    bm25_chunks: list,
    vector_chunks: list,
    graph_chunks: list | None = None,
    k: int = 60,
    graph_weight: float = 1.2,
) -> list:
    """Reciprocal Rank Fusion with graph evidence signal.
    k=60 per Cormack et al. 2009.
    Returns deduplicated list of Chunk objects ordered by descending RRF score."""
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
            scores[chunk.id] = scores.get(chunk.id, 0.0) + (graph_weight / (k + rank + 1))
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
    # only embed via Titan if full path is chosen (not structured and not fast-path)
    # and embedding not yet computed
    query_embedding = state.get("query_embedding")
    if not plan.fast_path and not is_structured and query_embedding is None:
        from providers.embedding_provider import embed_text
        query_embedding = embed_text(state["query"])

    latency_ms = (time.perf_counter() - t0) * 1000.0
    if is_structured:
        planned = "structured_aggregate"
    else:
        planned = "fast" if plan.fast_path else "full"

    logger.info("route_query.latency_ms=%.2f fast_path=%s planned=%s primary_strategies=%s", latency_ms, plan.fast_path, planned, selected_strategies.primary)
    return {
        "query_embedding": query_embedding,
        "plan": plan,
        "planned_path": planned,
        "executed_path": planned,
        "selected_strategies": selected_strategies.to_dict(),
        "stage_timings": {"route_query_ms": latency_ms},
    }


def run_okf_router(state: PipelineState):
    """Pre-retrieval OKF lookup (Component 2). Returns okf_seed_chunk_ids for BM25
    soft boost. Failure is always silent — empty seed list = retrieval unchanged."""
    from services.retrieval.planner import extract_entities

    t0 = time.perf_counter()
    entities = extract_entities(state["query"])
    props, seed_ids = [], []
    if entities:
        try:
            retriever = OKFRetriever()
            props = retriever.lookup(entities)
            seed_ids = [
                p.get("source_chunk_id") for p in props if p.get("source_chunk_id")
            ]
        except Exception as e:
            logger.warning(
                "okf_router.failed entity_count=%d error=%s", len(entities), e
            )
    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "okf_router.latency_ms=%.2f okf_router.seed_count=%d", latency_ms, len(seed_ids)
    )
    existing = state.get("stage_timings", {})
    return {
        "okf_properties": props,
        "okf_seed_chunk_ids": seed_ids,
        "stage_timings": {**existing, "okf_router_ms": latency_ms},
    }


def run_bm25(state: PipelineState):
    """BM25 retrieval (Component 1 + 2). top_k=40 for union design.
    Soft-boosts OKF-matched chunks by OKF_BOOST before returning."""
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

    # OKF soft-boost — any chunk whose id appears in okf_seed_chunk_ids gets
    # its BM25 score multiplied by OKF_BOOST. This is a pre-reranker signal only.
    seed_ids = set(state.get("okf_seed_chunk_ids", []))
    if seed_ids:
        results = [(c, s * OKF_BOOST if c.id in seed_ids else s) for c, s in results]
        results.sort(key=lambda x: x[1], reverse=True)

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


def run_page_index(state: PipelineState):
    """PageIndex narrowing (Component 1 / Rule 5). Narrows candidate pages
    based on headings/footnotes retrieved by BM25 with document-scoped constraints."""
    t0 = time.perf_counter()
    retriever = PageIndexRetriever()
    bm25_cands = state.get("bm25_candidates")
    if bm25_cands:
        chunks, pages, c_ids = retriever.filter_and_rank(
            state["query"], bm25_cands, top_k=20
        )
        page_scopes = [
            (str(c.document_id), c.page_number)
            for c in chunks
            if c.page_number is not None and getattr(c, "document_id", None)
        ]
        # Include all selected chunk IDs
        all_selected_ids = [str(c.id) for c in chunks]
        c_ids = list(set((c_ids or []) + all_selected_ids))
    else:
        # When BM25 is skipped (e.g. fast path), do not restrict page/chunk IDs
        pages, c_ids, page_scopes = None, None, None

    avg_score = 1.0 if pages else 0.0
    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "page_index.latency_ms=%.2f page_index.confidence_score=%.2f",
        latency_ms,
        avg_score,
    )
    existing = state.get("stage_timings", {})
    return {
        "candidate_page_ids": pages,
        "candidate_chunk_ids": c_ids,
        "candidate_page_scopes": page_scopes,
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
    """Graph traversal with provenance-backed source chunk extraction."""
    from services.retrieval.planner import extract_entities

    t0 = time.perf_counter()
    entities = extract_entities(state["query"])

    retriever = GraphRetriever()
    results = retriever.expand(entities)

    # Convert graph traversal outputs to provenance-backed chunk IDs
    graph_chunks = []
    repo = _get_repo()
    ws_id = state.get("workspace_id", "")
    chunk_ids_to_fetch = set()

    for item in results:
        c_id = item.get("source_chunk_id") or item.get("chunk_id")
        if c_id:
            chunk_ids_to_fetch.add(str(c_id))

    if entities and repo:
        try:
            okf_props = repo.get_okf_properties(entities)
            for p in okf_props:
                sc = p.get("source_chunk_id")
                if sc:
                    chunk_ids_to_fetch.add(str(sc))
        except Exception:
            pass

    if chunk_ids_to_fetch and repo:
        try:
            all_ws_chunks = repo.get_all_chunks(workspace_id=ws_id)
            ws_by_id = {str(c.id): c for c in all_ws_chunks}
            for cid in chunk_ids_to_fetch:
                if cid in ws_by_id:
                    graph_chunks.append(ws_by_id[cid])
        except Exception as e:
            logger.debug("graph_chunk_fetch_failed error=%s", e)

    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "graph.latency_ms=%.2f graph.result_count=%d graph.chunk_count=%d",
        latency_ms,
        len(results),
        len(graph_chunks),
    )
    existing = state.get("stage_timings", {})
    return {
        "graph_results": results,
        "graph_chunks": graph_chunks,
        "stage_timings": {**existing, "graph_ms": latency_ms},
    }


def run_reranker(state: PipelineState):
    """RRF merge of bm25_candidates + candidate_chunks (vector) + graph_chunks, then neural rerank.
    Tracks raw retrieval candidates per stage for honest evaluation."""
    t0 = time.perf_counter()
    bm25 = state.get("bm25_candidates", [])
    vector = state.get("candidate_chunks", [])
    graph = state.get("graph_chunks", [])

    merged = _rrf_merge(bm25, vector, graph_chunks=graph, k=60, graph_weight=1.2)
    if not merged:
        logger.warning("run_reranker: no chunks to rerank — returning empty")
        return {"top_chunks": [], "retrieval_candidates": {}, "reranker_mode": "remote_success"}

    from providers.reranker_provider import _is_circuit_open
    circuit_open = _is_circuit_open()
    reranker_mode = "circuit_open_lexical_fallback" if circuit_open else "remote_success"

    top_chunks = rerank(state["query"], merged, top_k=6)
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
        "rrf": [str(c.id) for c in merged],
        "reranked": [str(c.id) for c in top_chunks],
    }

    from services.retrieval.evidence_contract import evidence_item_from_chunk
    retained_evidence_items = []
    ws_id = state.get("workspace_id", "")
    for c in top_chunks:
        strat = getattr(c, "strategy", None) or (c.metadata.get("strategy") if isinstance(getattr(c, "metadata", None), dict) else None)
        if not strat:
            if c in vector:
                strat = "vector_rerank"
            elif c in bm25:
                strat = "bm25"
            elif c in graph:
                strat = "knowledge_graph"
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
        return {"error": "No context available"}
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
        "stage_timings": {**existing, "llm_ms": latency_ms},
    }


def end_fast_path(state: PipelineState):
    """Verified factual fast-path answer (Component 3). No LLM.
    Supports deterministic verified factual extraction as well as
    extractive sentence-to-chunk provenance selection.
    """
    from services.retrieval.response_builder import build_citation

    t0 = time.perf_counter()
    query = state["query"]
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
        return {
            "final_answer": "NOT_FOUND",
            "confidence_score": 0.0,
            "faithfulness": None,
            "citation_utilization_rate": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "top_chunks": [],
            "citations": [],
            "stage_timings": {**state.get("stage_timings", {}), "fast_path_ms": latency_ms},
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

    if not selected_sentences and non_heading_chunks:
        selected_sentences = [non_heading_chunks[0].text.strip()[:500]]
        contributing_chunks = [non_heading_chunks[0]]

    if selected_sentences and contributing_chunks:
        answer = " ".join(selected_sentences)[:800]
        top_chunks = contributing_chunks
        citations = [build_citation(c).to_dict() for c in contributing_chunks]
        util_rate = round(len(contributing_chunks) / len(all_top), 4) if all_top else 1.0
        conf = getattr(contributing_chunks[0], "similarity_score", None)
        if conf is None:
            conf = 1.0

        latency_ms = (time.perf_counter() - t0) * 1000.0
        existing = state.get("stage_timings", {})
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
                "reranked": [str(c.id) for c in top_chunks],
                "compressed_context": [str(c.id) for c in top_chunks],
                "llm_citations": [str(c.get("chunk_id", "")) for c in citations if c.get("chunk_id")],
            },
            "stage_timings": {**existing, "fast_path_ms": latency_ms},
        }

    latency_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "final_answer": "NOT_FOUND",
        "confidence_score": 0.0,
        "faithfulness": None,
        "citation_utilization_rate": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "top_chunks": [],
        "citations": [],
        "executed_path": "fast",
        "stage_timings": {**state.get("stage_timings", {}), "fast_path_ms": latency_ms},
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
    """If verified extraction succeeded, end fast-path.
    If high-confidence top chunk exists (similarity >= 0.40), end fast-path via extractive fallback.
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
    return "run_okf_router_post"


def route_after_reranker_or_graph(state: PipelineState):
    """After graph expansion, always proceed to reranker (graph added to state)."""
    return "run_reranker"


def route_after_okf_post(state: PipelineState):
    plan = state["plan"]
    if plan and plan.use_graph:
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
            res["query_embedding"] = embed_text(state["query"])
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
    if ea in ("structured_aggregate_incomplete", "structured_aggregate_storage_failure", "structured_aggregate_ambiguous"):
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
workflow.add_node("end_fast_path", end_fast_path)

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
        "run_okf_router_post": "run_okf_router_post",
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
workflow.add_edge("run_fidelity", "run_llm")
workflow.add_edge("run_llm", END)
workflow.add_edge("end_fast_path", END)

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
        benchmark_mode: bool = False,
    ):
        if not workspace_id:
            raise ValueError("workspace_id is required for pipeline execution")

        initial_state = {
            "query": query,
            "workspace_id": workspace_id,
            "query_embedding": query_embedding,
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
            "reranker_mode": "remote_success",
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
        }

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
                self.fast_path = plan.fast_path if plan else False
                self.stages = plan.stages if plan else []
                self.planned_path = state.get("planned_path", "fast" if self.fast_path else "full")
                self.executed_path = state.get("executed_path", "fast" if self.fast_path else "full")
                self.structured_aggregate = (self.executed_path == "structured_aggregate")
                self.reranker_mode = state.get("reranker_mode", "remote_success")
                self.retrieval_candidates = state.get("retrieval_candidates", {})

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
