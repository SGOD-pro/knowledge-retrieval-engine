import hashlib
import os
import tempfile
import time
from pathlib import Path
from fastapi import APIRouter, File, HTTPException, UploadFile

from config import settings
from db.database import CloudRepository
from ingestion_lambda.format_router import SUPPORTED_FORMATS
from ingestion_lambda.parse_service import ingest_document
from schemas.models import QueryRequest

router = APIRouter()


def repository() -> CloudRepository:
    return CloudRepository()


@router.post("/ingest")
async def ingest(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_FORMATS:
        raise HTTPException(415, f"Supported formats: {', '.join(sorted(SUPPORTED_FORMATS))}")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        temporary.write(await file.read())
        path = Path(temporary.name)
    try:
        document = ingest_document(path, provider=settings.MODEL_PROVIDER)
        repository().save(document)
        return {"id": document.id, "filename": document.filename, "source_format": document.source_format, "chunk_count": len(document.chunks)}
    finally:
        path.unlink(missing_ok=True)


@router.get("/documents/{document_id}")
def get_document(document_id: str):
    document = repository().get(document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    return document.to_dict()


@router.post("/query")
def query_endpoint(req: QueryRequest):
    from db.redis_cache import cache
    from config import settings, CACHE_MIN_CONFIDENCE, CACHE_TTL_SECONDS
    from services.langgraph_pipeline import pipeline
    
    # Compute cache key per MEMORY.md specification
    query_norm = req.query.strip().lower()
    doc_scope_hash = ""
    if req.document_ids:
        sorted_ids = sorted(req.document_ids)
        doc_scope_hash = hashlib.sha256(",".join(sorted_ids).encode("utf-8")).hexdigest()
        
    raw_key = (query_norm + doc_scope_hash).encode("utf-8")
    cache_key = f"query:{hashlib.sha256(raw_key).hexdigest()}"
    
    # 1. Check Exact Match Cache (Layer 2)
    cached_response = cache.get_cache(cache_key)
    if cached_response:
        cached_response["cached"] = True
        return cached_response

    # 2. Check Semantic Cache (Layer 1)
    from services.retrieval.planner import planner
    plan = planner.route(req.query)
    
    query_embedding = None
    if not plan.fast_path:
        from providers.embedding_provider import embed_text
        from providers.provider_client import get_active_provider
        provider = req.provider or get_active_provider()
        
        query_embedding = embed_text(req.query, provider=provider)
        semantic_key = repository().check_semantic_cache(query_embedding, doc_scope_hash, provider)
        
        if semantic_key:
            cached_response = cache.get_cache(semantic_key)
            if cached_response:
                cached_response["cached"] = True
                return cached_response

    # 3. Run Pipeline
    t0 = time.perf_counter()
    response = pipeline.run(req.query, req.document_ids)
    t1 = time.perf_counter()
    
    total_ms = round((t1 - t0) * 1000.0, 2)
    # Real per-stage timings accumulated by each pipeline node (Component 4).
    # stage_timings keys: route_query_ms, okf_router_ms, bm25_ms, vector_ms,
    #                     reranker_ms, compressor_ms, fidelity_ms, llm_ms, fast_path_ms
    stage_timings = getattr(response, "stage_timings", {})
    latency_breakdown = {**stage_timings, "total_ms": total_ms}

    fast_path = response.fast_path
    
    # response.citations are now fully built citation dictionaries directly from the pipeline
    final_citations = response.citations

    response_dict = {
        "answer": response.answer,
        "citations": final_citations,
        "confidence_score": response.confidence_score,
        "latency_breakdown": latency_breakdown,
        "fast_path": fast_path,
        "cached": False,
        "document_ids": req.document_ids or [],
        "retrieval_path": getattr(response, "stages", []),
    }
    
    # 4. Write Cache (ONLY if conditions are met)
    if response.confidence_score >= CACHE_MIN_CONFIDENCE and response.answer != "NOT_FOUND":
        cache.set_cache(cache_key, response_dict, CACHE_TTL_SECONDS)
        if query_embedding:
            from providers.provider_client import get_active_provider
            provider = req.provider or get_active_provider()
            repository().save_semantic_cache(cache_key, query_embedding, doc_scope_hash, provider)
        
    return response_dict
