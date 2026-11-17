from .conftest import ADMIN, USER

BB = {"retailer": "best-buy", "market": "US", "period": "2026-10"}


def test_workflow_scores_match_the_assessment_workflow(client):
    d = client.get("/api/assessment", params=BB, headers=USER).json()
    assert [s["label"] for s in d["scores"]] == ["L3.00", "L3.00", "L3.00", "L3.71 H"]
    counts = {s["status"]: s["all"] for s in d["status"]}
    # The 158-question final set: one result per question
    assert counts == {"Yes": 48, "Partial": 26, "Issue": 4, "No": 3, "Not disclosed": 11, "Access blocked": 0, "Check pending": 65, "N/A": 1}
    assert d["totals"]["maturity"] == 95
    assert d["info"]["data_source"] == "workflow"
    assert len(d["trend"]) == 10


def test_other_views_are_marked_illustrative(client):
    d = client.get("/api/assessment", params={"retailer": "currys", "market": "GB", "period": "2026-09"}, headers=USER).json()
    assert d["info"]["data_source"] == "illustrative"


def test_simulator_changes_with_threshold(client):
    hi = client.post("/api/score/simulate", json={"threshold": 90}, headers=USER).json()
    assert hi["scores"][0]["label"] != "L3.00"


def test_every_page_endpoint_responds(client):
    paths = ["meta", "outcomes", "competitors", "evidence-register", "score-register",
             "actions/capabilities", "actions/stops", "reading-guide",
             "guide/question-set", "guide/accounts", "guide/dm8", "guide/dimensions", "guide/levels",
             "guide/framework", "guide/evidence-quality", "guide/freshness"]
    for p in paths:
        assert client.get(f"/api/{p}", headers=USER).status_code == 200, p
    assert client.get("/api/vendor-comparison", params=BB, headers=USER).status_code == 200


def test_admin_views_are_admin_only(client):
    for p in ["journey", "actions/evidence-gaps", "actions/next-actions", "actions/unpublished", "actions/caveats"]:
        assert client.get(f"/api/{p}", headers=USER).status_code == 403, p
        assert client.get(f"/api/{p}", headers=ADMIN).status_code == 200, p


def test_score_register_has_workflow_versions(client):
    d = client.get("/api/score-register", params=BB, headers=USER).json()
    assert [v["label"] for v in d["versions"]][:2] == ["229-set baseline", "Computed · 158-question set"]
    assert len(d["monthly"]) == 10


def test_vendor_comparison_shape(client):
    d = client.get("/api/vendor-comparison", params=BB, headers=USER).json()
    assert {"Microsoft", "Apple", "HP", "Lenovo"} <= set(d["vendors"])
    assert len(d["trend"]) == 10 and d["themes"]


def test_unknown_market_is_a_clear_404(client):
    r = client.get("/api/assessment", params={"retailer": "currys", "market": "JP"}, headers=USER)
    assert r.status_code == 404
    assert "not assessed" in r.json()["detail"]


def test_skus_listed_and_managed_by_admins(client):
    from tests.conftest import ADMIN, USER
    meta = client.get("/api/meta", headers=ADMIN).json()
    assert [s["name"] for s in meta["skus"]] == ["Surface devices", "Copilot+ PCs", "Windows 11 PCs", "Microsoft 365"]

    # users cannot change SKUs
    assert client.post("/api/admin/skus", json={"name": "Xbox"}, headers=USER).status_code == 403

    r = client.post("/api/admin/skus", json={"name": "Xbox consoles"}, headers=ADMIN)
    assert r.status_code == 201 and r.json()["code"] == "xbox-consoles"
    assert client.post("/api/admin/skus", json={"name": "xbox consoles"}, headers=ADMIN).status_code == 409

    assert client.put("/api/admin/skus/xbox-consoles", json={"name": "Xbox"}, headers=ADMIN).status_code == 200
    names = [s["name"] for s in client.get("/api/admin/settings", headers=ADMIN).json()["skus"]]
    assert names[-1] == "Xbox"

    assert client.delete("/api/admin/skus/xbox-consoles", headers=ADMIN).status_code == 204
    assert client.delete("/api/admin/skus/nope", headers=ADMIN).status_code == 404
    assert len(client.get("/api/meta", headers=ADMIN).json()["skus"]) == 4


