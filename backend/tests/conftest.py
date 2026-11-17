"""Tests run against a separate database (TEST_DATABASE_URL), rebuilt from the seed once per session."""
import os

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://retail:retail@localhost:5432/retail_ai_test")
# DB_HOST (set in .env) takes priority over DATABASE_URL, so clear it or the tests would reseed the real database
os.environ["DB_HOST"] = ""
os.environ["AUTH_MODE"] = "dev"
os.environ["SEED_ON_STARTUP"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.seed.seed import ensure_seeded  # noqa: E402

ADMIN = {"Authorization": "Dev sharath.hy@c5i.ai"}
ADMIN2 = {"Authorization": "Dev sharath.hy@course5i.com"}
USER = {"Authorization": "Dev test.user@course5i.com"}
OUTSIDER = {"Authorization": "Dev someone@example.com"}


@pytest.fixture(scope="session")
def client():
    ensure_seeded(force=True)
    with TestClient(app) as c:
        yield c
