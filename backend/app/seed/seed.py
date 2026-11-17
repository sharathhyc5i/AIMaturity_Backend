"""Create tables and load seed data.

Best Buy · United States · Oct 2026 is the real workflow run (9 Oct 2026).
Every other retailer / market / month is generated *illustrative* data so the
filters have something to show. It is flagged `data_source="illustrative"` and
the UI labels it clearly. Replace it by loading real runs into the same tables.

SEED_VERSION: when it changes, the database is rebuilt from this seed on start-up.
"""
import json
import random
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import delete, inspect, select, text
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from ..db import Base, SessionLocal, engine

SEED_VERSION = 7
DATA = json.loads((Path(__file__).parent / "seed_data.json").read_text(encoding="utf-8"))
REAL = ("best-buy", "US", "2026-10")
VENDORS = ["Microsoft", "Apple", "Dell", "HP", "Lenovo", "Samsung"]
NOT_RUN_FINDING = "Not run yet for this account. Queued for the next agent run."


def _period(s: str) -> date:
    y, mo = s.split("-")
    return date(int(y), int(mo), 1)


# ---------------------------------------------------------------- results
def _illustrative_results(retailer: dict, market: str, month: str, questions: list[dict], real: dict) -> list[dict]:
    """Deterministic, plausible statuses that improve month on month.

    Each question gets fixed random draws per retailer × market, and the bar for a
    good answer relaxes as the months go by, so a status can only stay or improve.
    """
    month_idx = DATA["months"].index(month)
    if (retailer["slug"], market) == REAL[:2]:
        return _history_before_real(month_idx, questions, real)
    p = retailer["profile"] - (0.06 if market != retailer["hq"] else 0) - 0.2 + 0.026 * month_idx
    resolve = 0.04 + 0.045 * month_idx
    out, n = [], 0
    for q in questions:
        rq = random.Random(f"{retailer['slug']}|{market}|{q['qid']}")
        u, v, w = rq.random(), rq.random(), rq.random()
        base = real.get(q["qid"], {"status": "Check pending"})["status"]
        if base == "Check pending" or not q["in_run"]:
            if v < resolve:
                status = "Yes" if u < p * 0.8 else "Partial" if u < p * 0.8 + 0.3 else ("No" if w < 0.3 else "Not disclosed")
            else:
                status = "Check pending"
        elif base == "Yes":
            status = "Yes" if u < p + 0.12 else "Partial" if u < p + 0.3 else ("No" if w < 0.3 else "Not disclosed")
        elif base == "Partial":
            status = "Yes" if u < p * 0.5 else "Partial" if u < 0.85 else ("Issue" if w < 0.4 else "Not disclosed")
        elif base in ("Issue", "No"):
            status = "Partial" if u < p * 0.4 else ("Issue" if w < 0.6 else "No")
        else:
            status = "Partial" if u < p * 0.3 else base
        srcs = []
        if status in ("Yes", "Partial", "Issue", "No"):
            n += 1
            srcs = [f"S{(n % 38) + 1:02d}"]
        finding = {
            "Yes": "Met on the sampled pages and sources for this market.",
            "Partial": "Partly met, or met on a limited sample; a wider sample would firm it up.",
            "Issue": "Checked and found a defect on the sampled pages.",
            "No": "Checked: the public information does not meet the test.",
            "Not disclosed": "Not published by the retailer in any public source reviewed.",
            "Check pending": "Public-source check queued for the next agent run.",
            "N/A": "Does not apply to this market.",
        }[status]
        out.append(dict(qid=q["qid"], status=status, finding=finding, sources=srcs, flags=[]))
    return out


def _history_before_real(month_idx: int, questions: list[dict], real: dict) -> list[dict]:
    """Earlier months for the real account: fewer checks run and weaker answers, building up to the October run."""
    back = (len(DATA["months"]) - 1 - month_idx) * 0.06
    out = []
    for q in questions:
        r = real.get(q["qid"])
        if not r:
            continue
        u = random.Random(f"history|{q['qid']}").random()
        status, finding, srcs = r["status"], r["finding"], r["sources"]
        if status not in ("Check pending", "Access blocked", "N/A") and u < back:
            status, finding, srcs = "Check pending", "Not yet checked in this month's run.", []
        elif status == "Yes" and u < back * 1.8:
            status, finding = "Partial", "Partly met at the time; confirmed in a later run."
        out.append(dict(qid=q["qid"], status=status, finding=finding, sources=srcs, flags=[]))
    return out


