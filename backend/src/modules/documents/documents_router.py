from fastapi import APIRouter, BackgroundTasks, File, UploadFile, status
from modules.documents.documents_service import documents_service

router = APIRouter(tags=["documents"])


@router.post(
    "/workspaces/{workspace_id}/documents", status_code=status.HTTP_201_CREATED
)
async def upload_workspace_documents_endpoint(
    workspace_id: str,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
):
    return await documents_service.upload_workspace_documents(
        workspace_id=workspace_id,
        background_tasks=background_tasks,
        files=files,
    )


@router.post("/ingest")
async def ingest(file: UploadFile = File(...)):
    return await documents_service.ingest_direct(file)


@router.get("/documents/{document_id}")
def get_document(document_id: str):
    return documents_service.get_document(document_id)


@router.get("/documents/{document_id}/file")
def get_document_file_endpoint(document_id: str):
    return documents_service.get_document_file(document_id)
