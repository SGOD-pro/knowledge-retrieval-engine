import json
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from schemas.models import QueryRequest
from modules.query.query_service import query_service

router = APIRouter(tags=["query"])


@router.post("/query")
def query_endpoint(req: QueryRequest):
    return query_service.execute_query(req)


@router.post("/query/stream")
async def query_stream_endpoint(req: QueryRequest):
    async def event_generator():
        async for event in query_service.execute_query_stream(req):
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