def _illustrative_flags(results: list[dict], qmap: dict) -> list[dict]:
    flags = []
    pend = [r["qid"] for r in results if r["status"] == "Check pending"]
    if pend:
        flags.append(dict(area="Questions", kind="Check pending", severity="warn",
                          text=f"{len(pend)} public-source checks have not been run yet.", qids=pend[:8]))
    nd = [r["qid"] for r in results if r["status"] == "Not disclosed" and qmap[r["qid"]]["lens"] == "Maturity"]
    if nd:
        flags.append(dict(area="Evidence", kind="Not disclosed", severity="info",
                          text=f"{len(nd)} maturity questions have no public answer either way.", qids=nd[:8]))
    dyn = [r["qid"] for r in results if r["qid"] in ("D1-11", "D1-14", "D2-08", "D2-18") and r["status"] != "Check pending"]
    if dyn:
        flags.append(dict(area="Freshness", kind="30-day refresh", severity="warn",
                          text="Dynamic price and availability observations must be refreshed within 30 days of client publication.", qids=dyn))
    flags.append(dict(area="Dimensions", kind="Coverage", severity="info",
                      text="No maturity question tests Levels 4–5 in D1–D3 or Level 5 in D4, so D1–D3 can reach Level 3 at most and D4 Level 4.", qids=[]))
    return flags


# ---------------------------------------------------------------- evidence
EVIDENCE_TEMPLATES = [
    # id, cell, title, claim, category, use case, product?, questions, source type
    ("EV-101", "D1-L1", "Sampled product page content", "A sampled {cat} product page returns title, SKU, specs, price and availability in the initial HTML.", "Copilot+ PCs", "Product discovery", True, ["D1-01", "D1-02"], "Website"),
    ("EV-102", "D1-L1", "Search-index snapshot vs live page", "The search-index snapshot of a sampled product page is compared with the live page on SKU, price and review count.", "Copilot+ PCs", "Product discovery", True, ["D1-11", "D1-14"], "Website"),
    ("EV-103", "D2-L1", "Product image alt text", "Image alt text on sampled {cat} pages is checked against the product shown.", "Copilot+ PCs", "AI-generated content", True, ["D1-15"], "Website"),
    ("EV-104", "D1-L2", "Laptop category facets and compare", "The laptop category offers processor, memory and Copilot+ PC facets and a cross-brand compare control.", "Copilot+ PCs", "Product selection", True, ["D1-26", "D1-27"], "Website"),
    ("EV-105", "D2-L2", "Product and Offer structured data", "Product and Offer markup on sampled pages carries price, availability and seller.", "Surface devices", "Machine-readable content", True, ["D1-13"], "Website"),
    ("EV-106", "D2-L3", "Delivery and pickup on product page", "Product pages show delivery dates by postcode and store pickup next to price.", "Windows 11 PCs", "Availability and fulfillment", True, ["D2-08", "D2-18"], "Website"),
    ("EV-107", "D1-L3", "Own-site AI shopping assistant", "The retailer states an AI shopping assistant is live on its site or app for this market.", "All categories", "Conversational shopping", False, ["D1-32", "D1-33"], "Public statement"),
    ("EV-108", "D4-L2", "Named AI ownership", "A named executive or team owns AI and digital delivery.", "—", "AI ownership", False, ["D4-01", "D4-05"], "Public statement"),
]


def _status_to_result(statuses: list[str]) -> str:
    if any(s == "Issue" for s in statuses):
        return "Issue"
    if any(s in ("Yes", "Partial") for s in statuses):
        return "Pass"
    if any(s == "No" for s in statuses):
        return "Fail"
    return "Unknown"


