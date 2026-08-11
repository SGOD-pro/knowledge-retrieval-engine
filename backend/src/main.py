from fastapi import FastAPI
from api.routes import router

app = FastAPI(title="Knowledge Retrieval Engine")

app.include_router(router)
