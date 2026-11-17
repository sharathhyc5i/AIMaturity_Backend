"""Roles, user management and Microsoft SSO through App Service Authentication."""
import base64
import json

import pytest

from app.config import settings

from .conftest import ADMIN, ADMIN2, OUTSIDER, USER


def test_roles(client):
    assert client.get("/api/auth/me", headers=ADMIN).json()["role"] == "admin"
    assert client.get("/api/auth/me", headers=ADMIN2).json()["role"] == "admin"
    assert client.get("/api/auth/me", headers=USER).json()["role"] == "user"
    assert client.get("/api/auth/me", headers=OUTSIDER).status_code == 403
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/meta").status_code == 401
    assert client.get("/api/auth/config").status_code == 200  # public


def test_users_cannot_reach_admin(client):
    for method, path in [("get", "/api/admin/users"), ("get", "/api/admin/settings"), ("get", "/api/admin/evidence-approvals"),
                         ("post", "/api/admin/markets")]:
        assert getattr(client, method)(path, headers=USER).status_code == 403, path


def test_admin_adds_and_removes_users(client):
    r = client.post("/api/admin/users", json={"email": "Partner@Example.com", "name": "Partner", "role": "user"}, headers=ADMIN)
    assert r.status_code == 201
    uid = r.json()["id"]
    partner = {"Authorization": "Dev partner@example.com"}
    assert client.get("/api/auth/me", headers=partner).status_code == 200  # invited outside the domains
    assert client.delete(f"/api/admin/users/{uid}", headers=ADMIN).status_code == 204
    assert client.get("/api/auth/me", headers=partner).status_code == 403

    # A removed domain user stays removed
    domain_user = {"Authorization": "Dev leaver@c5i.ai"}
    client.get("/api/auth/me", headers=domain_user)
    users = client.get("/api/admin/users", headers=ADMIN).json()["users"]
    leaver = next(u for u in users if u["email"] == "leaver@c5i.ai")
    client.delete(f"/api/admin/users/{leaver['id']}", headers=ADMIN)
    assert client.get("/api/auth/me", headers=domain_user).status_code == 403


def test_configured_admins_are_protected(client):
    users = client.get("/api/admin/users", headers=ADMIN).json()["users"]
    other = next(u for u in users if u["email"] == "sharath.hy@course5i.com")
    assert client.delete(f"/api/admin/users/{other['id']}", headers=ADMIN).status_code == 400


def test_evidence_approval_flow(client):
    q = client.get("/api/admin/evidence-approvals", params={"scope": "current", "retailer": "best-buy", "market": "US", "period": "2026-10"}, headers=ADMIN).json()
    pending = [r for r in q["records"] if r["approval"] == "Pending review"]
    assert {r["eid"] for r in pending} == {"EV-001", "EV-002", "EV-003", "EV-005", "EV-008"}
    rid = pending[0]["id"]
    assert client.post(f"/api/admin/evidence/{rid}/decision", json={"decision": "Rejected"}, headers=ADMIN).status_code == 400  # comment needed
    r = client.post(f"/api/admin/evidence/{rid}/decision", json={"decision": "Approved", "comment": "Checked 5 SKUs"}, headers=ADMIN).json()
    assert r["approval"] == "Approved" and r["register_status"] == "Human-verified" and r["history"][-1]["by"] == "sharath.hy@c5i.ai"


def test_settings_crud(client):
    h = ADMIN
    assert client.post("/api/admin/markets", json={"code": "NL", "name": "Netherlands", "currency": "EUR"}, headers=h).status_code == 201
    r = client.post("/api/admin/retailers", json={"name": "Coolblue", "route": 1, "segment": "Strategic", "markets": ["NL"]}, headers=h)
    assert r.status_code == 201
    meta = client.get("/api/meta", headers=h).json()
    cb = next(x for x in meta["retailers"] if x["slug"] == "coolblue")
    assert cb["markets"][0]["code"] == "NL" and len(cb["markets"][0]["periods"]) == 10
    a = client.get("/api/assessment", params={"retailer": "coolblue", "market": "NL", "period": "2026-10"}, headers=h).json()
    assert a["info"]["data_source"] == "not_run"
    # Removing the only country of a retailer is blocked
    assert client.delete("/api/admin/markets/NL", headers=h).status_code == 400
    assert client.delete("/api/admin/retailers/coolblue", headers=h).status_code == 204
    assert client.delete("/api/admin/markets/NL", headers=h).status_code == 204

    # Dimensions: add, update, remove; a dimension with questions is protected
    assert client.post("/api/admin/dimensions", json={"code": "D5", "name": "Trust & safety", "color": "#008272"}, headers=h).status_code == 201
    scores = client.get("/api/assessment", params=BB_PARAMS, headers=h).json()["scores"]
    assert scores[-1]["dimension"] == "D5" and scores[-1]["label"] == "Not established"
    assert client.put("/api/admin/dimensions/D5", json={"code": "D5", "name": "Trust and safety", "color": "#008272"}, headers=h).status_code == 200
    assert client.delete("/api/admin/dimensions/D1", headers=h).status_code == 400
    assert client.delete("/api/admin/dimensions/D5", headers=h).status_code == 204

    # Levels: only the next level can be added; only an unused top level removed
    assert client.post("/api/admin/levels", json={"level": 7, "name": "Beyond", "stage": "Run"}, headers=h).status_code == 400
    assert client.post("/api/admin/levels", json={"level": 6, "name": "Leading", "stage": "Run"}, headers=h).status_code == 201
    assert len(client.get("/api/assessment", params=BB_PARAMS, headers=h).json()["scores"][0]["cells"]) == 6
    assert client.delete("/api/admin/levels/6", headers=h).status_code == 204

    # Scoring threshold changes the scores everyone sees
    cfg = client.get("/api/admin/settings", headers=h).json()["scoring"]
    client.put("/api/admin/scoring", json={**cfg, "threshold": 90}, headers=h)
    assert client.get("/api/assessment", params=BB_PARAMS, headers=USER).json()["scores"][0]["label"] != "L3.00"
    client.put("/api/admin/scoring", json=cfg, headers=h)
    assert client.get("/api/assessment", params=BB_PARAMS, headers=USER).json()["scores"][3]["label"] == "L3.71 H"

    # Freshness and evidence states are saved as lists
    fr = client.get("/api/admin/settings", headers=h).json()["freshness"]
    assert client.put("/api/admin/freshness", json=fr[:3], headers=h).status_code == 200
    assert len(client.get("/api/guide/freshness", headers=h).json()["policy"]) == 3
    client.put("/api/admin/freshness", json=fr, headers=h)
    st = client.get("/api/admin/settings", headers=h).json()["evidence_states"]
    assert client.put("/api/admin/evidence-states", json=st + [st[0]], headers=h).status_code == 400