def _illustrative_evidence(name: str, run_date: date, results: dict) -> list[dict]:
    out = []
    for eid, cell, title, claim, cat, use, product, qids, src in EVIDENCE_TEMPLATES:
        statuses = [results[q]["status"] for q in qids if q in results]
        res = _status_to_result(statuses)
        if res == "Unknown" and all(s == "Check pending" for s in statuses):
            continue  # nothing fetched yet
        reg = {"Pass": "Accepted", "Issue": "Adjudication", "Fail": "Accepted", "Unknown": "Unknown"}[res]
        conf = "High" if src == "Website" and res != "Unknown" else ("Medium" if res != "Unknown" else "Low")
        dynamic = cell in ("D1-L1", "D2-L3")
        out.append(dict(
            eid=eid, title=title, claim=claim.format(cat=cat), publisher=name, url=f"{name} website" if src == "Website" else f"{name} public statement",
            pub_date="n/a (live page)" if src == "Website" else "2026", observed=run_date.isoformat(), channel="Website" if src == "Website" else "Corporate",
            category=cat, use_case=use, stage="Live" if res != "Unknown" else "Plan", independence="Original observation", cell=cell,
            rationale="Illustrative record generated for the demo.", limits=["Illustrative data — not an observation of this retailer."],
            source_type=src, confidence=conf, result=res, h_flag=False, register_status=reg,
            register_note=f"{res}" + (" · sample too small" if res == "Unknown" else ""),
            freshness="Refresh ≤30 days before publication" if dynamic else "Current", change="",
            val_owner="Assessor", val_proof="Confirm on a wider sample" if product else "Confirm with a primary source",
            val_due=run_date.replace(day=min(run_date.day + 14, 28)).isoformat(), val_reviewer="QA reviewer",
            val_disposition="Accepted" if reg == "Accepted" else "Open",
            next_level="—", questions=qids, sources=[], v1=dict(status="Fetched", note=res, confidence=conf, freshness="Not checked"),
            product=product,
        ))
    return out


def _needs_review(e: dict) -> bool:
    """Manual review is required for product evidence that validation did not accept outright."""
    product = e.get("product", e.get("category") in ("Copilot+ PCs", "Surface devices", "Windows 11 PCs", "Microsoft 365"))
    return bool(product) and (e["register_status"] != "Accepted" or e.get("val_disposition") == "Open")


# ---------------------------------------------------------------- vendor comparison
def _vendor_scores(retailer: dict, market: str, month: str, themes: list[str]) -> list[dict]:
    month_idx = DATA["months"].index(month)
    out = []
    for v in VENDORS:
        rb = random.Random(f"{retailer['slug']}|{market}|{v}")
        bias = rb.uniform(-12, 12) + (5 if v in ("Apple", "Microsoft") else 0)
        growth = rb.uniform(0.4, 2.2) + (0.6 if v == "Microsoft" else 0)
        for t in themes:
            rt = random.Random(f"{retailer['slug']}|{market}|{v}|{t}")
            theme_bias = rt.uniform(-12, 12)
            noise = random.Random(f"{retailer['slug']}|{market}|{v}|{t}|{month}").uniform(-3, 3)
            score = 46 + bias + theme_bias + growth * month_idx + noise
            out.append(dict(vendor=v, theme=t, score=round(max(5, min(97, score)), 1)))
    return out


