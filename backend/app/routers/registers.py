"""Evidence register, score register and vendor comparison (any signed-in user)."""
from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import models as m
from ..auth import current_user
from ..db import get_db
from ..services.context import build_ctx, find_assessment, scores_for, scoring_settings
from ..services.scoring import UNSCORED
from .overview import filters

router = APIRouter(prefix="/api", tags=["registers"], dependencies=[Depends(current_user)])


def evidence_out(e: m.EvidenceRecord) -> dict:
    d = dict(e.data)
    d.update(id=e.id, eid=e.eid, cell=e.cell, stage=e.stage, confidence=e.confidence, register_status=e.register_status,
             requires_review=e.requires_review, approval=e.approval, reviewed_by=e.reviewed_by,
             reviewed_at=e.reviewed_at.isoformat() if e.reviewed_at else None, review_comment=e.review_comment, history=e.history or [])
    return d


@router.get("/evidence-register")
def evidence_register(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    states = {s["name"]: s for s in ctx.guide.get("evidence_states", [])}
    rows = [evidence_out(e) for e in sorted(ctx.assessment.evidence, key=lambda x: x.eid)]
    counts = defaultdict(int)
    for r in rows:
        counts[r["register_status"]] += 1
    return dict(info=ctx.info(), records=rows, states=list(states.values()),
                summary=dict(total=len(rows), by_status=dict(counts),
                             pending=sum(1 for r in rows if r["approval"] == "Pending review"),
                             approved=sum(1 for r in rows if r["approval"] == "Approved"),
                             h=sum(1 for r in rows if r.get("h_flag"))))


def source_type_group(t: str) -> str:
    """'Primary (filing)' -> 'Primary'; 'Vendor case study' stays as it is."""
    return (t or "Other").split("(")[0].strip() or "Other"


def source_out(s: m.Source, cited_by: list[str] | None = None) -> dict:
    return dict(id=s.id, retailer=s.retailer, market=s.market, code=s.code, title=s.title, publisher=s.publisher, date=s.date,
                source_type=s.source_type, group=source_type_group(s.source_type), url=s.url, dimensions=s.dimensions or [],
                used_for=s.used_for, citing_rows=s.citing_rows, cited_by=cited_by if cited_by is not None else [])


def _code_key(code: str):
    digits = "".join(ch for ch in code if ch.isdigit())
    return (int(digits) if digits else 10**6, code)


@router.get("/source-register")
def source_register(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    # Which questions cite each source in the selected month's results
    cited: dict[str, list[str]] = defaultdict(list)
    for r in ctx.assessment.results:
        for code in r.sources or []:
            cited[code].append(r.qid)
    rows = db.scalars(select(m.Source).where(m.Source.retailer == f["retailer"], m.Source.market == f["market"])).all()
    rows = sorted(rows, key=lambda s: _code_key(s.code))
    out = [source_out(s, sorted(set(cited.get(s.code, [])))) for s in rows]
    groups = defaultdict(int)
    for s in out:
        groups[s["group"]] += 1
    dims = sorted({d for s in out for d in s["dimensions"]})
    return dict(info=ctx.info(), sources=out,
                summary=dict(total=len(out), by_group=dict(groups), dimensions=dims,
                             cited_this_month=sum(1 for s in out if s["cited_by"])))


@router.get("/score-register")
def score_register(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    a = ctx.assessment
    snaps = db.scalars(select(m.ScoreSnapshot).where(m.ScoreSnapshot.assessment_id == a.id).order_by(m.ScoreSnapshot.version)).all()
    cfg = scoring_settings(db)
    live = [dict(dimension=s["dimension"], label=s["label"], value=s["value"], level=s["level"], cells=[c["pct"] for c in s["cells"]]) for s in ctx.scores]
    latest = snaps[-1].scores if snaps else []
    drift = [s["dimension"] for s in live if next((x for x in latest if x["dimension"] == s["dimension"]), {}).get("label") != s["label"]]

    # Month on month, for the same retailer × market
    months = db.scalars(select(m.Assessment).where(m.Assessment.retailer_market_id == a.retailer_market_id)
                        .order_by(m.Assessment.period).options(selectinload(m.Assessment.results))).all()
    monthly = []
    for x in months:
        sc = scores_for(db, x, ctx.questions)
        monthly.append(dict(period=x.period.strftime("%Y-%m"), data_source=x.data_source,
                            scores={s["dimension"]: dict(label=s["label"], value=s["value"]) for s in sc},
                            answered=sum(1 for r in x.results if r.status not in UNSCORED)))
    return dict(info=ctx.info(), current=dict(scores=live, weights=cfg["weights"], threshold=cfg["threshold"]),
                unsaved_changes=drift,
                versions=[dict(id=s.id, version=s.version, label=s.label, created_by=s.created_by, created_at=s.created_at.isoformat(),
                               weights=s.weights, threshold=s.threshold, scores=s.scores, notes=s.notes) for s in snaps],
                monthly=monthly)


@router.get("/vendor-comparison")
def vendor_comparison(retailer: str = Query(...), market: str = Query(...), period: str = Query(...), db: Session = Depends(get_db)):
    a, rm = find_assessment(db, retailer, market, period)
    rows = db.scalars(select(m.VendorScore).where(m.VendorScore.assessment_id == a.id)).all()
    themes = sorted({r.theme for r in rows})
    vendors = list(dict.fromkeys(r.vendor for r in sorted(rows, key=lambda r: r.id)))
    by = {(r.vendor, r.theme): r.score for r in rows}
    overall = {v: round(sum(by[(v, t)] for t in themes) / len(themes), 1) for v in vendors} if themes else {}

    months = db.scalars(select(m.Assessment).where(m.Assessment.retailer_market_id == rm.id).order_by(m.Assessment.period)).all()
    ids = [x.id for x in months]
    allrows = db.scalars(select(m.VendorScore).where(m.VendorScore.assessment_id.in_(ids))).all()
    agg = defaultdict(list)
    for r in allrows:
        agg[(r.assessment_id, r.vendor)].append(r.score)
    trend = [dict(period=x.period.strftime("%Y-%m"), scores={v: round(sum(agg[(x.id, v)]) / len(agg[(x.id, v)]), 1) for v in vendors if agg[(x.id, v)]})
             for x in months]
    # Month by month for each theme, for the competitor detail pop-up
    per = {(r.assessment_id, r.vendor, r.theme): r.score for r in allrows}
    theme_trend = {t: [dict(period=x.period.strftime("%Y-%m"), scores={v: per[(x.id, v, t)] for v in vendors if (x.id, v, t) in per})
                       for x in months] for t in themes}
    # Roll themes up to dimensions: each theme belongs to the dimension of its vendor-axis questions,
    # weighted by how many questions it has there
    weights: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for q in db.scalars(select(m.Question).where(m.Question.lens == "Competitive · vendor", m.Question.in_run.is_(True))):
        if q.sub_theme in themes:
            weights[q.dimension][q.sub_theme] += 1
    dim_order = [d.code for d in db.scalars(select(m.Dimension).order_by(m.Dimension.position, m.Dimension.code))]

    def roll(score_of) -> dict:
        out = {}
        for v in vendors:
            vals = [(score_of(v, t), w) for t, w in weights_items if score_of(v, t) is not None]
            out[v] = round(sum(x * w for x, w in vals) / sum(w for _, w in vals), 1) if vals else None
        return out

    dimensions, dim_trend = [], {}
    for dcode in dim_order:
        if dcode not in weights:
            continue
        weights_items = list(weights[dcode].items())
        dimensions.append(dict(dimension=dcode, themes=[dict(theme=t, questions=w) for t, w in weights_items],
                               scores=roll(lambda v, t: by.get((v, t)))))
        dim_trend[dcode] = [dict(period=x.period.strftime("%Y-%m"), scores={k: val for k, val in roll(lambda v, t, _x=x: per.get((_x.id, v, t))).items() if val is not None})
                            for x in months]

    return dict(retailer=rm.retailer.name, slug=rm.retailer.slug, market=rm.market.name, market_code=rm.market.code,
                period=a.period.strftime("%Y-%m"), data_source="illustrative",
                vendors=vendors, themes=themes, matrix=[dict(theme=t, scores={v: by[(v, t)] for v in vendors}) for t in themes],
                overall=overall, trend=trend, theme_trend=theme_trend, dimensions=dimensions, dim_trend=dim_trend)


# ---------------------------------------------------------------- Competitor scorecard (static placeholder)
def placeholder_level(score100: float | None) -> float:
    """Static placeholder until competitor assessments are loaded: treatment score (0–100) ÷ 20 → level 0–5."""
    return round((score100 or 0) / 20, 2)


@router.get("/competitor-detail")
def competitor_detail(vendor: str = Query(...), dimension: str = Query(..., pattern="^D[0-9]{1,2}$"),
                      retailer: str = Query(...), market: str = Query(...), period: str = Query(...), db: Session = Depends(get_db)):
    """The same shape as /api/dimension-detail, for a competitor vendor inside a retailer.

    Static placeholder: the level comes from the vendor's treatment score. Replace this
    endpoint's body when real competitor assessments are loaded; the pop-up does not change.
    """
    from fastapi import HTTPException
    from ..services.scoring import stage_for

    vc = vendor_comparison(retailer=retailer, market=market, period=period, db=db)
    if vendor not in vc["vendors"]:
        raise HTTPException(404, f"No data for {vendor}.")
    vd = next((x for x in vc["dimensions"] if x["dimension"] == dimension), None)
    dim = db.get(m.Dimension, dimension)
    if not vd or not dim:
        raise HTTPException(404, f"Dimension {dimension} has no competitor themes.")
    ctx = build_ctx(db, retailer=retailer, market=market, period=period)
    s100 = vd["scores"].get(vendor)
    value = placeholder_level(s100)
    themes = [t["theme"] for t in vd["themes"]]
    rows = [r for r in ctx.rows() if r["in_run"] and r["lens"] == "Competitive · vendor" and r["dimension"] == dimension and r["sub_theme"] in themes]
    lv = int(value)
    out = placeholder_detail(
        db, ctx, dim, value, rows, retailer, market,
        met_text=f"Level {lv} met (static placeholder): {vendor} scores {(s100 or 0):.1f} of 100 on {dim.name} in {vc['market']}.",
        none_text=f"No level established yet for {vendor} on {dim.name}.",
        basis=f"Static placeholder · themes: {', '.join(themes)}")
    out["competitor"] = dict(name=vendor, score100=s100, level=value)
    return out


def placeholder_detail(db: Session, ctx, dim: m.Dimension, value: float, rows: list[dict], retailer: str, market: str,
                       met_text: str, none_text: str, basis: str) -> dict:
    """A scorecard in the /api/dimension-detail shape for a static placeholder level.

    Replace the callers' level with real assessments when that data is loaded; the pop-up does not change.
    """
    from ..services.scoring import stage_for

    lv = int(value)
    frac = round(value - lv, 2)
    levels_meta = {l.level: l for l in db.scalars(select(m.Level).order_by(m.Level.level))}
    defs = {c.level: c for c in db.scalars(select(m.LevelCell).where(m.LevelCell.dimension == dim.code))}
    cells, levels = [], []
    for L in sorted(levels_meta):
        result = "Met" if L <= lv else ("Stops here" if L == lv + 1 else "Not reached")
        gate = "Met" if L <= lv else ("Not met" if L == lv + 1 else "")
        cells.append(dict(level=L, tested=True, n=0, total=0, weighted=0, pct=None, inferred=False, yes=0, other=0,
                          not_disclosed=0, result=result, gate=gate))
        d = defs.get(L)
        levels.append(dict(level=L, name=d.name if d else levels_meta[L].name, stage=d.stage if d else levels_meta[L].stage,
                           test=d.test if d else "", threshold="", requirement="", result=result, gate=gate,
                           pct=None, yes=0, other=0, total=0, counts={}, counted=0, weighted=0, questions=[]))
    label = f"L{value:.2f}" if lv else "Not established"
    score = dict(dimension=dim.code, level=lv, fraction=frac, value=value, number=f"L{value:.2f}", h=False, label=label,
                 key=f"L{lv}" if lv else "Not established", stage=stage_for(lv) if lv else "Not established",
                 ceiling=max(levels_meta), cells=cells)
    why = [dict(kind="met" if lv else "none", level=lv, text=met_text if lv else none_text)]
    if lv < max(levels_meta):
        why.append(dict(kind="fail", level=lv + 1, text=f"Level {lv + 1} not met yet: {round(frac * 100)}% of the way there (static placeholder)."))

    # Sources: what the related questions cite
    cites: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        for code in r["sources"]:
            cites[code].append(r["qid"])
    logged = {x.code: x for x in db.scalars(select(m.Source).where(m.Source.retailer == retailer, m.Source.market == market))}
    sources = []
    for code in sorted(cites, key=lambda c: -len(cites[c])):
        x = logged.get(code)
        sources.append(dict(code=code, title=x.title if x else "", publisher=x.publisher if x else "", date=x.date if x else "",
                            source_type=x.source_type if x else "", group=(x.source_type.split("(")[0].strip() if x and x.source_type else "Not logged"),
                            url=x.url if x else "", cited_by=sorted(set(cites[code])), old=False))
    cfg = scoring_settings(db)
    return dict(
        info=ctx.info(), dimension=dict(code=dim.code, name=dim.name, short=dim.short, summary=dim.summary, color=dim.color),
        score=score, threshold=cfg["threshold"], weights=cfg["weights"], levels=levels, why=why,
        facts=dict(score=lv, label=label, stage=score["stage"], evidence_status="P",
                   evidence_status_text="Static placeholder until this data is loaded",
                   stale=False, stale_sources=[], basis=basis, baseline=None,
                   questions=len(rows), answered=sum(1 for r in rows if r["status"] not in UNSCORED)),
        evidence=[], sources=sources,
    )