def test_score_snapshot(client):
    r = client.post("/api/admin/score-register/snapshot", json={**BB_PARAMS, "label": "After review"}, headers=ADMIN)
    assert r.status_code == 201
    versions = client.get("/api/score-register", params=BB_PARAMS, headers=USER).json()["versions"]
    assert versions[-1]["label"] == "After review" and versions[-1]["created_by"] == "sharath.hy@c5i.ai"


BB_PARAMS = {"retailer": "best-buy", "market": "US", "period": "2026-10"}


# ---------------------------------------------------------------- Microsoft SSO (App Service Authentication)
def principal(email="sharath.hy@course5i.com", name="Sharath H Y", idp="aad"):
    body = {"auth_typ": idp, "name_typ": "name", "role_typ": "roles",
            "claims": [{"typ": "preferred_username", "val": email}, {"typ": "name", "val": name}]}
    return {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(body).encode()).decode(),
            "X-MS-CLIENT-PRINCIPAL-NAME": email, "X-MS-CLIENT-PRINCIPAL-IDP": idp}


@pytest.fixture
def microsoft(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "microsoft")
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")


def test_microsoft_sso_roles_and_name(client, microsoft):
    r = client.get("/api/auth/me", headers=principal())
    assert r.status_code == 200 and r.json() == {"email": "sharath.hy@course5i.com", "name": "Sharath H Y", "role": "admin"}
    r = client.get("/api/auth/me", headers=principal("Asha.R@C5i.ai", "Asha R"))
    assert r.json()["role"] == "user" and r.json()["email"] == "asha.r@c5i.ai" and r.json()["name"] == "Asha R"
    assert client.get("/api/auth/me", headers=principal("x@gmail.com", "X")).status_code == 403
    assert client.get("/api/auth/me").status_code == 401
    # Dev headers are ignored in Microsoft mode
    assert client.get("/api/auth/me", headers=ADMIN).status_code == 401
    assert client.get("/api/auth/config").json()["mode"] == "microsoft"


def test_microsoft_sso_rejects_bad_input(client, microsoft):
    assert client.get("/api/auth/me", headers={"X-MS-CLIENT-PRINCIPAL": "not-base64!!"}).status_code == 401
    assert client.get("/api/auth/me", headers=principal(idp="google")).status_code == 403


def test_microsoft_mode_refuses_without_app_service_auth(client, monkeypatch):
    """Without App Service Authentication the headers could be forged, so nothing is trusted."""
    monkeypatch.setattr(settings, "auth_mode", "microsoft")
    monkeypatch.delenv("WEBSITE_AUTH_ENABLED", raising=False)
    assert client.get("/api/auth/me", headers=principal()).status_code == 500
    assert client.get("/api/auth/config").json()["ready"] is False


def test_local_mode_signs_in_configured_user_with_role_rules(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "local_user_email", "sharath.hy@c5i.ai")
    r = client.get("/api/auth/me")
    assert r.status_code == 200 and r.json()["role"] == "admin"
    monkeypatch.setattr(settings, "local_user_email", "local.viewer@c5i.ai")
    r = client.get("/api/auth/me")
    assert r.status_code == 200 and r.json()["role"] == "user"
    assert client.get("/api/admin/users").status_code == 403
    monkeypatch.setattr(settings, "local_user_email", "someone@example.com")
    assert client.get("/api/auth/me").status_code == 403
