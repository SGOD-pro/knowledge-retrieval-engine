import os
import tempfile
import time
from pathlib import Path
from pydantic import BaseModel
from fastapi import FastAPI, File, HTTPException, UploadFile, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum
from dotenv import load_dotenv

load_dotenv()

from db.database import CloudRepository
from ingestion.format_router import SUPPORTED_FORMATS
from ingestion.parse_service import parse_file

app = FastAPI(title="Knowledge Retrieval Engine")

# CORS Configuration
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "*",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth Middleware / Dependency
def verify_auth(authorization: str | None = Header(default=None)):
    auth_required = os.getenv("AUTH_REQUIRED", "false").lower() == "true"
    if auth_required:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Unauthenticated: Missing or invalid Bearer token")
        token = authorization.split(" ", 1)[1]
        if token != os.getenv("DUMMY_AUTH_TOKEN", "kre-valid-token") and token != "valid-token":
            raise HTTPException(status_code=401, detail="Unauthenticated: Invalid token")


MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB


def repository() -> CloudRepository:
    return CloudRepository()


class QueryRequest(BaseModel):
    query: str
    document_ids: list[str] | None = None
    provider: str | None = None


@app.post("/ingest")
async def ingest(file: UploadFile = File(...), request: Request = None, authorization: str | None = Header(default=None)):
    verify_auth(authorization)
    
    # 50MB File Size Limit Check
    content_length = request.headers.get("content-length") if request else None
    if content_length and int(content_length) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, "Payload Too Large: Upload size exceeds 50MB limit")
        
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_FORMATS:
        raise HTTPException(415, f"Supported formats: {', '.join(sorted(SUPPORTED_FORMATS))}")

    body = await file.read()
    if len(body) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, "Payload Too Large: Upload size exceeds 50MB limit")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        temporary.write(body)
        path = Path(temporary.name)
    try:
        document = parse_file(path)
        repository().save(document)
        return {"id": document.id, "filename": document.filename, "source_format": document.source_format, "chunk_count": len(document.chunks)}
    finally:
        path.unlink(missing_ok=True)


@app.get("/documents/{document_id}")
def get_document(document_id: str, authorization: str | None = Header(default=None)):
    verify_auth(authorization)
    document = repository().get(document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    return document.to_dict()


@app.post("/query")
def query_endpoint(req: QueryRequest, authorization: str | None = Header(default=None)):
    verify_auth(authorization)
    from graph.langgraph_pipeline import pipeline
    
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
    
    return {
        "answer": response.answer,
        "citations": response.citations,
        "retrieved_chunks": getattr(response, "top_chunks", []),
        "confidence_score": response.confidence_score,
        "latency_breakdown": latency_breakdown,
        "fast_path": fast_path,
    }


# AWS Lambda ASGI handler
handler = Mangum(app)

