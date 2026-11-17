"""Sign-in and roles.

Microsoft SSO (AUTH_MODE=microsoft) uses Azure App Service Authentication ("Easy Auth")
with the Microsoft identity provider:

  * App Service signs the person in with Microsoft before the request reaches us.
  * The browser reads the signed-in user from /.auth/me (email and name).
  * For every request App Service adds X-MS-CLIENT-PRINCIPAL (base64 JSON of the
    user's claims) and X-MS-CLIENT-PRINCIPAL-NAME, and strips any copies a client
    tries to send. The API reads the user from those headers only.

Because those headers are only trustworthy behind App Service Authentication, the
API refuses to run in this mode unless App Service reports it is switched on
(WEBSITE_AUTH_ENABLED=True).

Role rules, in order:
  * an email in ADMIN_EMAILS                       -> admin (cannot be removed)
  * an email an admin added in Users               -> the role the admin gave it
  * an email ending in an ALLOWED_DOMAINS domain   -> user (created on first sign-in)
  * anything else, or a user an admin removed      -> 403

AUTH_MODE=local (the default) has no sign-in: every request is LOCAL_USER_EMAIL, run
through the same role rules, so switching to AUTH_MODE=microsoft needs no code change.
AUTH_MODE=dev accepts "Authorization: Dev <email>" and is used by the tests.
"""
import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .config import settings
from .db import get_db

EMAIL_CLAIMS = (
    "preferred_username", "email", "emails", "upn",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
)
NAME_CLAIMS = ("name", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")


@dataclass
class CurrentUser:
    id: int
    email: str
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def easy_auth_enabled() -> bool:
    """App Service sets WEBSITE_AUTH_ENABLED=True when Authentication is switched on for the app."""
    return os.environ.get("WEBSITE_AUTH_ENABLED", "").strip().lower() == "true"


def parse_principal(encoded: str) -> dict:
    """Decode X-MS-CLIENT-PRINCIPAL into {auth_typ, name_typ, claims: {typ: val}}."""
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4))
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(401, "The Microsoft sign-in information could not be read. Sign in again.") from exc
    claims: dict[str, str] = {}
    for c in data.get("claims", []) or []:
        typ, val = c.get("typ"), c.get("val")
        if typ and val and typ not in claims:
            claims[typ] = val
    return {"auth_typ": data.get("auth_typ"), "name_typ": data.get("name_typ"), "claims": claims}


def identity_from_headers(principal: str | None, principal_name: str | None, idp: str | None) -> tuple[str, str]:
    if not principal and not principal_name:
        raise HTTPException(401, "Sign in with Microsoft to continue.")
    if idp and idp.lower() not in ("aad", "microsoft", "azureactivedirectory"):
        raise HTTPException(403, "Sign in with your Microsoft work account.")
    claims = parse_principal(principal)["claims"] if principal else {}
    email = next((claims[k] for k in EMAIL_CLAIMS if "@" in claims.get(k, "")), None) or principal_name or ""
    if "@" not in email:
        raise HTTPException(403, "Your Microsoft account has no email address to check.")
    name = next((claims[k] for k in NAME_CLAIMS if claims.get(k)), "")
    return email.strip().lower(), name.strip()


def resolve_user(db: Session, email: str, name: str = "") -> m.User:
    email = email.strip().lower()
    domain = email.rsplit("@", 1)[-1]
    user = db.scalar(select(m.User).where(m.User.email == email))
    now = datetime.now(timezone.utc)

    if email in settings.admin_set:
        if not user:
            user = m.User(email=email, name=name, role="admin", status="active", source="bootstrap")
            db.add(user)
        user.role, user.status, user.source = "admin", "active", "bootstrap"
    elif user:
        if user.status != "active":
            raise HTTPException(403, "An administrator removed your access to this app. Ask an admin to add you again.")
    elif domain in settings.domain_set:
        user = m.User(email=email, name=name, role="user", status="active", source="domain")
        db.add(user)
    else:
        allowed = " or ".join(f"@{d}" for d in sorted(settings.domain_set))
        raise HTTPException(403, f"{email} is not allowed. Sign in with a {allowed} account, or ask an admin to add you.")

    # Keep the display name in step with Microsoft
    if name and user.name != name:
        user.name = name
    user.last_login = now
    db.commit()
    db.refresh(user)
    return user


def current_user(
    authorization: str | None = Header(default=None),
    x_ms_client_principal: str | None = Header(default=None),
    x_ms_client_principal_name: str | None = Header(default=None),
    x_ms_client_principal_idp: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> CurrentUser:
    if settings.auth_mode == "microsoft":
        if not easy_auth_enabled():
            raise HTTPException(500, "Microsoft sign-in is not switched on for this app. Turn on App Service Authentication, or set AUTH_MODE=local for local work.")
        email, name = identity_from_headers(x_ms_client_principal, x_ms_client_principal_name, x_ms_client_principal_idp)
        user = resolve_user(db, email, name)
    elif settings.auth_mode == "local":
        user = resolve_user(db, settings.local_user_email, settings.local_user_name)
    elif settings.auth_mode == "dev":
        scheme, _, value = (authorization or "").partition(" ")
        value = value.strip()
        if scheme.lower() != "dev" or "@" not in value:
            raise HTTPException(401, "Sign in to continue.")
        user = resolve_user(db, value, value.split("@")[0].replace(".", " ").title())
    else:
        raise HTTPException(500, f"Unknown AUTH_MODE '{settings.auth_mode}'. Use 'local', 'microsoft' or 'dev'.")
    return CurrentUser(user.id, user.email, user.name, user.role)


def require_admin(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(403, "Only admins can do this.")
    return user
