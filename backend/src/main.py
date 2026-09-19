import os
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import router

app = FastAPI(
    title="Knowledge Retrieval Engine API",
    version="v0.1.0",
    description="Enterprise Document Intelligence & Grounded Retrieval Engine",
)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if os.environ.get("AUTH_REQUIRED", "").lower() == "true":
        auth = request.headers.get("Authorization", "")
        if not auth or auth == "Bearer invalid-secret":
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)

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
    allow_methods=["GET", "POST", "OPTIONS","DELETE"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")
app.include_router(router)


