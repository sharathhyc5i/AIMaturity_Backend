from fastapi import APIRouter, Depends

from ..auth import CurrentUser, current_user, easy_auth_enabled
from ..config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/config")
def auth_config():
    """Public: how the web app should sign people in."""
    return dict(
        mode=settings.auth_mode,
        # App Service Authentication endpoints (same site as the app)
        me_url="/.auth/me",
        login_url="/.auth/login/aad",
        logout_url="/.auth/logout",
        ready=settings.auth_mode != "microsoft" or easy_auth_enabled(),
        allowed_domains=sorted(settings.domain_set),
    )


@router.get("/me")
def me(user: CurrentUser = Depends(current_user)):
    return dict(email=user.email, name=user.name, role=user.role)
