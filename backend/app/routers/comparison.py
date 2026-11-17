"""Retailer, competitor and technology partner comparison (any signed-in user).

Each tab compares one entity (left) with any number of others (right) on the same dimensions,
inside the retailer × market × month picked in the header.

Where an assessment exists the real scores are used: a retailer with a workflow or illustrative
run in this market, or a vendor with treatment scores inside this retailer. Everything else is a
static placeholder (stable for the same inputs) until that data is loaded.
"""
import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import models as m
from ..auth import current_user
from ..db import get_db
from ..services.context import build_ctx, find_assessment, scores_for
from .overview import filters
from .registers import competitor_detail, placeholder_detail, placeholder_level, vendor_comparison

router = APIRouter(prefix="/api", tags=["comparison"], dependencies=[Depends(current_user)])

VENDORS = ["Microsoft", "Apple", "Dell", "HP", "Acer", "ASUS", "Lenovo"]
SETS = {
    "retailer": dict(label="Retailer comparison", left_label="Retailer", right_label="Compare with",
                     options=["Best Buy", "Costco", "Walmart", "Amazon.com", "Newegg.com", "Microsoft Center", "Target"]),
    "competitor": dict(label="Competitor comparison", left_label="Vendor", right_label="Competitors",
                       options=VENDORS, default_left="Microsoft"),
    # Apple is not a technology partner in this comparison, and Microsoft is the vendor being assessed, so it is left out too
    "partner": dict(label="Technology partner comparison", left_label="Technology partner", right_label="Compare with",
                    options=[v for v in VENDORS if v not in ("Apple", "Microsoft")], default_left="Dell"),
}


def _h(*parts: str) -> int:
    return int(hashlib.sha1("|".join(parts).encode()).hexdigest()[:8], 16)


def _static(kind: str, name: str, retailer: str, market: str, dim: str, months: list[str]) -> list[float]:
    """A stable placeholder level per month: a base between L1.6 and L3.8 that climbs gently to the latest month."""
    h = _h(kind, name, retailer, market, dim)
    base = 1.6 + (h % 2200) / 1000
    step = 0.03 + (h >> 12) % 7 / 100
    n = len(months)
    return [round(max(0.5, base - (n - 1 - i) * step), 2) for i in range(n)]


def _entity(name: str, source: str, dims: list[str], months: list[str], trend: dict[str, list[float | None]], slug: str = "") -> dict:
    scores = {d: (trend[d][-1] if trend[d] else None) for d in dims}
    vals = [v for v in scores.values() if v is not None]
    return dict(name=name, source=source, slug=slug, scores=scores,
                overall=round(sum(vals) / len(vals), 2) if vals else None,
                trend={d: trend[d] for d in dims})