# ---------------------------------------------------------------- main seed
def seed(db: Session) -> None:
    for i, d in enumerate(DATA["dimensions"]):
        db.add(m.Dimension(code=d["code"], name=d["name"], short=d["short"], color=d["color"], summary=d["summary"], position=i))
    for lv in DATA["levels"]:
        db.add(m.Level(**lv))
    db.flush()
    for c in DATA["cells"]:
        db.add(m.LevelCell(**c))
    for i, q in enumerate(DATA["questions"]):
        db.add(m.Question(position=i, **q))
    db.flush()  # questions must exist before QuestionResult rows reference them

    markets = {}
    for mk in DATA["markets"]:
        markets[mk["code"]] = m.Market(code=mk["code"], name=mk["name"], currency=mk["currency"],
                                       market_maker_entries=mk["market_maker"], strategic_entries=mk["strategic"])
        db.add(markets[mk["code"]])
    g = DATA["guide"]
    for key, val in g.items():
        db.add(m.GuideContent(key=key, content=val))
    db.add(m.GuideContent(key="baseline_229", content=DATA["baseline_229"]))
    db.flush()

    from ..services.context import scores_for  # after guide content exists

    qmap = {q["qid"]: q for q in DATA["questions"]}
    real = {r["qid"]: r for r in DATA["bestbuy_us_2026_10"]}
    themes = sorted({q["sub_theme"] for q in DATA["questions"] if q["lens"] == "Competitive · vendor"})
    distributor_peers = {"US": ["TD SYNNEX US", "D&H Distributing", "Arrow ECS"], "CA": ["TD SYNNEX Canada", "D&H Canada", "Arrow ECS Canada"]}
    questions_db = list(db.scalars(select(m.Question).order_by(m.Question.position)))
    seeded_at = datetime(2026, 10, 9, 19, 23, tzinfo=timezone.utc)

    for rt in DATA["retailers"]:
        r = m.Retailer(slug=rt["slug"], name=rt["name"], hq=rt["hq"], route=rt["route"], segment=rt["segment"])
        db.add(r)
        db.flush()
        for code in rt["markets"]:
            peers = distributor_peers[code] if rt["route"] == 2 else DATA["peers"].get(code, [])
            rm = m.RetailerMarket(retailer_id=r.id, market_id=markets[code].id, peers=peers)
            db.add(rm)
            db.flush()
            for month in DATA["months"]:
                is_real = (rt["slug"], code, month) == REAL
                run = date(2026, 10, 9) if is_real else _period(month).replace(day=9)
                a = m.Assessment(retailer_market_id=rm.id, period=_period(month), run_date=run,
                                 data_source="workflow" if is_real else "illustrative",
                                 old_sources=["S10", "S11", "S37"] if is_real else [], notes=_notes(is_real))
                db.add(a)
                db.flush()
                if is_real:
                    # Results keyed to retired question IDs are skipped; the dry-run data step loads the current set.
                    results = [r for r in DATA["bestbuy_us_2026_10"] if r["qid"] in qmap]
                    flags, evidence = DATA["validation_flags"], DATA["evidence_bestbuy"]
                else:
                    results = _illustrative_results(rt, code, month, DATA["questions"], real)
                    flags = _illustrative_flags(results, qmap)
                    evidence = _illustrative_evidence(rt["name"], run, {x["qid"]: x for x in results})
                for res in results:
                    db.add(m.QuestionResult(assessment_id=a.id, **res))
                for f in flags:
                    db.add(m.ValidationFlag(assessment_id=a.id, **f))
                latest = month == DATA["months"][-1]
                for e in evidence:
                    review = _needs_review(e) and latest  # earlier months were reviewed at the time
                    db.add(m.EvidenceRecord(
                        assessment_id=a.id, eid=e["eid"], cell=e["cell"], stage=e["stage"], confidence=e["confidence"],
                        register_status=e["register_status"], data=e, requires_review=review,
                        approval="Pending review" if review else "Approved",
                        reviewed_by=None if review else "Validation node", reviewed_at=None if review else datetime.combine(run, datetime.min.time(), tzinfo=timezone.utc),
                        history=[] if review else [dict(at=run.isoformat(), by="Validation node", decision="Approved", comment="Accepted at validation.")]))
                for vs in _vendor_scores(rt, code, month, themes):
                    db.add(m.VendorScore(assessment_id=a.id, **vs))
                db.flush()
                db.refresh(a)
                sc = scores_for(db, a, questions_db)
                snap = [dict(dimension=s["dimension"], label=s["label"], value=s["value"], level=s["level"],
                             cells=[c["pct"] for c in s["cells"]]) for s in sc]
                cfg = g["scoring"]
                if is_real:
                    base = DATA["baseline_229"]
                    db.add(m.ScoreSnapshot(assessment_id=a.id, version=1, label="229-set baseline", created_by="Input 06 · Levels and scoring framework",
                                           created_at=datetime(2026, 10, 9, 18, 37, tzinfo=timezone.utc), weights={}, threshold=cfg["threshold"],
                                           scores=[dict(dimension=k, label=v, value=float(v[1]), level=int(v[1]), cells=[]) for k, v in base.items()],
                                           notes=["Best Buy US scores from the 229-question, five-dimension run, mapped to four dimensions.",
                                                  "D4 merges old D4 (L2) and D5 (L1 H); a merged level needs both parts, so the lower score holds."]))
                    db.add(m.ScoreSnapshot(assessment_id=a.id, version=2, label="Computed · 158-question set", created_by="Scoring node",
                                           created_at=seeded_at, weights=cfg["weights"], threshold=cfg["threshold"], scores=snap,
                                           notes=["Scored from the 95 maturity questions and Evidence register v2. Competitive questions are not scored.",
                                                  "Changed vs v1: D4 L1 H → L3.71 H."]))
                else:
                    db.add(m.ScoreSnapshot(assessment_id=a.id, version=1, label="Computed at run", created_by="Scoring node",
                                           created_at=datetime.combine(run, datetime.min.time(), tzinfo=timezone.utc).replace(hour=19),
                                           weights=cfg["weights"], threshold=cfg["threshold"], scores=snap,
                                           notes=["Illustrative data generated for the demo."]))

    for email in sorted(settings.admin_set):
        db.add(m.User(email=email, name="Sharath H Y", role="admin", status="active", source="bootstrap", created_by="Configuration"))
    db.add(m.GuideContent(key="seed_version", content={"version": SEED_VERSION}))
    db.commit()


