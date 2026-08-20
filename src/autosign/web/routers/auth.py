from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from autosign.core.auth import SESSION_COOKIE_NAME, AdminAuthService, AuthConfigurationError
from autosign.core.config import Settings
from autosign.core.login_limiter import LoginAttemptLimiter
from autosign.web.schemas import AdminPasswordRequest, AuthStatus


def create_auth_router(
    *,
    settings: Settings,
    auth: AdminAuthService,
    login_limiter: LoginAttemptLimiter,
) -> APIRouter:
    router = APIRouter()

    def set_auth_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=settings.auth_session_hours * 3600,
            httponly=True,
            secure=settings.auth_secure_cookie,
            samesite="strict",
            path="/",
        )

    @router.get("/api/v1/auth/status", response_model=AuthStatus)
    async def auth_status(request: Request, response: Response) -> AuthStatus:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Vary"] = "Cookie"
        payload = request.state.auth_payload
        return AuthStatus(
            configured=settings.auth_disabled or auth.is_configured(),
            authenticated=payload is not None,
            csrf_token=str(payload["csrf"]) if payload is not None else None,
        )

    @router.post("/api/v1/auth/setup", response_model=AuthStatus)
    async def setup_admin(
        request_data: AdminPasswordRequest,
        response: Response,
    ) -> AuthStatus:
        if settings.auth_disabled:
            raise HTTPException(status_code=409, detail="Authentication is disabled.")
        try:
            auth.setup(request_data.password.get_secret_value())
        except AuthConfigurationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session = auth.issue_session()
        set_auth_cookie(response, session.token)
        return AuthStatus(
            configured=True,
            authenticated=True,
            csrf_token=session.csrf_token,
        )

    @router.post("/api/v1/auth/login", response_model=AuthStatus)
    async def login_admin(
        request: Request,
        request_data: AdminPasswordRequest,
        response: Response,
    ) -> AuthStatus:
        if not auth.is_configured():
            raise HTTPException(
                status_code=409,
                detail="Administrator password is not configured.",
            )
        # Deliberately use the direct peer address. X-Forwarded-For is untrusted
        # until AutoSign has an explicit trusted-proxy configuration contract.
        client_key = request.client.host if request.client else "unknown"
        if login_limiter.is_limited(client_key):
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts. Try again in one minute.",
            )
        if not auth.verify_password(request_data.password.get_secret_value()):
            login_limiter.record_failure(client_key)
            raise HTTPException(
                status_code=401,
                detail="Administrator password is incorrect.",
            )
        login_limiter.clear(client_key)
        session = auth.issue_session()
        set_auth_cookie(response, session.token)
        return AuthStatus(
            configured=True,
            authenticated=True,
            csrf_token=session.csrf_token,
        )

    @router.post("/api/v1/auth/logout", response_model=AuthStatus)
    async def logout_admin(response: Response) -> AuthStatus:
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return AuthStatus(configured=True, authenticated=False)

    return router
