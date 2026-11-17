"""Admin-only: users, evidence approval, settings and score snapshots."""
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import models as m
from ..auth import CurrentUser, require_admin
from ..config import settings
from ..db import get_db
from ..services.context import build_ctx, find_assessment, scoring_settings
from .registers import evidence_out, source_out

router = APIRouter(prefix="/api/admin", tags=["admin"])
Admin = Depends(require_admin)


def now() -> datetime:
    return datetime.now(timezone.utc)


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "retailer"


# ======================================================================= Users
class UserIn(BaseModel):
    email: EmailStr
    name: str = ""
    role: str = Field("user", pattern="^(admin|user)$")


class RoleIn(BaseModel):
    role: str = Field(..., pattern="^(admin|user)$")


def user_out(u: m.User) -> dict:
    return dict(id=u.id, email=u.email, name=u.name, role=u.role, status=u.status, source=u.source, created_by=u.created_by,
                created_at=u.created_at.isoformat() if u.created_at else None, last_login=u.last_login.isoformat() if u.last_login else None,
                protected=u.email in settings.admin_set)


@router.get("/users")
def list_users(_: CurrentUser = Admin, db: Session = Depends(get_db)):
    users = db.scalars(select(m.User).where(m.User.status == "active").order_by(m.User.role, m.User.email)).all()
    return dict(users=[user_out(u) for u in users], allowed_domains=sorted(settings.domain_set), admin_emails=sorted(settings.admin_set))


@router.post("/users", status_code=201)
def add_user(body: UserIn, me: CurrentUser = Admin, db: Session = Depends(get_db)):
    email = body.email.lower()
    u = db.scalar(select(m.User).where(m.User.email == email))
    if u and u.status == "active":
        raise HTTPException(409, f"{email} already has access.")
    if not u:
        u = m.User(email=email)
        db.add(u)
    u.name, u.role, u.status, u.source, u.created_by, u.created_at = body.name.strip(), body.role, "active", "invited", me.email, now()
    db.commit()
    db.refresh(u)
    return user_out(u)


@router.patch("/users/{user_id}")
def change_role(user_id: int, body: RoleIn, me: CurrentUser = Admin, db: Session = Depends(get_db)):
    u = db.get(m.User, user_id)
    if not u or u.status != "active":
        raise HTTPException(404, "User not found.")
    if u.email in settings.admin_set:
        raise HTTPException(400, "This admin is set in configuration and cannot be changed here.")
    if u.id == me.id:
        raise HTTPException(400, "You cannot change your own role.")
    u.role = body.role
    db.commit()
    return user_out(u)


@router.delete("/users/{user_id}", status_code=204)
def remove_user(user_id: int, me: CurrentUser = Admin, db: Session = Depends(get_db)):
    u = db.get(m.User, user_id)
    if not u or u.status != "active":
        raise HTTPException(404, "User not found.")
    if u.email in settings.admin_set:
        raise HTTPException(400, "This admin is set in configuration and cannot be removed here.")
    if u.id == me.id:
        raise HTTPException(400, "You cannot remove yourself.")
    # Kept as a 'removed' row so a matching email domain does not grant access again
    u.status = "removed"
    db.commit()


# ======================================================================= Evidence approval
class DecisionIn(BaseModel):
    decision: str = Field(..., pattern="^(Approved|Rejected|Changes requested)$")
    comment: str = ""

    @field_validator("comment")
    @classmethod
    def trim(cls, v: str) -> str:
        return v.strip()[:2000]


