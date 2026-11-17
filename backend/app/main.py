import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .auth import current_user, easy_auth_enabled
from .config import settings
from .routers import actions, admin, auth, comparison, guide, overview, registers, site_checks
from .seed.seed import ensure_seeded

log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.auth_mode == "local":
        log.warning("AUTH_MODE=local: no sign-in, everyone is %s. Use AUTH_MODE=microsoft on Azure App Service.", settings.local_user_email)
    elif settings.auth_mode == "dev":
        log.warning("AUTH_MODE=dev: the API trusts an 'Authorization: Dev <email>' header. For tests only.")
    elif settings.auth_mode == "microsoft" and not easy_auth_enabled():
        log.error("AUTH_MODE=microsoft but App Service Authentication is not on (WEBSITE_AUTH_ENABLED is not True): every request will be refused.")
    if settings.seed_on_startup:
        log.info("Database: %s", ensure_seeded())
    yield


app = FastAPI(title="Retail AI Readiness API", version="3.0.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
# CORS_ORIGINS lists the sites allowed to call this API from a browser (the frontend's address when
# it is hosted separately). Credentials are allowed so the App Service sign-in cookie is sent.
app.add_middleware(CORSMiddleware, allow_origins=[o.strip().rstrip("/") for o in settings.cors_origins.split(",") if o.strip()],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

signed_in = [Depends(current_user)]
app.include_router(auth.router)                       # /api/auth/config is public; /api/auth/me needs sign-in
app.include_router(overview.router, dependencies=signed_in)
app.include_router(actions.router, dependencies=signed_in)
app.include_router(guide.router, dependencies=signed_in)
app.include_router(registers.router)                  # signed-in users
app.include_router(comparison.router)                 # signed-in users
app.include_router(site_checks.router)                # signed-in users
app.include_router(admin.router)                      # admins only


@app.get("/api/health")
def health():
    return {"status": "ok", "auth_mode": settings.auth_mode}


# ---- The built React app, served from the same site so /.auth/* and /api/* share one origin
DIST = Path(settings.frontend_dist)
if (DIST / "index.html").is_file():
    if (DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Not found")
        f = (DIST / path).resolve()
        if path and f.is_file() and DIST.resolve() in f.parents:
            return FileResponse(f)
        return FileResponse(DIST / "index.html")