def _notes(is_real: bool) -> list[str]:
    if is_real:
        return [
            "Scored from the 95 maturity questions of the 158-question set and Evidence register v2. Competitive questions are not scored.",
            "Weights: Yes 1 · Partial 0.5 · Issue 0.5 · No 0 · Not disclosed 0. Level threshold 35%. Untested lower levels inferred from a met level above.",
            "This set has not been run end to end yet: Best Buy results are carried from the previous run through each question's previous ID; 62 new questions start as Check pending.",
            "CEO changes 1 Nov 2026; AI ownership after the transition is not yet published (EV-010).",
        ]
    return [
        "Illustrative data generated to demonstrate the dashboard. It is not an assessment of this retailer.",
        "Replace it by loading a real agent run for this retailer, market and month.",
    ]


DEFAULT_SKUS = [("surface-devices", "Surface devices"), ("copilot-pc", "Copilot+ PCs"), ("windows-11", "Windows 11 PCs"), ("m365", "Microsoft 365")]


def ensure_skus(db) -> None:
    """Add the default SKUs when the table is empty. Never touches SKUs an admin has edited."""
    if db.scalar(select(m.Sku.id).limit(1)) is None:
        for i, (code, name) in enumerate(DEFAULT_SKUS):
            db.add(m.Sku(code=code, name=name, position=i))
        db.commit()
        return
    # Rename the old "surface" code to "surface-devices" in databases seeded before the change
    old = db.scalar(select(m.Sku).where(m.Sku.code == "surface"))
    if old and db.scalar(select(m.Sku.id).where(m.Sku.code == "surface-devices")) is None:
        old.code = "surface-devices"
        db.commit()


def ensure_sources(db) -> None:
    """Load the Best Buy US source log when the sources table is empty. Never touches edited rows."""
    if db.scalar(select(m.Source.id).limit(1)) is not None:
        return
    path = Path(__file__).with_name("sources_bestbuy_us.json")
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for s in data["sources"]:
        db.add(m.Source(retailer=data["retailer"], market=data["market"], **s))
    db.commit()


def ensure_question_set(db) -> str:
    """Keep the questions table in line with seed_data.json without reseeding.

    Adds new questions, updates changed ones and removes questions that are no longer in the
    set, together with their results (so they stop counting in any score). Load a new
    question-set workbook with: python -m app.seed.import_questions <file.xlsx>
    """
    wanted = {q["qid"]: (i, q) for i, q in enumerate(DATA["questions"])}
    have = {q.qid: q for q in db.scalars(select(m.Question))}
    added = changed = 0
    for qid, (i, q) in wanted.items():
        row = have.get(qid)
        if row is None:
            db.add(m.Question(position=i, **q))
            added += 1
            continue
        fields = dict(q, position=i)
        if any(getattr(row, k) != v for k, v in fields.items()):
            for k, v in fields.items():
                setattr(row, k, v)
            changed += 1
    db.flush()
    # Every assessment gets a result for every question in the set; new questions start as
    # Check pending (Not yet checked) until the next run
    in_set = [qid for qid, (_, q) in wanted.items() if q.get("in_run", True)]
    have_res = {(a_id, qid) for a_id, qid in db.execute(select(m.QuestionResult.assessment_id, m.QuestionResult.qid))}
    for a_id in db.scalars(select(m.Assessment.id)):
        for qid in in_set:
            if (a_id, qid) not in have_res:
                db.add(m.QuestionResult(assessment_id=a_id, qid=qid, status="Check pending", finding=ADDED_FINDING, sources=[], flags=[]))
    gone = [qid for qid in have if qid not in wanted]
    if gone:
        db.execute(delete(m.QuestionResult).where(m.QuestionResult.qid.in_(gone)))
        db.execute(delete(m.Question).where(m.Question.qid.in_(gone)))
    # Results for questions that are in the set but marked out of the run never count
    out = [qid for qid, (_, q) in wanted.items() if not q.get("in_run", True)]
    if out:
        db.execute(delete(m.QuestionResult).where(m.QuestionResult.qid.in_(out)))
    g = db.get(m.GuideContent, "removed")
    if g is not None and g.content != DATA["guide"].get("removed"):
        g.content = DATA["guide"].get("removed")
    db.commit()
    return f"questions: {len(wanted)} ({added} added, {changed} changed, {len(gone)} removed)"