def test_last_sku_cannot_be_removed(client):
    from tests.conftest import ADMIN
    codes = [s["code"] for s in client.get("/api/meta", headers=ADMIN).json()["skus"]]
    for c in codes[:-1]:
        assert client.delete(f"/api/admin/skus/{c}", headers=ADMIN).status_code == 204
    assert client.delete(f"/api/admin/skus/{codes[-1]}", headers=ADMIN).status_code == 400
    # put the defaults back for other tests
    for name in ["Surface devices", "Copilot+ PCs", "Windows 11 PCs"]:
        client.post("/api/admin/skus", json={"name": name}, headers=ADMIN)


def test_source_register_lists_best_buy_us_log(client):
    from tests.conftest import ADMIN, USER
    r = client.get("/api/source-register?retailer=best-buy&market=US&period=2026-10", headers=USER).json()
    assert r["summary"]["total"] == 38
    s01 = r["sources"][0]
    assert s01["code"] == "S01" and s01["citing_rows"] == 37 and s01["group"] == "Primary"
    assert "D1-01" not in s01["cited_by"] and len(s01["cited_by"]) > 0  # cited by real October questions
    # Another account has no sources yet
    assert client.get("/api/source-register?retailer=currys&market=GB&period=2026-10", headers=USER).json()["summary"]["total"] == 0


def test_sources_admin_crud(client):
    from tests.conftest import ADMIN, USER
    body = dict(retailer="best-buy", market="US", code="S99", title="Test source", publisher="Me", date="Oct 2026",
                source_type="Secondary (press)", url="https://example.com/a", dimensions=["d1", "D3"], used_for="Testing", citing_rows=2)
    assert client.post("/api/admin/sources", json=body, headers=USER).status_code == 403
    r = client.post("/api/admin/sources", json=body, headers=ADMIN)
    assert r.status_code == 201
    sid = r.json()["id"]
    assert client.post("/api/admin/sources", json=body, headers=ADMIN).status_code == 409          # same code twice
    assert client.post("/api/admin/sources", json={**body, "code": "S98", "market": "JP"}, headers=ADMIN).status_code == 400  # not an account
    assert client.post("/api/admin/sources", json={**body, "code": "S97", "url": "not a link"}, headers=ADMIN).status_code == 422
    assert client.put(f"/api/admin/sources/{sid}", json={**body, "title": "Renamed"}, headers=ADMIN).status_code == 200
    rows = client.get("/api/source-register?retailer=best-buy&market=US&period=2026-10", headers=USER).json()["sources"]
    mine = next(s for s in rows if s["code"] == "S99")
    assert mine["title"] == "Renamed" and mine["dimensions"] == ["D1", "D3"]
    assert client.delete(f"/api/admin/sources/{sid}", headers=ADMIN).status_code == 204
    assert client.delete(f"/api/admin/sources/{sid}", headers=ADMIN).status_code == 404


def test_dimension_detail_matches_score_and_scorecard_layout(client):
    from tests.conftest import USER
    r = client.get("/api/dimension-detail?dimension=D1&retailer=best-buy&market=US&period=2026-10", headers=USER)
    assert r.status_code == 200
    d = r.json()
    assert d["facts"]["label"] == "L3.00" and d["facts"]["stage"] == "Walk"
    gates = [l["gate"] for l in d["levels"]]
    assert gates[:3] == ["Met", "Met", "Met"]
    assert gates[3] == "" and d["why"][1]["kind"] == "ceiling"   # no D1 question tests Level 4: left blank, as in the scorecard
    assert d["why"][0]["kind"] == "met"
    assert any(s["code"] == "S25" and s["url"].startswith("https://") and s["cited_by"] for s in d["sources"])
    assert d["facts"]["evidence_status"] in ("P", "V")
    d4 = client.get("/api/dimension-detail?dimension=D4&retailer=best-buy&market=US&period=2026-10", headers=USER).json()
    assert d4["score"]["h"] and any(w["kind"] == "h" for w in d4["why"])
    assert client.get("/api/dimension-detail?dimension=D9&retailer=best-buy&market=US&period=2026-10", headers=USER).status_code == 404
    out = client.get("/api/outcomes?retailer=best-buy&market=US&period=2026-10", headers=USER).json()
    assert out["sources"]["S01"]["url"].startswith("https://")


def test_level_cells_use_scorecard_words(client):
    from tests.conftest import USER
    sc = {s["dimension"]: s for s in client.get("/api/assessment?retailer=best-buy&market=US&period=2026-10", headers=USER).json()["scores"]}
    assert [c["gate"] for c in sc["D3"]["cells"]][:3] == ["Met", "Met", "Met"]       # D3 L1 is inferred: shown as Met
    assert [c["gate"] for c in sc["D4"]["cells"]] == ["Met", "Met", "Met", "Not disclosed", ""]
    assert [c["gate"] for c in sc["D1"]["cells"]][3:] == ["", ""]                    # ceiling: blank


