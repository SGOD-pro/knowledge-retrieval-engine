from fastapi import APIRouter
from schemas.models import QueryRequest
from modules.query.query_service import query_service

router = APIRouter(tags=["query"])


@router.post("/query")
def query_endpoint(req: QueryRequest):
    return query_service.execute_query(req)
