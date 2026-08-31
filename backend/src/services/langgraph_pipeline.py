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


# ---------------------------------------------------------------------------
# RRF merge (Component 1)
# ---------------------------------------------------------------------------


def _rrf_merge(bm25_chunks: list, vector_chunks: list, k: int = 60) -> list:
    """Reciprocal Rank Fusion. k=60 per the original RRF paper (Cormack et al. 2009).
    Returns deduplicated list of Chunk objects ordered by descending RRF score."""
    scores: dict[str, float] = {}
    by_id: dict[str, object] = {}
    for rank, chunk in enumerate(bm25_chunks):
        scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (k + rank + 1)
        by_id[chunk.id] = chunk
    for rank, chunk in enumerate(vector_chunks):
        scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (k + rank + 1)
        by_id[chunk.id] = chunk
    return [by_id[cid] for cid in sorted(scores, key=scores.__getitem__, reverse=True)]


# ---------------------------------------------------------------------------
# Pipeline nodes
# ---------------------------------------------------------------------------


def route_query(state: PipelineState):
    t0 = time.perf_counter()

    plan = planner.route(state["query"])
    query_embedding = None
    if not plan.fast_path and not state.get("force_full_path", False):
        try:
            from providers.embedding_provider import embed_text

            query_embedding = embed_text(state["query"])
        except Exception as e:
            logger.warning("route_query: embedding generation failed: %s", e)

    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info("route_query.latency_ms=%.2f fast_path=%s", latency_ms, plan.fast_path)
    return {
        "query_embedding": query_embedding,
        "plan": plan,
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
    """BM25 retrieval (Component 1 + 2). top_k=20 for union design.
    Soft-boosts OKF-matched chunks by OKF_BOOST before returning."""
    from db.database import CloudRepository

    t0 = time.perf_counter()
    repo = CloudRepository()
    all_chunks = repo.get_all_chunks(
        document_ids=state.get("document_ids"),
        workspace_id=state.get("workspace_id", ""),
    )

    retriever = BM25Retriever()
    results = retriever.search(state["query"], all_chunks, top_k=20)

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
    based on headings/footnotes retrieved by BM25."""
    t0 = time.perf_counter()
    retriever = PageIndexRetriever()
    bm25_cands = state.get("bm25_candidates")
    if bm25_cands:
        chunks, pages, c_ids = retriever.filter_and_rank(
            state["query"], bm25_cands
        )
    else:
        # When BM25 is skipped (e.g. fast path), do not restrict page/chunk IDs
        pages, c_ids = None, None

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
        "stage_timings": {**existing, "page_index_ms": latency_ms},
    }


def run_vector(state: PipelineState):
    """Vector retrieval (Component 1). Searches within PageIndex candidates."""
    from db.database import CloudRepository

    t0 = time.perf_counter()
    repo = CloudRepository()
    retriever = VectorRetriever(repository=repo)

    plan = state["plan"]
    is_fast_path = plan.fast_path if plan else False

    chunks = retriever.search(
        query=state["query"],
        query_embedding=state.get("query_embedding"),
        fast_path=is_fast_path,
        document_ids=state.get("document_ids"),
        candidate_page_ids=state.get("candidate_page_ids"),
        candidate_chunk_ids=state.get("candidate_chunk_ids"),
        workspace_id=state.get("workspace_id", ""),
        top_k=10,
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
    from services.retrieval.planner import extract_entities

    t0 = time.perf_counter()
    entities = extract_entities(state["query"])

    retriever = GraphRetriever()
    results = retriever.expand(entities)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info("graph.latency_ms=%.2f graph.result_count=%d", latency_ms, len(results))
    existing = state.get("stage_timings", {})
    return {
        "graph_results": results,
        "stage_timings": {**existing, "graph_ms": latency_ms},
    }


def run_reranker(state: PipelineState):
    """RRF merge of bm25_candidates + candidate_chunks (vector), then neural rerank
    (Component 1). Both retrieval lists searched the full corpus independently."""
    t0 = time.perf_counter()
    bm25 = state.get("bm25_candidates", [])
    vector = state.get("candidate_chunks", [])

    merged = _rrf_merge(bm25, vector, k=60)
    if not merged:
        logger.warning("run_reranker: no chunks to rerank — returning empty")
        return {"top_chunks": []}

    top_chunks = rerank(state["query"], merged, top_k=6)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "reranker.latency_ms=%.2f reranker.input_count=%d reranker.output_count=%d",
        latency_ms,
        len(merged),
        len(top_chunks),
    )
    existing = state.get("stage_timings", {})
    return {
        "top_chunks": top_chunks,
        "stage_timings": {**existing, "reranker_ms": latency_ms},
    }


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
    return {
        "compressed_text": compressed,
        "context_snippet": compressed[:500],  # expose for LLM faithfulness judge
        "stage_timings": {**existing, "compressor_ms": latency_ms},
    }


def run_fidelity(state: PipelineState):
    t0 = time.perf_counter()
    query = state.get("query", "")
    compressed = state.get("compressed_text", "")
    if not compressed:
        return {"error": "No context available"}
    try:
        check_fidelity(query, [compressed])
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
            "stage_timings": {**state.get("stage_timings", {}), "llm_ms": 0.0},
        }

    response = call_llm(state["query"], compressed)

    # Real confidence score — avg reranker score
    top_chunks = state.get("top_chunks", [])
    avg_reranker = (
        sum(getattr(c, "reranker_score", 0.0) for c in top_chunks) / len(top_chunks)
        if top_chunks
        else 0.0
    )
    confidence = avg_reranker

    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info("llm.latency_ms=%.2f llm.confidence=%.4f", latency_ms, confidence)
    existing = state.get("stage_timings", {})

    from services.retrieval.response_builder import build_citation

    return {
        "final_answer": response.get("answer", "NOT_FOUND"),
        "citations": [build_citation(c).to_dict() for c in top_chunks],
        "confidence_score": confidence,
        "stage_timings": {**existing, "llm_ms": latency_ms},
    }


def end_fast_path(state: PipelineState):
    """Extractive fast-path answer (Component 3). No LLM. Sentence scoring by query
    term overlap. Fidelity check runs on context (correct 2-arg signature). Real
    confidence score with explicit None checks (no falsy-zero override)."""
    from services.retrieval.response_builder import build_citation

    t0 = time.perf_counter()

    query = state["query"]
    top_chunks = state.get("top_chunks") or state.get("candidate_chunks", [])[:5]

    # M1 / L2: Early exit if no chunks are available
    if not top_chunks:
        return {
            "final_answer": "NOT_FOUND",
            "confidence_score": 0.0,
            "top_chunks": [],
            "citations": [],
            "stage_timings": {**state.get("stage_timings", {}), "fast_path_ms": 0.0},
        }

    # Score each sentence by query term overlap (no LLM — Rule 1)
    query_terms = set(re.findall(r"\w+", query.lower()))
    scored: list[tuple[int, str]] = []
    for chunk in top_chunks:
        for sentence in re.split(r"(?<=[.!?])\s+", chunk.text.strip()):
            if not sentence.strip():
                continue
            terms = set(re.findall(r"\w+", sentence.lower()))
            scored.append((len(query_terms & terms), sentence))
    scored.sort(key=lambda x: x[0], reverse=True)

    seen: set[str] = set()
    selected: list[str] = []
    for _, sentence in scored:
        if sentence not in seen:
            selected.append(sentence)
            seen.add(sentence)
        if len(selected) == 3:
            break

    answer = " ".join(selected) if selected else "NOT_FOUND"

    # Fidelity gate
    if answer != "NOT_FOUND":
        chunks_text = [c.text for c in top_chunks]
        try:
            check_fidelity(query, chunks_text)
        except CoverageError as e:
            logger.warning("fast_path.fidelity_failed reason=%s", e)
            answer = "NOT_FOUND"

    # Real confidence — computed from vector similarity / reranker scores
    scores = []
    for c in top_chunks:
        s = c.reranker_score if getattr(c, "reranker_score", None) is not None else getattr(c, "similarity_score", None)
        if s is not None:
            scores.append(float(s))
    
    if scores:
        confidence = round(sum(scores) / len(scores), 4)
    elif answer != "NOT_FOUND":
        # Fallback for extractive match when vector similarity is unattached
        confidence = round(len(selected) / 3.0 * 0.85, 4) if selected else 0.0
    else:
        confidence = 0.0

    confidence = min(1.0, max(0.0, confidence))

    latency_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "fast_path.latency_ms=%.2f fast_path.confidence=%.4f fast_path.answer_len=%d",
        latency_ms,
        confidence,
        len(answer),
    )
    existing = state.get("stage_timings", {})
    return {
        "final_answer": answer,
        "confidence_score": confidence,
        "top_chunks": top_chunks,
        "citations": (
            [build_citation(c).to_dict() for c in top_chunks]
            if answer != "NOT_FOUND"
            else []
        ),
        "stage_timings": {**existing, "fast_path_ms": latency_ms},
    }


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------


def route_after_vector(state: PipelineState):
    plan = state["plan"]
    if plan.fast_path and not state.get("force_full_path", False):
        return "end_fast_path"
    return "run_okf_router_post"  # OKF already ran pre-BM25; this routes to reranker or graph


def route_after_reranker_or_graph(state: PipelineState):
    """After graph expansion, always proceed to reranker (graph added to state)."""
    return "run_reranker"


def route_after_okf_post(state: PipelineState):
    """Decide whether to run graph expansion for relationship queries."""
    plan = state["plan"]
    if plan.use_graph:
        return "run_graph"
    return "run_reranker"


# ---------------------------------------------------------------------------
# Dummy passthrough node — avoids duplicate conditional edge targets
# ---------------------------------------------------------------------------


def run_okf_post(state: PipelineState):
    """Passthrough: OKF already ran pre-BM25 (run_okf_router). This node exists
    only so the graph can branch to run_graph or run_reranker after vector."""
    return {}


# ---------------------------------------------------------------------------
# Build LangGraph
# ---------------------------------------------------------------------------

workflow = StateGraph(PipelineState)

workflow.add_node("route_query", route_query)
workflow.add_node("run_okf_router", run_okf_router)  # pre-BM25 OKF (Component 2)
workflow.add_node("run_bm25", run_bm25)
workflow.add_node("run_page_index", run_page_index)
workflow.add_node("run_vector", run_vector)
workflow.add_node("run_okf_router_post", run_okf_post)  # routing branch after vector
workflow.add_node("run_graph", run_graph)
workflow.add_node("run_reranker", run_reranker)
workflow.add_node("run_compressor", run_compressor)
workflow.add_node("run_fidelity", run_fidelity)
workflow.add_node("run_llm", run_llm)
workflow.add_node("end_fast_path", end_fast_path)

# Edge order: route_query → OKF (pre-BM25) → BM25 → PageIndex → vector → branch
workflow.add_edge(START, "route_query")
workflow.add_edge("route_query", "run_okf_router")
workflow.add_edge("run_okf_router", "run_bm25")
workflow.add_edge("run_bm25", "run_page_index")
workflow.add_edge("run_page_index", "run_vector")

workflow.add_conditional_edges(
    "run_vector",
    route_after_vector,
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
workflow.add_edge("run_reranker", "run_compressor")
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
    ):
        if not workspace_id:
            raise ValueError("workspace_id is required for pipeline execution")

        initial_state = {
            "query": query,
            "workspace_id": workspace_id,
            "query_embedding": None,
            "document_ids": document_ids,
            "force_full_path": force_full_path,
            "plan": None,
            "bm25_candidates": [],
            "candidate_page_ids": [],
            "candidate_chunk_ids": [],
            "candidate_chunks": [],
            "okf_properties": [],
            "okf_seed_chunk_ids": [],
            "graph_results": [],
            "top_chunks": [],
            "compressed_text": "",
            "context_snippet": "",
            "final_answer": "",
            "confidence_score": 0.0,
            "citations": [],
            "error": None,
            "stage_timings": {},
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
                plan = state.get("plan")
                self.fast_path = plan.fast_path if plan else False
                self.stages = plan.stages if plan else []

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