def test_vendor_comparison_has_theme_trend(client):
    from tests.conftest import USER
    v = client.get("/api/vendor-comparison?retailer=best-buy&market=US&period=2026-10", headers=USER).json()
    assert "Apple" in v["vendors"] and "Microsoft" in v["vendors"]
    t = v["theme_trend"][v["themes"][0]]
    assert len(t) == 10 and "Apple" in t[-1]["scores"]


def test_vendor_scores_roll_up_to_dimensions(client):
    from tests.conftest import USER
    v = client.get("/api/vendor-comparison?retailer=best-buy&market=US&period=2026-10", headers=USER).json()
    dims = {d["dimension"]: d for d in v["dimensions"]}
    assert set(dims) == {"D1", "D2", "D3", "D4"}
    d4 = dims["D4"]   # one theme only: its score equals that theme's score
    theme = d4["themes"][0]["theme"]
    row = next(r for r in v["matrix"] if r["theme"] == theme)
    assert d4["scores"]["Apple"] == round(row["scores"]["Apple"], 1)
    assert len(v["dim_trend"]["D1"]) == 10


def test_competitor_detail_has_scorecard_shape(client):
    from tests.conftest import USER
    r = client.get("/api/competitor-detail?vendor=Apple&dimension=D1&retailer=best-buy&market=US&period=2026-10", headers=USER)
    assert r.status_code == 200
    d = r.json()
    for key in ("score", "levels", "why", "facts", "evidence", "sources", "dimension"):
        assert key in d
    assert d["score"]["value"] == round(d["competitor"]["score100"] / 20, 2)
    assert d["levels"][0]["gate"] == "Met" and d["facts"]["evidence_status"] == "P"
    assert client.get("/api/competitor-detail?vendor=Nobody&dimension=D1&retailer=best-buy&market=US&period=2026-10", headers=USER).status_code == 404


def test_comparison_tabs(client):
    for kind, left in [("retailer", "Best Buy"), ("competitor", "Microsoft"), ("partner", "Dell")]:
        d = client.get("/api/comparison", params={**BB, "kind": kind}, headers=USER).json()
        assert d["default_left"] == left and d["default_right"]
        # Partner comparison leaves out Apple and Microsoft, so it has five vendors
        assert len(d["entities"]) == (5 if kind == "partner" else 7) and all(set(e["scores"]) == {"D1", "D2", "D3", "D4"} for e in d["entities"])
        assert all(len(e["trend"]["D1"]) == len(d["months"]) for e in d["entities"])
    # Best Buy has a real run, so it carries its workflow scores
    bb = next(e for e in client.get("/api/comparison", params={**BB, "kind": "retailer"}, headers=USER).json()["entities"] if e["name"] == "Best Buy")
    assert bb["source"] == "workflow" and bb["scores"]["D4"] == 3.71
    # Placeholders are stable between calls
    a = client.get("/api/comparison", params={**BB, "kind": "partner"}, headers=USER).json()["entities"]
    b = client.get("/api/comparison", params={**BB, "kind": "partner"}, headers=USER).json()["entities"]
    assert a == b
    det = client.get("/api/entity-detail", params={**BB, "kind": "retailer", "name": "Costco", "dimension": "D2"}, headers=USER).json()
    assert det["score"]["label"].startswith("L") and det["competitor"]["name"] == "Costco"


def test_final_question_set(client):
    """The app holds exactly the final question set (158 questions); retired questions never count."""
    d = client.get("/api/guide/question-set", headers=USER).json()
    qs = d["questions"]
    assert len(qs) == 158 and all(q["in_run"] for q in qs)
    lens = {}
    for q in qs:
        lens[q["lens"]] = lens.get(q["lens"], 0) + 1
    assert lens == {"Maturity": 95, "Competitive · vendor": 40, "Competitive · retailer": 23}
    assert not {"D4-03", "D4-19"} & {q["qid"] for q in qs}
    # Listed in the Guide under their previous IDs (D4-03 was v1 D4-03, D4-19 was v1 D5-23)
    assert {"D4-03", "D5-23"} <= {r["old_id"] for r in d["removed"] if r["reason"] == "Not in the final question set"}
    # Every month of an illustrative account is scored on the same 158 questions
    rows = client.get("/api/outcomes", params={"retailer": "currys", "market": "GB", "period": "2026-09"}, headers=USER).json()["questions"]
    assert len([r for r in rows if r["in_run"]]) == 158


