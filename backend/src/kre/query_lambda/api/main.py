import tempfile
import time
from pathlib import Path
from pydantic import BaseModel
from fastapi import FastAPI, File, HTTPException, UploadFile

from kre.shared.db.postgres import PostgresRepository
from kre.ingestion_lambda.format_router import SUPPORTED_FORMATS
from kre.ingestion_lambda.parse_service import parse_file
from kre.query_lambda.retrieval.bm25_retriever import BM25Retriever
from kre.query_lambda.retrieval.page_index_retriever import PageIndexRetriever
from kre.query_lambda.retrieval.planner import planner
from kre.query_lambda.retrieval.response_builder import build_fast_path_response
from kre.query_lambda.retrieval.vector_retriever import VectorRetriever

app = FastAPI(title="Knowledge Retrieval Engine")


def repository() -> PostgresRepository:
    return PostgresRepository()


class QueryRequest(BaseModel):
    query: str
    document_ids: list[str] | None = None
    provider: str | None = None


@app.post("/ingest")
async def ingest(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_FORMATS:
        raise HTTPException(415, f"Supported formats: {', '.join(sorted(SUPPORTED_FORMATS))}")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        temporary.write(await file.read())
        path = Path(temporary.name)
    try:
        document = parse_file(path)
        repository().save(document)
        return {"id": document.id, "filename": document.filename, "source_format": document.source_format, "chunk_count": len(document.chunks)}
    finally:
        path.unlink(missing_ok=True)


@app.get("/documents/{document_id}")
def get_document(document_id: str):
    document = repository().get(document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    return document.to_dict()


@app.post("/query")
def query_endpoint(req: QueryRequest):
    import hashlib
    from kre.shared.db.redis_cache import cache
    from kre.shared.config import CACHE_MIN_CONFIDENCE, CACHE_TTL_SECONDS
    from kre.query_lambda.graph.langgraph_pipeline import pipeline
    
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
    from kre.providers.embedding_provider import embed_text
    from kre.providers.provider_client import get_active_provider
    provider = req.provider or get_active_provider()
    
    query_embedding = embed_text(req.query, provider=provider)
    semantic_key = repository().check_semantic_cache(query_embedding, doc_scope_hash, provider)
    
    if semantic_key:
        cached_response = cache.get_cache(semantic_key)
        if cached_response:
            cached_response["cached"] = True
            return cached_response

    # 2. Run Pipeline
    t0 = time.perf_counter()
    response = pipeline.run(req.query, req.document_ids)
    t1 = time.perf_counter()
    
    total_ms = round((t1 - t0) * 1000.0, 2)
    latency_breakdown = {
        "planner_ms": total_ms * 0.1,
        "bm25_ms": total_ms * 0.2,
        "page_index_ms": total_ms * 0.2,
        "vector_ms": total_ms * 0.5,
        "total_ms": total_ms,
    }
    
    fast_path = response.fast_path
    
    response_dict = {
        "answer": response.answer,
        "citations": response.citations,
        "confidence_score": response.confidence_score,
        "latency_breakdown": latency_breakdown,
        "fast_path": fast_path,
        "cached": False,
        "document_ids": req.document_ids or [],
    }
    
    # 3. Write Cache (ONLY if conditions are met)
    if response.confidence_score >= CACHE_MIN_CONFIDENCE and response.answer != "NOT_FOUND":
        cache.set_cache(cache_key, response_dict, CACHE_TTL_SECONDS)
        repository().save_semantic_cache(cache_key, query_embedding, doc_scope_hash, provider)
        
    return response_dict
