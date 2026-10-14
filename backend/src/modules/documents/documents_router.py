from typing import Any
from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, UploadFile, status
from modules.documents.documents_service import documents_service
from src.security.auth import require_auth_context
from src.schemas.contracts.envelope import IdentityEnvelope, TrustedAuthContext

router = APIRouter(tags=["documents"])


@router.post(
    "/workspaces/{workspace_id}/documents", status_code=status.HTTP_201_CREATED
)
async def upload_workspace_documents_endpoint(
    workspace_id: str,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    auth_context: Any = Depends(require_auth_context),
):
    if isinstance(auth_context, TrustedAuthContext):
        try:
            IdentityEnvelope(workspace_id=workspace_id).validate_auth(auth_context)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")

    return await documents_service.upload_workspace_documents(
        workspace_id=workspace_id,
        background_tasks=background_tasks,
        files=files,
    )


@router.post("/ingest", deprecated=True)
async def ingest(
    file: UploadFile = File(...),
    authorization: str | None = Header(None),
):
    """Legacy ingest endpoint deprecated in favor of /workspaces/{workspace_id}/documents.

    Validates bearer token when authorization is provided or when auth is strictly required.
    """
    import os
    from config import settings
    from src.security.auth import extract_bearer_token, TestAuthRegistry

    if settings.KRE_AUTH_MODE != "test" or os.environ.get("AUTH_REQUIRED", "").lower() == "true":
        token = extract_bearer_token(authorization)
        TestAuthRegistry.resolve_token_principal(token)

    return await documents_service.ingest_direct(file)


@router.get("/documents/{document_id}")
def get_document(document_id: str):
    return documents_service.get_document(document_id)


@router.get("/documents/{document_id}/file")
def get_document_file_endpoint(document_id: str):
    return documents_service.get_document_file(document_id)