def test_access_blocked_is_left_out_of_the_score():
    """Not assessed – access blocked counts like Check pending: never in the denominator."""
    from app.services.scoring import Q, score_dimension
    base = [Q("D1-01", "D1", 1, "Maturity", "Yes"), Q("D1-02", "D1", 1, "Maturity", "No"), Q("D1-03", "D1", 1, "Maturity", "Yes")]
    blocked = base + [Q("D1-04", "D1", 1, "Maturity", "Access blocked"), Q("D1-05", "D1", 1, "Maturity", "Access blocked")]
    # Even if someone gives the status a weight in Settings, it is never counted
    weights = {"Yes": 1.0, "Partial": 0.5, "Issue": 0.5, "No": 0.0, "Not disclosed": 0.0, "Access blocked": 0.0}
    a, b = score_dimension("D1", base), score_dimension("D1", blocked, weights=weights)
    assert a["cells"][0]["pct"] == b["cells"][0]["pct"] == 66.7 and b["cells"][0]["n"] == 3


def test_site_checks(client):
    d = client.get("/api/site-checks", params=BB, headers=USER).json()
    assert [c["name"] for c in d["checks"]] == ["Sitemap", "Response codes", "Canonicals", "Schema", "Metadata", "Robots.txt"]
    assert d["site"] == "www.bestbuy.com" and d["access"] is None
    meta = next(c for c in d["checks"] if c["id"] == "metadata")
    # D1-52 is not in the final 158-question set, so Metadata has no question to rate
    assert meta["rating"] == "na" and meta["questions"] == []
    robots = next(c for c in d["checks"] if c["id"] == "robots")
    assert "User-agent: GPTBot" in robots["robots"] and "Sitemap: https://www.bestbuy.com/sitemap.xml" in robots["robots"]
    # A demo account whose site blocks the crawler
    cu = client.get("/api/site-checks", params={"retailer": "currys", "market": "GB", "period": "2026-10"}, headers=USER).json()
    assert cu["counts"].get("blocked", 0) >= 1 and cu["access"]["status"] == "To request"


def test_allowlisting_moves_blocked_checks_back_to_the_queue(client):
    params = {"retailer": "currys", "market": "GB", "period": "2026-10"}
    assert client.get("/api/admin/access-requests", headers=USER).status_code == 403
    reqs = client.get("/api/admin/access-requests", params={"scope": "current", **params}, headers=ADMIN).json()["requests"]
    req = next(r for r in reqs if r["status"] in ("To request", "Requested"))
    assert req["site"] == "www.currys.co.uk" and "allowlist" in req["email"]["subject"]
    assert client.post(f"/api/admin/access-requests/{req['id']}", json={"status": "Requested"}, headers=ADMIN).json()["status"] == "Requested"
    done = client.post(f"/api/admin/access-requests/{req['id']}", json={"status": "Allowlisted", "note": "Confirmed"}, headers=ADMIN).json()
    assert done["status"] == "Allowlisted" and [h["status"] for h in done["history"]][-2:] == ["Requested", "Allowlisted"]
    rows = {r["qid"]: r for r in client.get("/api/outcomes", params=params, headers=USER).json()["questions"]}
    assert all(rows[q]["status"] == "Check pending" for q in req["qids"])
    # A closed request cannot be changed again
    assert client.post(f"/api/admin/access-requests/{req['id']}", json={"status": "Requested"}, headers=ADMIN).status_code == 409


def test_agency_fix_plan(client):
    acts = client.get("/api/actions/next-actions", params=BB, headers=ADMIN).json()["actions"]
    fix = next(a for a in acts if a["kind"] == "Fix for the agency")
    assert fix["editable"] and fix["rating"] == "needs"
    body = {"retailer": "best-buy", "market": "US", "qid": fix["qids"][0], "owner": "Agency SEO team", "effort": "M", "status": "In progress"}
    assert client.put("/api/admin/action-plan", json=body, headers=USER).status_code == 403
    assert client.put("/api/admin/action-plan", json=body, headers=ADMIN).status_code == 200
    again = next(a for a in client.get("/api/actions/next-actions", params=BB, headers=ADMIN).json()["actions"] if a["qids"] == fix["qids"] and a["kind"] == "Fix for the agency")
    assert (again["owner"], again["effort"], again["status"]) == ("Agency SEO team", "M", "In progress")