@router.get("/evidence-approvals")
def evidence_approvals(scope: str = Query("all", pattern="^(all|current)$"), retailer: str | None = None, market: str | None = None,
                       period: str | None = None, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    q = (select(m.EvidenceRecord).where(m.EvidenceRecord.requires_review.is_(True))
         .options(selectinload(m.EvidenceRecord.assessment).selectinload(m.Assessment.retailer_market).selectinload(m.RetailerMarket.retailer),
                  selectinload(m.EvidenceRecord.assessment).selectinload(m.Assessment.retailer_market).selectinload(m.RetailerMarket.market)))
    if scope == "current":
        if not (retailer and market and period):
            raise HTTPException(400, "retailer, market and period are needed for the current view.")
        a, _rm = find_assessment(db, retailer, market, period)
        q = q.where(m.EvidenceRecord.assessment_id == a.id)
    recs = db.scalars(q).all()
    out = []
    for e in recs:
        a = e.assessment
        d = evidence_out(e)
        d.update(retailer=a.retailer_market.retailer.name, retailer_slug=a.retailer_market.retailer.slug,
                 market=a.retailer_market.market.name, market_code=a.retailer_market.market.code,
                 period=a.period.strftime("%Y-%m"), data_source=a.data_source)
        out.append(d)
    order = {"Pending review": 0, "Changes requested": 1, "Rejected": 2, "Approved": 3}
    out.sort(key=lambda d: (order.get(d["approval"], 9), d["data_source"] != "workflow", [-int(x) for x in d["period"].split("-")]))
    counts = {k: sum(1 for d in out if d["approval"] == k) for k in order}
    return dict(records=out, counts=counts)


@router.post("/evidence/{record_id}/decision")
def decide(record_id: int, body: DecisionIn, me: CurrentUser = Admin, db: Session = Depends(get_db)):
    e = db.get(m.EvidenceRecord, record_id)
    if not e:
        raise HTTPException(404, "Evidence record not found.")
    if body.decision != "Approved" and not body.comment:
        raise HTTPException(400, "Add a comment that says what is wrong or what is needed.")
    e.approval = body.decision
    e.reviewed_by, e.reviewed_at, e.review_comment = me.email, now(), body.comment or None
    if body.decision == "Approved":
        e.register_status = "Human-verified"
    elif body.decision == "Rejected":
        e.register_status = "Rejected for cell"
    e.history = [*(e.history or []), dict(at=now().isoformat(), by=me.email, decision=body.decision, comment=body.comment)]
    db.commit()
    db.refresh(e)
    return evidence_out(e)


# ======================================================================= Score snapshot
class SnapshotIn(BaseModel):
    retailer: str
    market: str
    period: str
    label: str = ""


@router.post("/score-register/snapshot", status_code=201)
def save_snapshot(body: SnapshotIn, me: CurrentUser = Admin, db: Session = Depends(get_db)):
    ctx = build_ctx(db, body.retailer, body.market, body.period)
    cfg = scoring_settings(db)
    last = db.scalar(select(func.max(m.ScoreSnapshot.version)).where(m.ScoreSnapshot.assessment_id == ctx.assessment.id)) or 0
    prev = db.scalar(select(m.ScoreSnapshot).where(m.ScoreSnapshot.assessment_id == ctx.assessment.id, m.ScoreSnapshot.version == last))
    scores = [dict(dimension=s["dimension"], label=s["label"], value=s["value"], level=s["level"], cells=[c["pct"] for c in s["cells"]]) for s in ctx.scores]
    changed = []
    if prev:
        old = {x["dimension"]: x["label"] for x in prev.scores}
        changed = [f"{s['dimension']} {old.get(s['dimension'], '—')} → {s['label']}" for s in scores if old.get(s["dimension"]) != s["label"]]
    approved = sum(1 for e in ctx.assessment.evidence if e.approval == "Approved" and e.reviewed_by != "Validation node")
    snap = m.ScoreSnapshot(assessment_id=ctx.assessment.id, version=last + 1, label=body.label.strip() or "Saved from current settings",
                           created_by=me.email, weights=cfg["weights"], threshold=cfg["threshold"], scores=scores,
                           notes=[("Changed vs v%d: " % last + "; ".join(changed) + ".") if changed else "No score changed since the last version.",
                                  f"Weights: {' · '.join(f'{k} {v:g}' for k, v in cfg['weights'].items())}. Threshold {cfg['threshold']:g}%.",
                                  f"{approved} evidence records approved by a reviewer at the time of saving."])
    db.add(snap)
    db.commit()
    return dict(version=snap.version)


# ======================================================================= Settings
@router.get("/settings")
def get_settings(_: CurrentUser = Admin, db: Session = Depends(get_db)):
    g = {x.key: x.content for x in db.scalars(select(m.GuideContent))}
    retailers = db.scalars(select(m.Retailer).order_by(m.Retailer.id).options(selectinload(m.Retailer.markets).selectinload(m.RetailerMarket.market))).all()
    mk = db.scalars(select(m.Market).order_by(m.Market.id)).all()
    dims = db.scalars(select(m.Dimension).order_by(m.Dimension.position, m.Dimension.code)).all()
    lv = db.scalars(select(m.Level).order_by(m.Level.level)).all()
    qcount = dict(db.execute(select(m.Question.dimension, func.count()).group_by(m.Question.dimension)).all())
    lcount = dict(db.execute(select(m.Question.level, func.count()).where(m.Question.level.is_not(None)).group_by(m.Question.level)).all())
    used = {}
    for r in retailers:
        for x in r.markets:
            used.setdefault(x.market.code, []).append(r.name)
    return dict(
        retailers=[dict(slug=r.slug, name=r.name, route=r.route, segment=r.segment, hq=r.hq,
                        markets=[dict(code=x.market.code, peers=x.peers) for x in r.markets]) for r in retailers],
        markets=[dict(code=x.code, name=x.name, currency=x.currency, market_maker=x.market_maker_entries, strategic=x.strategic_entries,
                      used_by=used.get(x.code, [])) for x in mk],
        dimensions=[dict(code=d.code, name=d.name, short=d.short, color=d.color, summary=d.summary, questions=qcount.get(d.code, 0)) for d in dims],
        levels=[dict(level=l.level, name=l.name, stage=l.stage, meaning=l.meaning, min_evidence=l.min_evidence, questions=lcount.get(l.level, 0)) for l in lv],
        scoring=scoring_settings(db), freshness=g.get("freshness", []), evidence_states=g.get("evidence_states", []),
        routes=g.get("routes", []),
        skus=[dict(code=s.code, name=s.name, position=s.position) for s in db.scalars(select(m.Sku).order_by(m.Sku.position, m.Sku.id))],
        sources=[source_out(s) for s in db.scalars(select(m.Source).order_by(m.Source.retailer, m.Source.market, m.Source.id))],
    )


# ---- Retailers
class RetailerIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    route: int = Field(1, ge=1, le=3)
    segment: str = Field("Strategic", max_length=40)
    markets: list[str] = Field(..., min_length=1)
    peers: list[str] = []


def _periods(db: Session):
    return sorted(set(db.scalars(select(m.Assessment.period))))


def _new_assessments(db: Session, rm: m.RetailerMarket):
    questions = db.scalars(select(m.Question).where(m.Question.in_run.is_(True))).all()
    for p in _periods(db):
        a = m.Assessment(retailer_market_id=rm.id, period=p, run_date=p.replace(day=1), data_source="not_run", old_sources=[],
                         notes=["Added in Settings. No agent run yet: every question is Check pending until the first run."])
        db.add(a)
        db.flush()
        for q in questions:
            db.add(m.QuestionResult(assessment_id=a.id, qid=q.qid, status="Check pending",
                                    finding="Not run yet for this account. Queued for the next agent run.", sources=[], flags=[]))


def _market_by_code(db: Session, code: str) -> m.Market:
    mk = db.scalar(select(m.Market).where(m.Market.code == code.upper()))
    if not mk:
        raise HTTPException(404, f"Country {code} does not exist. Add it under Countries first.")
    return mk


@router.post("/retailers", status_code=201)
def add_retailer(body: RetailerIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    slug = slugify(body.name)
    if db.scalar(select(m.Retailer).where((m.Retailer.slug == slug) | (func.lower(m.Retailer.name) == body.name.strip().lower()))):
        raise HTTPException(409, f"A retailer called {body.name} already exists.")
    mks = [_market_by_code(db, c) for c in dict.fromkeys(body.markets)]
    r = m.Retailer(slug=slug, name=body.name.strip(), hq=mks[0].code, route=body.route, segment=body.segment)
    db.add(r)
    db.flush()
    for mk in mks:
        rm = m.RetailerMarket(retailer_id=r.id, market_id=mk.id, peers=[p.strip() for p in body.peers if p.strip()])
        db.add(rm)
        db.flush()
        _new_assessments(db, rm)
    db.commit()
    return dict(slug=slug)


@router.put("/retailers/{slug}")
def update_retailer(slug: str, body: RetailerIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    r = db.scalar(select(m.Retailer).where(m.Retailer.slug == slug).options(selectinload(m.Retailer.markets).selectinload(m.RetailerMarket.market)))
    if not r:
        raise HTTPException(404, "Retailer not found.")
    r.name, r.route, r.segment = body.name.strip(), body.route, body.segment
    want = list(dict.fromkeys(c.upper() for c in body.markets))
    have = {x.market.code: x for x in r.markets}
    for code, rm in have.items():
        if code not in want:
            db.delete(rm)
    for code in want:
        if code not in have:
            mk = _market_by_code(db, code)
            rm = m.RetailerMarket(retailer_id=r.id, market_id=mk.id, peers=[p.strip() for p in body.peers if p.strip()])
            db.add(rm)
            db.flush()
            _new_assessments(db, rm)
    db.commit()
    return dict(slug=slug)


@router.delete("/retailers/{slug}", status_code=204)
def delete_retailer(slug: str, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    r = db.scalar(select(m.Retailer).where(m.Retailer.slug == slug))
    if not r:
        raise HTTPException(404, "Retailer not found.")
    if db.scalar(select(func.count(m.Retailer.id))) <= 1:
        raise HTTPException(400, "Keep at least one retailer.")
    if slug == settings.default_retailer:
        raise HTTPException(400, "This is the default retailer the app opens on. Change DEFAULT_RETAILER first.")
    for s in db.scalars(select(m.Source).where(m.Source.retailer == slug)):
        db.delete(s)
    db.delete(r)
    db.commit()


# ---- Countries
class MarketIn(BaseModel):
    code: str = Field(..., min_length=2, max_length=3, pattern="^[A-Za-z]{2,3}$")
    name: str = Field(..., min_length=2, max_length=80)
    currency: str = Field("USD", min_length=3, max_length=3)
    market_maker: int = Field(0, ge=0, le=999)
    strategic: int = Field(0, ge=0, le=999)


@router.post("/markets", status_code=201)
def add_market(body: MarketIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    code = body.code.upper()
    if db.scalar(select(m.Market).where(m.Market.code == code)):
        raise HTTPException(409, f"Country code {code} already exists.")
    db.add(m.Market(code=code, name=body.name.strip(), currency=body.currency.upper(), market_maker_entries=body.market_maker, strategic_entries=body.strategic))
    db.commit()
    return dict(code=code)


@router.put("/markets/{code}")
def update_market(code: str, body: MarketIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    mk = _market_by_code(db, code)
    mk.name, mk.currency, mk.market_maker_entries, mk.strategic_entries = body.name.strip(), body.currency.upper(), body.market_maker, body.strategic
    db.commit()
    return dict(code=mk.code)


@router.delete("/markets/{code}", status_code=204)
def delete_market(code: str, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    mk = _market_by_code(db, code)
    rms = db.scalars(select(m.RetailerMarket).where(m.RetailerMarket.market_id == mk.id).options(selectinload(m.RetailerMarket.retailer))).all()
    stranded = []
    for rm in rms:
        n = db.scalar(select(func.count(m.RetailerMarket.id)).where(m.RetailerMarket.retailer_id == rm.retailer_id))
        if n <= 1:
            stranded.append(rm.retailer.name)
    if stranded:
        raise HTTPException(400, f"{', '.join(stranded)} would have no country left. Give them another country, or remove them first.")
    for rm in rms:
        db.delete(rm)
    for s in db.scalars(select(m.Source).where(m.Source.market == mk.code)):
        db.delete(s)
    db.delete(mk)
    db.commit()


# ---- Sources (Source register)
class SourceIn(BaseModel):
    retailer: str = Field(..., min_length=1, max_length=40)
    market: str = Field(..., min_length=2, max_length=3)
    code: str = Field(..., pattern="^[A-Za-z]{1,3}[0-9]{1,4}$")
    title: str = Field(..., min_length=2, max_length=300)
    publisher: str = Field("", max_length=200)
    date: str = Field("", max_length=60)
    source_type: str = Field("", max_length=80)
    url: str = Field("", max_length=2000, pattern=r"^(|https?://\S+)$")
    dimensions: list[str] = []
    used_for: str = Field("", max_length=2000)
    citing_rows: int = Field(0, ge=0, le=9999)


def _check_account(db: Session, retailer: str, market: str) -> None:
    ok = db.scalar(select(m.RetailerMarket.id).join(m.Retailer).join(m.Market)
                   .where(m.Retailer.slug == retailer, m.Market.code == market.upper()))
    if not ok:
        raise HTTPException(400, f"{retailer} is not assessed in {market.upper()}. Pick a retailer and one of its countries.")


def _apply_source(s: m.Source, b: SourceIn) -> None:
    s.retailer, s.market, s.code = b.retailer, b.market.upper(), b.code.upper()
    s.title, s.publisher, s.date, s.source_type = b.title.strip(), b.publisher.strip(), b.date.strip(), b.source_type.strip()
    s.url, s.used_for, s.citing_rows = b.url.strip(), b.used_for.strip(), b.citing_rows
    s.dimensions = sorted({d.strip().upper() for d in b.dimensions if d.strip()})


def _source_clash(db: Session, b: SourceIn, skip_id: int | None = None) -> None:
    q = select(m.Source.id).where(m.Source.retailer == b.retailer, m.Source.market == b.market.upper(), m.Source.code == b.code.upper())
    if skip_id:
        q = q.where(m.Source.id != skip_id)
    if db.scalar(q):
        raise HTTPException(409, f"{b.code.upper()} already exists for this retailer and country.")


@router.post("/sources", status_code=201)
def add_source(body: SourceIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    _check_account(db, body.retailer, body.market)
    _source_clash(db, body)
    s = m.Source()
    _apply_source(s, body)
    db.add(s)
    db.commit()
    return dict(id=s.id)


@router.put("/sources/{source_id}")
def update_source(source_id: int, body: SourceIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    s = db.get(m.Source, source_id)
    if not s:
        raise HTTPException(404, "Source not found.")
    _check_account(db, body.retailer, body.market)
    _source_clash(db, body, skip_id=s.id)
    _apply_source(s, body)
    db.commit()
    return dict(id=s.id)


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: int, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    s = db.get(m.Source, source_id)
    if not s:
        raise HTTPException(404, "Source not found.")
    db.delete(s)
    db.commit()


# ---- SKUs (header SKU dropdown)
class SkuIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    code: str | None = Field(None, pattern="^[a-z0-9-]{2,40}$")
    position: int | None = Field(None, ge=0, le=999)


def _sku(db: Session, code: str) -> m.Sku:
    s = db.scalar(select(m.Sku).where(m.Sku.code == code))
    if not s:
        raise HTTPException(404, f"SKU {code} does not exist.")
    return s


@router.post("/skus", status_code=201)
def add_sku(body: SkuIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    name = body.name.strip()
    code = body.code or slugify(name)
    if db.scalar(select(m.Sku).where((m.Sku.code == code) | (func.lower(m.Sku.name) == name.lower()))):
        raise HTTPException(409, f"An SKU called {name} already exists.")
    pos = body.position if body.position is not None else (db.scalar(select(func.max(m.Sku.position))) or 0) + 1
    db.add(m.Sku(code=code, name=name, position=pos))
    db.commit()
    return dict(code=code)


@router.put("/skus/{code}")
def update_sku(code: str, body: SkuIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    s = _sku(db, code)
    name = body.name.strip()
    if db.scalar(select(m.Sku).where(func.lower(m.Sku.name) == name.lower(), m.Sku.id != s.id)):
        raise HTTPException(409, f"An SKU called {name} already exists.")
    s.name = name
    if body.position is not None:
        s.position = body.position
    db.commit()
    return dict(code=s.code)


@router.delete("/skus/{code}", status_code=204)
def delete_sku(code: str, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    s = _sku(db, code)
    if db.scalar(select(func.count(m.Sku.id))) <= 1:
        raise HTTPException(400, "Keep at least one SKU: the header dropdown needs one to show.")
    db.delete(s)
    db.commit()


# ---- Dimensions
class DimensionIn(BaseModel):
    code: str = Field(..., pattern="^D[0-9]{1,2}$")
    name: str = Field(..., min_length=2, max_length=120)
    short: str = Field("", max_length=80)
    color: str = Field("#605E5C", pattern="^#[0-9A-Fa-f]{6}$")
    summary: str = Field("", max_length=600)


@router.post("/dimensions", status_code=201)
def add_dimension(body: DimensionIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    if db.get(m.Dimension, body.code):
        raise HTTPException(409, f"{body.code} already exists.")
    pos = (db.scalar(select(func.max(m.Dimension.position))) or 0) + 1
    db.add(m.Dimension(code=body.code, name=body.name.strip(), short=(body.short or body.name).strip(), color=body.color, summary=body.summary.strip(), position=pos))
    db.flush()
    for l in db.scalars(select(m.Level)):
        db.add(m.LevelCell(dimension=body.code, level=l.level, name=l.name, stage=l.stage, test="To be defined", threshold="To be defined",
                           requirement="To be defined", anchor="To be defined", evidence="To be defined", boundary="To be defined"))
    db.commit()
    return dict(code=body.code)


@router.put("/dimensions/{code}")
def update_dimension(code: str, body: DimensionIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    d = db.get(m.Dimension, code)
    if not d:
        raise HTTPException(404, "Dimension not found.")
    d.name, d.short, d.color, d.summary = body.name.strip(), (body.short or body.name).strip(), body.color, body.summary.strip()
    db.commit()
    return dict(code=code)


@router.delete("/dimensions/{code}", status_code=204)
def delete_dimension(code: str, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    d = db.get(m.Dimension, code)
    if not d:
        raise HTTPException(404, "Dimension not found.")
    n = db.scalar(select(func.count()).select_from(m.Question).where(m.Question.dimension == code))
    if n:
        raise HTTPException(400, f"{code} has {n} questions in the question set. It can be removed once no question uses it.")
    if db.scalar(select(func.count()).select_from(m.Dimension)) <= 1:
        raise HTTPException(400, "Keep at least one dimension.")
    for c in db.scalars(select(m.LevelCell).where(m.LevelCell.dimension == code)):
        db.delete(c)
    db.flush()
    db.delete(d)
    db.commit()


# ---- Levels and scoring
class LevelIn(BaseModel):
    level: int = Field(..., ge=1, le=9)
    name: str = Field(..., min_length=2, max_length=40)
    stage: str = Field(..., pattern="^(Crawl|Walk|Run)$")
    meaning: str = Field("", max_length=600)
    min_evidence: str = Field("", max_length=600)


@router.post("/levels", status_code=201)
def add_level(body: LevelIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    top = db.scalar(select(func.max(m.Level.level))) or 0
    if body.level != top + 1:
        raise HTTPException(400, f"Levels are added in order. The next level is {top + 1}.")
    db.add(m.Level(level=body.level, name=body.name.strip(), stage=body.stage, meaning=body.meaning.strip(), min_evidence=body.min_evidence.strip()))
    db.flush()
    for d in db.scalars(select(m.Dimension)):
        db.add(m.LevelCell(dimension=d.code, level=body.level, name=body.name.strip(), stage=body.stage, test="To be defined", threshold="To be defined",
                           requirement="To be defined", anchor="To be defined", evidence="To be defined", boundary="To be defined"))
    db.commit()
    return dict(level=body.level)


@router.put("/levels/{level}")
def update_level(level: int, body: LevelIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    lv = db.get(m.Level, level)
    if not lv:
        raise HTTPException(404, "Level not found.")
    lv.name, lv.stage, lv.meaning, lv.min_evidence = body.name.strip(), body.stage, body.meaning.strip(), body.min_evidence.strip()
    for c in db.scalars(select(m.LevelCell).where(m.LevelCell.level == level)):
        c.name, c.stage = lv.name, lv.stage
    db.commit()
    return dict(level=level)


@router.delete("/levels/{level}", status_code=204)
def delete_level(level: int, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    top = db.scalar(select(func.max(m.Level.level))) or 0
    if level != top:
        raise HTTPException(400, f"Only the top level (Level {top}) can be removed.")
    if level <= 1:
        raise HTTPException(400, "Keep at least one level.")
    n = db.scalar(select(func.count()).select_from(m.Question).where(m.Question.level == level))
    if n:
        raise HTTPException(400, f"{n} questions test Level {level}. It can be removed once no question uses it.")
    for c in db.scalars(select(m.LevelCell).where(m.LevelCell.level == level)):
        db.delete(c)
    db.flush()
    db.delete(db.get(m.Level, level))
    db.commit()


class ScoringIn(BaseModel):
    weights: dict[str, float]
    threshold: float = Field(..., ge=1, le=100)
    infer: bool = True

    @field_validator("weights")
    @classmethod
    def check(cls, w: dict[str, float]) -> dict[str, float]:
        need = {"Yes", "Partial", "Issue", "No", "Not disclosed"}
        if set(w) != need:
            raise ValueError("Give a weight for Yes, Partial, Issue, No and Not disclosed.")
        if any(v < 0 or v > 1 for v in w.values()):
            raise ValueError("Weights go from 0 to 1.")
        return {k: w[k] for k in ["Yes", "Partial", "Issue", "No", "Not disclosed"]}


def _guide_put(db: Session, key: str, value) -> None:
    g = db.get(m.GuideContent, key)
    if g:
        g.content = value
    else:
        db.add(m.GuideContent(key=key, content=value))


@router.put("/scoring")
def update_scoring(body: ScoringIn, _: CurrentUser = Admin, db: Session = Depends(get_db)):
    g = db.get(m.GuideContent, "scoring")
    c = dict(g.content) if g else {}
    c.update(weights=body.weights, threshold=body.threshold, infer=body.infer)
    _guide_put(db, "scoring", c)
    db.commit()
    return scoring_settings(db)


# ---- Freshness policy and evidence states (edited as whole lists)
class FreshnessRow(BaseModel):
    evidence: str = Field(..., min_length=2, max_length=200)
    window: str = Field(..., min_length=1, max_length=40)
    action: str = Field(..., min_length=2, max_length=400)


class StateRow(BaseModel):
    name: str = Field(..., min_length=2, max_length=40)
    tone: str = Field("neutral", pattern="^(good|warning|critical|neutral)$")
    description: str = Field("", max_length=400)


@router.put("/freshness")
def update_freshness(rows: list[FreshnessRow], _: CurrentUser = Admin, db: Session = Depends(get_db)):
    _guide_put(db, "freshness", [r.model_dump() for r in rows])
    db.commit()
    return rows


@router.put("/evidence-states")
def update_states(rows: list[StateRow], _: CurrentUser = Admin, db: Session = Depends(get_db)):
    names = [r.name.strip().lower() for r in rows]
    if len(set(names)) != len(names):
        raise HTTPException(400, "Each evidence state needs a different name.")
    _guide_put(db, "evidence_states", [r.model_dump() for r in rows])
    db.commit()
    return rows


# ======================================================================= Access requests (allowlisting)
class AccessUpdate(BaseModel):
    status: str = Field(pattern="^(To request|Requested|Allowlisted)$")
    note: str = Field("", max_length=1000)
    contact: str | None = Field(None, max_length=200)


def access_out(r: m.AccessRequest) -> dict:
    from ..services.access import email_draft
    rm = r.retailer_market
    return dict(id=r.id, retailer=rm.retailer.name, retailer_slug=rm.retailer.slug, market=rm.market.name, market_code=rm.market.code,
                period=r.period.strftime("%Y-%m"), site=r.site, reason=r.reason, user_agent=r.user_agent, qids=r.qids or [],
                status=r.status, contact=r.contact or "", note=r.note or "", history=r.history or [],
                created_at=r.created_at.isoformat() if r.created_at else None, updated_at=r.updated_at.isoformat() if r.updated_at else None,
                email=email_draft(r))


@router.get("/access-requests")
def access_requests(scope: str = Query("all", pattern="^(all|current)$"), retailer: str | None = None, market: str | None = None,
                    _: CurrentUser = Admin, db: Session = Depends(get_db)):
    from ..services.access import sync_access_requests
    sync_access_requests(db)
    q = select(m.AccessRequest).options(selectinload(m.AccessRequest.retailer_market).selectinload(m.RetailerMarket.retailer),
                                        selectinload(m.AccessRequest.retailer_market).selectinload(m.RetailerMarket.market))
    if scope == "current":
        if not (retailer and market):
            raise HTTPException(400, "retailer and market are needed for the current view.")
        q = q.join(m.RetailerMarket).join(m.Retailer).join(m.Market).where(m.Retailer.slug == retailer, m.Market.code == market)
    rows = [access_out(r) for r in db.scalars(q)]
    order = {"To request": 0, "Requested": 1, "Allowlisted": 2}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["retailer"], r["market"]))
    return dict(requests=rows, counts={k: sum(1 for r in rows if r["status"] == k) for k in order})


@router.post("/access-requests/{rid}")
def update_access_request(rid: int, body: AccessUpdate, user: CurrentUser = Admin, db: Session = Depends(get_db)):
    from ..services.access import mark
    r = db.get(m.AccessRequest, rid)
    if not r:
        raise HTTPException(404, "Access request not found.")
    if r.status == "Allowlisted":
        raise HTTPException(409, "This request is closed: the crawler was allowlisted and the checks were queued for the next run.")
    mark(db, r, body.status, user.email, body.note.strip(), body.contact.strip() if body.contact is not None else None)
    db.refresh(r)
    return access_out(r)


# ======================================================================= Agency action plans
class PlanIn(BaseModel):
    retailer: str
    market: str
    qid: str = Field(max_length=10)
    owner: str = Field("", max_length=200)
    effort: str = Field("", pattern="^(|S|M|L)$")
    status: str = Field("Open", pattern="^(Open|In progress|Done)$")


@router.put("/action-plan")
def save_action_plan(body: PlanIn, user: CurrentUser = Admin, db: Session = Depends(get_db)):
    """Owner, effort and status of a Needs Optimization gap the agency fixes."""
    rm = db.scalar(select(m.RetailerMarket).join(m.Retailer).join(m.Market)
                   .where(m.Retailer.slug == body.retailer, m.Market.code == body.market))
    if not rm or not db.get(m.Question, body.qid):
        raise HTTPException(404, "Retailer, market or question not found.")
    p = db.scalar(select(m.ActionPlan).where(m.ActionPlan.retailer_market_id == rm.id, m.ActionPlan.qid == body.qid))
    if p is None:
        p = m.ActionPlan(retailer_market_id=rm.id, qid=body.qid)
        db.add(p)
    p.owner, p.effort, p.status, p.updated_by = body.owner.strip(), body.effort, body.status, user.email
    db.commit()
    return dict(qid=p.qid, owner=p.owner, effort=p.effort, status=p.status)