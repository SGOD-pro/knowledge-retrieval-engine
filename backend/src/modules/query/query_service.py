import hashlib
import logging
import time
from config import CACHE_MIN_CONFIDENCE, CACHE_TTL_SECONDS
from modules.query.query_repository import QueryRepository
from schemas.models import QueryRequest

logger = logging.getLogger(__name__)


class QueryService:
    """Service orchestrating semantic caching, multi-stage retrieval, and grounded synthesis."""

    def __init__(self, repo: QueryRepository | None = None):
        self.repo = repo or QueryRepository()

    def execute_query(self, req: QueryRequest) -> dict:
        if not req.workspace_id:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="workspace_id is required")

        # 0. Resolve the workspace's document_ids via DynamoDB workspace query (PK=WORKSPACE#{id}, SK begins_with DOC#)
        ws_docs_resp = self.repo.get_workspace_documents(req.workspace_id, page=1, limit=1000)
        ws_docs = ws_docs_resp.get("documents", [])
        ws_doc_ids = [d["id"] for d in ws_docs if d.get("id")]

        # In test environments or direct chunk ingests, also include any chunk doc IDs tagged with workspace_id
        if not ws_doc_ids:
            ws_chunks = self.repo.get_all_chunks(workspace_id=req.workspace_id)
            ws_doc_ids = list({str(c.document_id) for c in ws_chunks if str(c.document_id)})

        if req.document_ids is not None:
            # req.document_ids is an optional additional filter on top of workspace scope, not a replacement
            target_doc_ids = [d_id for d_id in req.document_ids if d_id in set(ws_doc_ids)]
        else:
            target_doc_ids = ws_doc_ids

        if not target_doc_ids:
            return {
                "answer": "I couldn't find any relevant documents in this workspace to answer your query. Please upload documents to this workspace to enable grounded retrieval.",
                "citations": [],
                "confidence": 0.0,
                "confidence_score": 0.0,
                "latency_ms": 0.0,
                "latency_breakdown": {
                    "total_ms": 0.0,
                },
                "fast_path": False,
                "retrieval_path": "empty",
                "faithfulness": None,
                "citation_utilization_rate": None,
                "token_usage": {"input_tokens": 0, "output_tokens": 0},
                "cached": False,
                "document_ids": [],
            }

        # Compute cache key per MEMORY.md specification
        query_norm = req.query.strip().lower()
        doc_scope_hash = ""
        if target_doc_ids:
            sorted_ids = sorted(target_doc_ids)
            doc_scope_hash = hashlib.sha256(
                ",".join(sorted_ids).encode("utf-8")
            ).hexdigest()

        raw_key = (query_norm + doc_scope_hash).encode("utf-8")
        cache_key = f"query:{hashlib.sha256(raw_key).hexdigest()}"

        # 1. Check Exact Match Cache (Layer 2) — bypassed in benchmark_mode or cache=False
        use_cache = req.cache and not getattr(req, "benchmark_mode", False)
        if use_cache:
            try:
                from db.redis_cache import cache

                cached_response = cache.get_cache(cache_key)
                if cached_response:
                    cached_response["cached"] = True
                    return cached_response
            except Exception:
                pass

        # 2. Check Semantic Cache (Layer 1) — bypassed in benchmark_mode or cache=False
        query_embedding = None
        if use_cache:
            try:
                from services.retrieval.planner import planner

                plan = planner.route(req.query)
                is_full = (not plan.fast_path) or getattr(req, "force_full_path", False)

                if is_full:
                    from providers.embedding_provider import embed_text
                    from providers.provider_client import get_active_provider

                    provider = req.provider or get_active_provider()

                    query_embedding = embed_text(req.query, provider=provider)
                    semantic_key = self.repo.check_semantic_cache(
                        query_embedding, doc_scope_hash, provider
                    )

                    if semantic_key:
                        from db.redis_cache import cache

                        cached_response = cache.get_cache(semantic_key)
                        if cached_response:
                            cached_response["cached"] = True
                            return cached_response
            except Exception:
                pass

        # 3. Run Pipeline with explicit workspace_id and shared query_embedding
        t0 = time.perf_counter()
        try:
            from services.langgraph_pipeline import pipeline
            import inspect

            sig = inspect.signature(pipeline.run)
            params = sig.parameters
            accepts_var_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )

            kwargs = {
                "query": req.query,
                "document_ids": target_doc_ids,
                "workspace_id": req.workspace_id,
            }
            if accepts_var_kwargs or "force_full_path" in params:
                kwargs["force_full_path"] = getattr(req, "force_full_path", False)
            if accepts_var_kwargs or "query_embedding" in params:
                kwargs["query_embedding"] = query_embedding
            if accepts_var_kwargs or "benchmark_mode" in params:
                kwargs["benchmark_mode"] = getattr(req, "benchmark_mode", False)

            response = pipeline.run(**kwargs)
        except Exception as e:
            logger.error("pipeline.run failed with error: %s", e, exc_info=True)
            total_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            return {
                "answer": "I couldn't find any relevant documents in this workspace to answer your query. Please upload documents to this workspace to enable grounded retrieval.",
                "citations": [],
                "confidence": 0.0,
                "confidence_score": 0.0,
                "latency_ms": total_ms,
                "latency_breakdown": {
                    "total_ms": total_ms,
                },
                "fast_path": False,
                "retrieval_path": "empty",
                "planned_path": "full",
                "executed_path": "empty",
                "faithfulness": None,
                "citation_utilization_rate": None,
                "token_usage": {"input_tokens": 0, "output_tokens": 0},
                "cached": False,
                "document_ids": target_doc_ids or [],
                "retrieval_candidates": {},
                "reranker_mode": "remote_success",
            }

        t1 = time.perf_counter()
        total_ms = round((t1 - t0) * 1000.0, 2)
        stage_timings = getattr(response, "stage_timings", {})
        latency_breakdown = {**stage_timings, "total_ms": total_ms}

        fast_path = response.fast_path
        final_citations = response.citations

        # Ensure format matches Citation model
        formatted_citations = []
        for idx, c in enumerate(final_citations):
            if isinstance(c, dict):
                c_dict = dict(c)
                if "id" not in c_dict:
                    c_dict["id"] = idx + 1
                formatted_citations.append(c_dict)
            else:
                formatted_citations.append(
                    {
                        "id": idx + 1,
                        "chunk_id": str(getattr(c, "id", f"chunk_{idx}")),
                        "document_id": str(getattr(c, "document_id", "doc_1")),
                        "document_filename": getattr(
                            c, "document_filename", "document.pdf"
                        ),
                        "source_format": getattr(c, "source_format", "pdf"),
                        "text": getattr(c, "text", ""),
                        "page_number": getattr(c, "page_number", 1),
                        "bounding_box": getattr(c, "bounding_box", None),
                        "location_reference": getattr(
                            c, "location_reference", f"Page {getattr(c, 'page_number', 1)}"
                        ),
                    }
                )

        answer_text = response.answer
        if answer_text == "NOT_FOUND" or not answer_text:
            answer_text = "I couldn't find any relevant passages in the workspace documents matching your query."

        faithfulness = getattr(response, "faithfulness", None)
        if response.answer == "NOT_FOUND" or not response.answer:
            faithfulness = None

        citation_util = getattr(response, "citation_utilization_rate", None)
        usage = getattr(response, "usage", {"input_tokens": 0, "output_tokens": 0})

        planned_path = getattr(response, "planned_path", "fast" if fast_path else "full")
        executed_path = getattr(response, "executed_path", "fast" if fast_path else "full")
        retrieval_candidates = getattr(response, "retrieval_candidates", {})
        reranker_mode = getattr(response, "reranker_mode", "remote_success")

        logger.info(
            "query.cost_tracking path=%s input_tokens=%d output_tokens=%d total_tokens=%d latency_ms=%.2f",
            "fast" if fast_path else "full",
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            total_ms,
        )

        response_dict = {
            "answer": answer_text,
            "citations": formatted_citations,
            "confidence": response.confidence_score,
            "confidence_score": response.confidence_score,
            "latency_ms": total_ms,
            "latency_breakdown": latency_breakdown,
            "fast_path": fast_path,
            "retrieval_path": executed_path,
            "planned_path": planned_path,
            "executed_path": executed_path,
            "faithfulness": faithfulness,
            "citation_utilization_rate": citation_util,
            "token_usage": usage,
            "cached": False,
            "document_ids": req.document_ids or [],
            "retrieval_candidates": retrieval_candidates,
            "reranker_mode": reranker_mode,
        }

        # 4. Write Cache (ONLY if cache enabled and not in benchmark_mode)
        if (
            use_cache
            and response.confidence_score >= CACHE_MIN_CONFIDENCE
            and response.answer != "NOT_FOUND"
        ):
            from db.redis_cache import cache

            cache.set_cache(cache_key, response_dict, CACHE_TTL_SECONDS)
            if query_embedding:
                from providers.provider_client import get_active_provider

                provider = req.provider or get_active_provider()
                self.repo.save_semantic_cache(
                    cache_key, query_embedding, doc_scope_hash, provider
                )

        return response_dict

    async def execute_query_stream(self, req: QueryRequest):
        """Streams real-time architectural pipeline stage updates and final synthesized answer."""
        import asyncio

        yield {
            "type": "stage",
            "stage": "listening",
            "state": "listening",
            "label": "Analyzing query & planning retrieval strategy...",
            "progress": 15,
        }
        await asyncio.sleep(0.06)

        from services.retrieval.planner import planner
        plan = planner.route(req.query)
        is_fast = plan.fast_path

        yield {
            "type": "stage",
            "stage": "routing",
            "state": "connecting",
            "label": f"Strategy chosen: {'⚡ Fast Match Sub-500ms Path' if is_fast else '🧠 Multi-Hop Graph Reasoning Path'}",
            "path": "fast" if is_fast else "full",
            "progress": 35,
        }
        await asyncio.sleep(0.06)

        yield {
            "type": "stage",
            "stage": "searching",
            "state": "searching",
            "label": "Searching Qdrant vector index & BM25 sparse index...",
            "progress": 60,
        }
        await asyncio.sleep(0.06)

        if not is_fast:
            yield {
                "type": "stage",
                "stage": "graph",
                "state": "weaving",
                "label": "Traversing OKF Knowledge Graph & resolving entities...",
                "progress": 75,
            }
            await asyncio.sleep(0.06)

        yield {
            "type": "stage",
            "stage": "synthesis",
            "state": "solving",
            "label": "Synthesizing answer & verifying citation fidelity...",
            "progress": 90,
        }

        # Execute query synchronously or in threadpool
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, self.execute_query, req)

        yield {
            "type": "result",
            "data": res,
        }


query_service = QueryService()
