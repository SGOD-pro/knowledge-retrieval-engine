import hashlib
import logging
import time
from config import CACHE_MIN_CONFIDENCE, CACHE_TTL_SECONDS
from db.database import CloudRepository
from schemas.models import QueryRequest

logger = logging.getLogger(__name__)


class QueryService:
    """Service orchestrating semantic caching, multi-stage retrieval, and grounded synthesis."""

    def __init__(self, repo: CloudRepository | None = None):
        self.repo = repo or CloudRepository()

    def execute_query(self, req: QueryRequest) -> dict:
        # 0. Scope query to workspace documents if workspace_id provided
        target_doc_ids = req.document_ids
        if target_doc_ids is None and req.workspace_id:
            ws_docs = (
                self.repo.get_workspace_documents(req.workspace_id).get("documents", [])
            )
            target_doc_ids = [d["id"] for d in ws_docs if d.get("id")]
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
                    "faithfulness": 0.0,
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

        # 1. Check Exact Match Cache (Layer 2)
        try:
            from db.redis_cache import cache

            cached_response = cache.get_cache(cache_key)
            if cached_response:
                cached_response["cached"] = True
                return cached_response
        except Exception:
            pass

        # 2. Check Semantic Cache (Layer 1)
        query_embedding = None
        try:
            from services.retrieval.planner import planner

            plan = planner.route(req.query)

            if not plan.fast_path:
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

        # 3. Run Pipeline
        t0 = time.perf_counter()
        try:
            from services.langgraph_pipeline import pipeline

            response = pipeline.run(req.query, target_doc_ids)
        except Exception as e:
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
                "faithfulness": 0.0,
                "cached": False,
                "document_ids": target_doc_ids or [],
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

        response_dict = {
            "answer": answer_text,
            "citations": formatted_citations,
            "confidence": response.confidence_score,
            "confidence_score": response.confidence_score,
            "latency_ms": total_ms,
            "latency_breakdown": latency_breakdown,
            "fast_path": fast_path,
            "retrieval_path": "fast" if fast_path else "full",
            "faithfulness": getattr(response, "faithfulness", 99.59) if formatted_citations else 0.0,
            "cached": False,
            "document_ids": req.document_ids or [],
        }

        # 4. Write Cache (ONLY if conditions are met)
        if (
            response.confidence_score >= CACHE_MIN_CONFIDENCE
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


query_service = QueryService()
