from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    # Database: either one DATABASE_URL, or the separate DB_* values below.
    # When DB_HOST is set, the DB_* values are used and DATABASE_URL is ignored. They need no
    # URL-encoding, so a password with @ # / : ? works as typed.
    # Credentials are never stored here: set them in backend/.env (local) or in the App Service
    # application settings (Azure). The defaults below are only for a local PostgreSQL.
    database_url: str = "postgresql+psycopg://retail:retail@localhost:5432/retail_ai"
    db_host: str = ""
    db_port: int = 5432
    db_name: str = "ai_maturity"
    db_user: str = ""
    db_password: str = ""
    db_sslmode: str = "require"  # "require" for Azure Database for PostgreSQL; "disable" for a local one
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    seed_on_startup: bool = True
    
    # Default view when the app opens
    default_retailer: str = "best-buy"
    default_market: str = "US"
    default_period: str = "2026-11"

    # Built React app served by FastAPI (one site, one origin). Empty or missing folder = API only.
    frontend_dist: str = str(BACKEND_DIR / "static")

    # The crawler identity named in allowlisting requests to retailers
    crawler_user_agent: str = "C5i-AIReadiness-Crawler/1.0 (+https://www.c5i.ai)"
    crawler_contact: str = "sharath.hy@c5i.ai"

    # ---- Sign-in --------------------------------------------------------
    # "local":     no sign-in screen. Every request is LOCAL_USER_EMAIL, and that email
    #              still goes through the role rules below (admin or user). Local work only.
    # "microsoft": Microsoft SSO through Azure App Service Authentication ("Easy Auth").
    #              The browser reads /.auth/me; the API trusts only the identity
    #              App Service attaches to each request (X-MS-CLIENT-PRINCIPAL).
    # "dev":       "Authorization: Dev <email>" header, used by the automated tests.
    auth_mode: str = "local"

    # Who you are in "local" mode. Change it to any user email to see the app as a user.
    local_user_email: str = "sharath.hy@c5i.ai"
    local_user_name: str = "Sharath H Y"

    # Who gets in
    admin_emails: str = "sharath.hy@c5i.ai,sharath.hy@course5i.com"
    allowed_domains: str = "c5i.ai,course5i.com"

    @property
    def db_url(self):
        """The SQLAlchemy URL to connect with (password never printed)."""
        from sqlalchemy.engine import URL, make_url
        from sqlalchemy.exc import ArgumentError

        if self.db_host.strip():
            return URL.create("postgresql+psycopg", username=self.db_user.strip() or None, password=self.db_password or None,
                              host=self.db_host.strip(), port=self.db_port, database=self.db_name.strip() or None,
                              query={"sslmode": self.db_sslmode.strip()} if self.db_sslmode.strip() else {})
        raw = self.database_url.strip().strip('"').strip("'")
        if "://" not in raw:
            # Connection strings copied from the Azure portal: libpq ("host=… dbname=… user=…")
            # or ADO.NET ("Server=…;Database=…;User Id=…;Password=…;Ssl Mode=Require;")
            import re
            pairs = re.findall(r"([A-Za-z ]+?)\s*=\s*('(?:[^'\\]|\\.)*'|[^;\s]+)", raw)
            kv = {k.strip().lower().replace(" ", ""): v.strip("'") for k, v in pairs}
            pick = lambda *keys: next((kv[k] for k in keys if kv.get(k)), None)  # noqa: E731
            host = pick("host", "server")
            if host:
                ssl = pick("sslmode")
                return URL.create("postgresql+psycopg", username=pick("user", "userid", "username"), password=pick("password"),
                                  host=host, port=int(pick("port") or 5432), database=pick("dbname", "database"),
                                  query={"sslmode": ssl.lower()} if ssl else {})
        try:
            return make_url(raw)
        except ArgumentError:
            shown = raw.split("@")[-1] if "@" in raw else raw[:30]
            raise SystemExit(
                "DATABASE_URL in backend/.env is not a valid URL (…@" + shown + ").\n"
                "Expected: postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require\n"
                "Check for leftover <placeholders>, spaces or quotes, and URL-encode special characters in the password "
                "(@ → %40, # → %23, / → %2F, : → %3A, ? → %3F), or use DB_HOST, DB_NAME, DB_USER and DB_PASSWORD instead."
            ) from None

    @property
    def admin_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def domain_set(self) -> set[str]:
        return {d.strip().lower().lstrip("@") for d in self.allowed_domains.split(",") if d.strip()}


settings = Settings()
