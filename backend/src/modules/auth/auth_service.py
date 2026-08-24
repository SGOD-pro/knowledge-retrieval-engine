import time
from schemas.models import AuthResponse, LoginRequest, User


class AuthService:
    """Service handling user authentication and OAuth sessions."""

    def login(self, credentials: LoginRequest) -> AuthResponse:
        return AuthResponse(
            access_token=f"mock_jwt_token_{int(time.time())}",
            token_type="bearer",
            expires_in=3600,
            user=User(
                id="usr_54321",
                email=credentials.email or "alexandra.chen@enterprise.com",
                name="Alexandra Chen",
                role="analyst",
                avatar=None,
            ),
        )

    def oauth_login(self, provider: str) -> dict:
        return {
            "provider": provider,
            "status": "connected",
            "redirect_url": f"/workspaces?sso={provider}",
        }


auth_service = AuthService()