ADDED_FINDING = "Not yet checked: added to the question set after this run."
SITE_QIDS = ["D1-52", "D1-53", "D1-54"]
# Demo only: a few illustrative sites block our crawler in the latest two months
BLOCKED_QIDS = ["D1-01", "D1-02", "D1-04", "D1-06", "D1-52", "D1-53", "D1-54"]
DEMO_BLOCKED = {
    ("currys", "GB"): "HTTP 403 from bot protection (Akamai Bot Manager) for our crawler's user agent on product pages.",
    ("yodobashi", "JP"): "A browser-check page was served instead of the product page, so no page content could be read.",
}


def _data_step_site_checks(db) -> None:
    """Placeholder results for the new site-check questions, and access-blocked examples for the demo."""
    g = db.get(m.GuideContent, "status_definitions")
    if g is not None:
        g.content = DATA["guide"]["status_definitions"]
    real = {r["qid"]: r for r in DATA["bestbuy_us_2026_10"]}
    by_slug = {r["slug"]: r for r in DATA["retailers"]}
    qmap = {q["qid"]: q for q in DATA["questions"]}
    rms = db.scalars(select(m.RetailerMarket)).all()
    for rm in rms:
        rt = by_slug.get(rm.retailer.slug)
        runs = sorted(rm.assessments, key=lambda a: a.period)
        for a in runs:
            if a.data_source != "illustrative" or rt is None:
                continue
            month = a.period.strftime("%Y-%m")
            if month not in DATA["months"]:
                continue
            res = {r.qid: r for r in a.results}
            fresh = [qmap[q] for q in SITE_QIDS if q in res and res[q].finding == ADDED_FINDING]
            if fresh:
                for g in _illustrative_results(rt, rm.market.code, month, fresh, real):
                    r = res[g["qid"]]
                    r.status, r.finding, r.sources = g["status"], g["finding"], g["sources"]
        # Access blocked examples (illustrative accounts only)
        reason = DEMO_BLOCKED.get((rm.retailer.slug, rm.market.code))
        if not reason:
            continue
        for a in runs[-2:]:
            if a.data_source != "illustrative":
                continue
            for r in a.results:
                if r.qid in BLOCKED_QIDS:
                    r.status, r.sources = "Access blocked", []
                    r.finding = f"Not assessed – access blocked: {reason}"
    db.flush()


DRY_RUN_FILE = Path(__file__).with_name("dry_run_2026_11_06.json")
DRY_RUN_ACCOUNTS = [("best-buy", "US"), ("amazon", "US")]
DRY_RUN_PERIOD = date(2026, 10, 1)