def build(db: Session, kind: str, retailer: str, market: str, period: str) -> dict:
    if kind not in SETS:
        raise HTTPException(404, f"Unknown comparison '{kind}'. Use retailer, competitor or partner.")
    cfg = SETS[kind]
    a, rm = find_assessment(db, retailer, market, period)
    ctx = build_ctx(db, retailer=retailer, market=market, period=period)
    dims = [d.code for d in db.scalars(select(m.Dimension).order_by(m.Dimension.position))]
    all_months = db.scalars(select(m.Assessment).where(m.Assessment.retailer_market_id == rm.id).order_by(m.Assessment.period)
                            .options(selectinload(m.Assessment.results))).all()
    hist = [x for x in all_months if x.period <= a.period]
    months = [x.period.strftime("%Y-%m") for x in hist]

    def real_retailer(slug: str, name: str) -> dict | None:
        """Real month-by-month scores for a retailer that has runs in this market."""
        rm2 = db.scalar(select(m.RetailerMarket).join(m.Retailer).join(m.Market)
                        .where(m.Retailer.slug == slug, m.Market.code == market))
        if not rm2:
            return None
        runs = {x.period.strftime("%Y-%m"): x for x in db.scalars(
            select(m.Assessment).where(m.Assessment.retailer_market_id == rm2.id).options(selectinload(m.Assessment.results)))}
        if period not in runs:
            return None
        trend = {d: [] for d in dims}
        for p in months:
            sc = {s["dimension"]: s["value"] for s in scores_for(db, runs[p], ctx.questions)} if p in runs else {}
            for d in dims:
                trend[d].append(sc.get(d))
        return _entity(name, runs[period].data_source, dims, months, trend, slug=slug)

    current = real_retailer(retailer, rm.retailer.name)

    vendor_data = None
    if kind == "competitor":
        vc = vendor_comparison(retailer=retailer, market=market, period=period, db=db)
        vendor_data = vc

    by_name = {r.name.lower(): r.slug for r in db.scalars(select(m.Retailer))}
    entities = []
    for name in cfg["options"]:
        e = None
        if kind == "retailer" and name.lower() in by_name:
            e = real_retailer(by_name[name.lower()], name)
        if kind == "competitor" and vendor_data and name in vendor_data["vendors"]:
            trend = {}
            for d in dims:
                series = {t["period"]: t["scores"].get(name) for t in vendor_data["dim_trend"].get(d, [])}
                trend[d] = [placeholder_level(series[p]) if series.get(p) is not None else None for p in months]
            e = _entity(name, "static", dims, months, trend)
        if e is None:
            e = _entity(name, "static", dims, months, {d: _static(kind, name, retailer, market, d, months) for d in dims})
        entities.append(e)

    left = cfg.get("default_left") or rm.retailer.name
    right_default = next((x for x in cfg["options"] if x.lower() != left.lower()), "")
    return dict(kind=kind, label=cfg["label"], left_label=cfg["left_label"], right_label=cfg["right_label"],
                retailer=rm.retailer.name, slug=rm.retailer.slug, market=rm.market.name, market_code=rm.market.code,
                period=a.period.strftime("%Y-%m"), months=months,
                dimensions=[dict(code=d.code, name=d.name) for d in db.scalars(select(m.Dimension).order_by(m.Dimension.position))],
                options=cfg["options"], default_left=left, default_right=[right_default] if right_default else [],
                current=current, entities=entities)


@router.get("/comparison")
def comparison(kind: str = Query("retailer"), f: dict = Depends(filters), db: Session = Depends(get_db)):
    return build(db, kind, **f)


@router.get("/entity-detail")
def entity_detail(kind: str = Query(...), name: str = Query(...), dimension: str = Query(..., pattern="^D[0-9]{1,2}$"),
                  retailer: str = Query(...), market: str = Query(...), period: str = Query(...), db: Session = Depends(get_db)):
    """The scorecard pop-up for one entity in a comparison (same shape as /api/dimension-detail).

    Real retailers use /api/dimension-detail instead; this serves vendors and placeholders.
    """
    if kind == "competitor":
        vc = vendor_comparison(retailer=retailer, market=market, period=period, db=db)
        if name in vc["vendors"]:
            return competitor_detail(vendor=name, dimension=dimension, retailer=retailer, market=market, period=period, db=db)
    data = build(db, kind, retailer, market, period)
    e = next((x for x in data["entities"] if x["name"] == name), None)
    dim = db.get(m.Dimension, dimension)
    if not e or not dim:
        raise HTTPException(404, f"No {kind} data for {name} on {dimension}.")
    value = e["scores"].get(dimension) or 0
    ctx = build_ctx(db, retailer=retailer, market=market, period=period)
    lens = "Competitive · retailer" if kind == "retailer" else "Competitive · vendor"
    rows = [r for r in ctx.rows() if r["in_run"] and r["lens"] == lens and r["dimension"] == dimension]
    what = {"retailer": "retailer", "competitor": "competitor", "partner": "technology partner"}[kind]
    out = placeholder_detail(
        db, ctx, dim, value, rows, retailer, market,
        met_text=f"Level {int(value)} met (static placeholder): {name} as a {what} on {dim.name} in {data['market']}.",
        none_text=f"No level established yet for {name} on {dim.name}.",
        basis=f"Static placeholder · {SETS[kind]['label'].lower()}")
    out["competitor"] = dict(name=name, kind=kind, level=value)
    return out