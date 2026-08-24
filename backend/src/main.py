import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router

app = FastAPI(
    title="Knowledge Retrieval Engine API",
    version="v0.1.0",
    description="Enterprise Document Intelligence & Grounded Retrieval Engine",
)

# CORS middleware for local and dev frontend ports
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Mount both with /api/v1 prefix (contract standard) and root prefix (direct access)
app.include_router(router, prefix="/api/v1")
app.include_router(router)