def _dry_run_refs(text_: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Split a workbook Source cell into (url, note) pairs and capture notes."""
    import re

    t = (text_ or "").strip()
    if not t or t in ("—", "-"):
        return [], []
    urls: list[tuple[str, str]] = []

    def keep(mt):
        urls.append((mt.group(1), (mt.group(3) or "").strip()))
        return ""

    rest = re.sub(r'(https?://[^\s|"]+)(\s*\(([^)]*)\))?', keep, t)
    note = re.sub(r'[|"\s]+', " ", rest).strip()
    return urls, [note] if note else []


def _data_step_dry_run(db) -> None:
    """The 6 Nov 2026 dry run: Best Buy US and Amazon US answers, 0-1 scores and sources for October 2026.

    Each result gets its score (N/A has none), a status from it (Yes, Partial, No, N/A) and the answer as
    its finding; every distinct URL becomes a source DR-S###, every capture note DR-E###.
    """
    from urllib.parse import urlparse

    data = json.loads(DRY_RUN_FILE.read_text(encoding="utf-8"))
    qdim = {q["qid"]: q["dimension"] for q in data["questions"]}
    for slug, market in DRY_RUN_ACCOUNTS:
        rm = db.scalar(select(m.RetailerMarket).join(m.Retailer).join(m.Market)
                       .where(m.Retailer.slug == slug, m.Market.code == market))
        a = rm and db.scalar(select(m.Assessment).where(m.Assessment.retailer_market_id == rm.id,
                                                       m.Assessment.period == DRY_RUN_PERIOD))
        if not a:
            continue
        db.execute(delete(m.Source).where(m.Source.retailer == slug, m.Source.market == market, m.Source.code.like("DR-%")))
        made: dict[tuple[str, str], m.Source] = {}

        def code_for(kind: str, key: str, qid: str, url: str = "", note: str = "") -> str:
            src = made.get((kind, key))
            if src is None:
                n = sum(1 for k in made if k[0] == kind) + 1
                src = m.Source(retailer=slug, market=market, code=f"DR-{kind}{n:03d}", title=(key if kind == "S" else note)[:300],
                               publisher=urlparse(url).netloc if url else "C5i capture", date="05-Nov-2026",
                               source_type="Web page" if kind == "S" else "Capture note", url=url,
                               dimensions=[], used_for=note if kind == "S" else "", citing_rows=0)
                db.add(src)
                made[(kind, key)] = src
            if qdim[qid] not in src.dimensions:
                src.dimensions = [*src.dimensions, qdim[qid]]
            src.citing_rows += 1
            return src.code

        res = {r.qid: r for r in a.results}
        for qid, v in data["retailers"][slug].items():
            r = res.get(qid)
            if r is None:
                r = m.QuestionResult(assessment_id=a.id, qid=qid, flags=[])
                db.add(r)
            s = v["score"]
            r.score = s
            r.status = "N/A" if s is None else "Yes" if s >= 1 else "Partial" if s >= 0.5 else "No"
            r.finding = v["answer"] or ""
            urls, notes = _dry_run_refs(v["source"])
            r.sources = [code_for("S", u, qid, url=u, note=nt) for u, nt in urls] + [code_for("E", nt, qid, note=nt) for nt in notes]
        a.data_source = "workflow"
        a.run_date = date(2026, 11, 6)
        a.notes = [
            f"Scored from the dry run of 6 Nov 2026: {len(data['questions'])} questions, each answered and scored with its sources.",
            "Each dimension scores the average of its question scores (Yes 1, Partial 0.5, No 0) × 5; every lens counts and N/A is left out.",
        ]
    db.flush()


NOVEMBER = date(2026, 11, 1)


def _data_step_november(db) -> None:
    """Add November 2026 for every account, holding the same results as October 2026 (the newest data,
    including the 6 Nov dry run), so the app opens on the current month. "drop-october-2026" then removes October.
    Copies each October assessment with its results, evidence records, validation flags and vendor scores."""
    octobers = db.scalars(select(m.Assessment).where(m.Assessment.period == DRY_RUN_PERIOD)).all()
    for a in octobers:
        if db.scalar(select(m.Assessment.id).where(m.Assessment.retailer_market_id == a.retailer_market_id,
                                                  m.Assessment.period == NOVEMBER)):
            continue
        n = m.Assessment(retailer_market_id=a.retailer_market_id, period=NOVEMBER, run_date=a.run_date,
                         data_source=a.data_source, old_sources=list(a.old_sources or []),
                         notes=list(a.notes or []))
        db.add(n)
        db.flush()
        for r in a.results:
            db.add(m.QuestionResult(assessment_id=n.id, qid=r.qid, status=r.status, finding=r.finding,
                                    sources=list(r.sources or []), flags=list(r.flags or []), score=r.score))
        for e in a.evidence:
            db.add(m.EvidenceRecord(assessment_id=n.id, eid=e.eid, cell=e.cell, stage=e.stage, confidence=e.confidence,
                                    register_status=e.register_status, data=dict(e.data or {}), requires_review=e.requires_review,
                                    approval=e.approval, reviewed_by=e.reviewed_by, reviewed_at=e.reviewed_at,
                                    review_comment=e.review_comment, history=list(e.history or [])))
        for f in a.flags:
            db.add(m.ValidationFlag(assessment_id=n.id, area=f.area, kind=f.kind, severity=f.severity, text=f.text, qids=list(f.qids or [])))
        for v in db.scalars(select(m.VendorScore).where(m.VendorScore.assessment_id == a.id)):
            db.add(m.VendorScore(assessment_id=n.id, vendor=v.vendor, theme=v.theme, score=v.score))
    db.flush()


def _data_step_empty_october(db) -> None:
    """October 2026 stays in the month picker and the charts, but holds no data: each account's October
    assessment loses its results, evidence, flags, vendor scores and score versions, and is marked not run.
    An account without an October assessment gets one with every question Check pending. Only November
    2026 carries results."""
    questions = db.scalars(select(m.Question).where(m.Question.in_run.is_(True))).all()
    for rm in db.scalars(select(m.RetailerMarket)).all():
        a = db.scalar(select(m.Assessment).where(m.Assessment.retailer_market_id == rm.id,
                                                 m.Assessment.period == DRY_RUN_PERIOD))
        if a is None:
            a = m.Assessment(retailer_market_id=rm.id, period=DRY_RUN_PERIOD, run_date=DRY_RUN_PERIOD, data_source="not_run",
                             old_sources=[], notes=[])
            db.add(a)
            db.flush()
        else:
            for model in (m.QuestionResult, m.EvidenceRecord, m.ValidationFlag, m.VendorScore, m.ScoreSnapshot):
                db.execute(delete(model).where(model.assessment_id == a.id))
            a.data_source = "not_run"
        a.notes = ["October 2026 has no data. Only November 2026 is scored."]
        db.flush()
        for q in questions:
            db.add(m.QuestionResult(assessment_id=a.id, qid=q.qid, status="Check pending",
                                    finding="No data for October 2026.", sources=[], flags=[]))
    db.flush()


DATA_STEPS = [("site-checks-2026-10", _data_step_site_checks), ("dry-run-2026-11-06", _data_step_dry_run),
              ("november-2026", _data_step_november), ("empty-october-2026", _data_step_empty_october)]


def apply_data_steps(db) -> list[str]:
    """One-off data updates, each applied once to an existing database (tracked in guide_content)."""
    rec = db.get(m.GuideContent, "data_steps")
    done = list((rec.content or {}).get("done", [])) if rec else []
    ran = []
    for name, fn in DATA_STEPS:
        if name in done:
            continue
        fn(db)
        done.append(name)
        ran.append(name)
    if ran:
        if rec is None:
            db.add(m.GuideContent(key="data_steps", content={"done": done}))
        else:
            rec.content = {"done": done}
    db.commit()
    return ran


# Columns added to existing tables after release; create_all only creates missing tables
ADDED_COLUMNS = [("questions", "score", "DOUBLE PRECISION"), ("question_results", "score", "DOUBLE PRECISION")]


def ensure_columns() -> None:
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, column, sql_type in ADDED_COLUMNS:
            if not insp.has_table(table):
                continue
            if column not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))


def ensure_seeded(force: bool = False) -> str:
    Base.metadata.create_all(engine)  # also adds new tables (such as skus) to an existing database
    ensure_columns()
    with SessionLocal() as db:
        ver = db.get(m.GuideContent, "seed_version")
        has_data = db.scalar(select(m.Retailer.id).limit(1)) is not None
        current = (ver.content or {}).get("version") if ver else None
    if not force and has_data and current == SEED_VERSION:
        with SessionLocal() as db:
            ensure_skus(db)
            ensure_sources(db)
            qs = ensure_question_set(db)
            steps = apply_data_steps(db)
            from ..services.access import sync_access_requests
            sync_access_requests(db)
        return f"up to date · {qs}" + (f" · data steps: {', '.join(steps)}" if steps else "")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)
        ensure_skus(db)
        ensure_sources(db)
        ensure_question_set(db)
        apply_data_steps(db)
        from ..services.access import sync_access_requests
        sync_access_requests(db)
    return f"seeded v{SEED_VERSION}"


def main(force: bool = False) -> None:
    print(ensure_seeded(force))


if __name__ == "__main__":
    import sys

    main(force="--force" in sys.argv)