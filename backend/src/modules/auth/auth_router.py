from fastapi import APIRouter
from schemas.models import AuthResponse, LoginRequest
from modules.auth.auth_service import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=AuthResponse)
def login_endpoint(credentials: LoginRequest):
    return auth_service.login(credentials)


@router.get("/oauth/{provider}")
def oauth_login_endpoint(provider: str):
    return auth_service.oauth_login(provider)
